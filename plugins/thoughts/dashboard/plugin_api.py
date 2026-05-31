"""Thoughts dashboard plugin — live human-readable operational thoughts.

Mounted at /api/plugins/thoughts/ by the dashboard plugin system.

This plugin intentionally does **not** expose raw model chain-of-thought.
It merges explicit Mind Event ledger entries with Kanban task events/runs into
concise, human-readable one-line operational summaries so Coop can watch what
Hermes is noticing, deciding, routing, doing, and deferring without leaking
private reasoning traces.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status as http_status

from hermes_cli import kanban_db, mind_events
from hermes_constants import get_hermes_home

try:
    from .material_action_reducer import material_action_reducer
except ImportError:  # tests load this file directly via importlib spec
    import importlib.util

    _material_path = Path(__file__).with_name("material_action_reducer.py")
    _material_spec = importlib.util.spec_from_file_location("thoughts_material_action_reducer", _material_path)
    if _material_spec is None or _material_spec.loader is None:
        raise
    _material_mod = importlib.util.module_from_spec(_material_spec)
    _material_spec.loader.exec_module(_material_mod)
    material_action_reducer = _material_mod.material_action_reducer

router = APIRouter()

_MAX_THOUGHT_CHARS = 220
_WS_POLL_SECONDS = 1.0


def _check_ws_token(provided: Optional[str]) -> bool:
    """Constant-time compare against the dashboard session token.

    Mirrors the Kanban plugin's WebSocket auth pattern. Bare FastAPI tests do
    not import the dashboard server, so they are allowed through.
    """
    if not provided:
        return False
    try:
        from hermes_cli import web_server as _ws
    except Exception:
        return True
    expected = getattr(_ws, "_SESSION_TOKEN", None)
    if not expected:
        return True
    return hmac.compare_digest(str(provided), str(expected))


def _resolve_board(board: Optional[str]) -> Optional[str]:
    if board is None or board == "":
        return None
    try:
        normed = kanban_db._normalize_board_slug(board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if normed and normed != kanban_db.DEFAULT_BOARD and not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {normed!r} does not exist")
    return normed


def _payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clean_text(value: Any, *, limit: int = _MAX_THOUGHT_CHARS) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _quote_title(title: Optional[str], task_id: str) -> str:
    title = _clean_text(title, limit=80)
    return f"“{title}”" if title else task_id


def _reason(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return _clean_text(value, limit=140)
    return ""


def _summary_snippet(row: sqlite3.Row, payload: dict[str, Any]) -> str:
    return _reason(payload, "summary", "result", "message", "reason", "error") or _clean_text(row["run_summary"], limit=140)


def _thought_for_event(row: sqlite3.Row) -> str:
    """Convert a task event into a one-line operational thought.

    These are summaries of observable state transitions, not hidden chain of
    thought. Keep wording human-like and useful for spotting blind spots.
    """
    kind = str(row["kind"] or "")
    payload = _payload(row["payload"])
    title = _quote_title(row["title"], row["task_id"])
    assignee = _clean_text(row["assignee"] or "someone", limit=40)
    profile = _clean_text(row["run_profile"] or payload.get("profile") or assignee, limit=40)
    status = _clean_text(row["status"], limit=30)
    detail = _summary_snippet(row, payload)

    if kind == "created":
        who = _clean_text(payload.get("assignee") or assignee, limit=40)
        return f"I queued {title} for {who}; this is now a trackable unit of work."
    if kind == "linked":
        parent = _clean_text(payload.get("parent") or payload.get("parent_id"), limit=32)
        child = _clean_text(payload.get("child") or payload.get("child_id"), limit=32)
        return f"I connected dependencies so {child or 'a child task'} waits on {parent or title}."
    if kind == "unlinked":
        return f"I loosened a dependency around {title} so the next safe step can proceed."
    if kind == "promoted":
        return f"{title} is ready; I’m letting the dispatcher pick it up when capacity is available."
    if kind in {"claimed", "spawned"}:
        return f"{profile} started working on {title}; I’m watching for proof, blockers, or review needs."
    if kind == "heartbeat":
        return f"{profile} is still working on {title}; no new decision is needed yet."
    if kind in {"completed", "done"}:
        suffix = f" — {detail}" if detail else ""
        return f"{profile} finished {title}; I’m checking what this unlocks next{suffix}."
    if kind == "blocked":
        reason = _reason(payload, "reason", "summary", "error")
        if reason.startswith("approval-required") or reason.startswith("approval-needed"):
            return f"{title} hit a real approval gate; I should stop before the boundary and prep the handoff."
        if reason.startswith("review-required"):
            return f"{title} needs reviewer eyes, not human approval; local branch/worktree review should continue."
        suffix = f" Reason: {reason}" if reason else ""
        return f"{title} is blocked; I need to classify whether this is real risk or fake risk.{suffix}"
    if kind in {"crashed", "timed_out", "spawn_failed", "spawn_auto_blocked"}:
        suffix = f" Detail: {detail}" if detail else ""
        return f"{title} failed during execution; I should recover state, inspect logs, and choose the safest next step.{suffix}"
    if kind == "commented":
        author = _clean_text(payload.get("author") or "someone", limit=40)
        return f"{author} added context to {title}; I should fold it into routing before acting."
    if kind == "reclaimed":
        return f"I reclaimed stale work on {title}; I should decide whether to retry, reroute, or block with evidence."
    if kind == "archived":
        return f"{title} was archived; I’ll keep it out of the active work loop."
    if kind == "status_changed":
        return f"{title} moved to {status}; I’m recalculating what this status unlocks."

    suffix = f" Detail: {detail}" if detail else ""
    return f"I observed {kind or 'an update'} on {title}; I’m updating the board picture{suffix}."


def _quality_for_event(row: sqlite3.Row, thought: str) -> dict[str, Any]:
    kind = str(row["kind"] or "")
    payload = _payload(row["payload"])
    reason = _reason(payload, "reason", "summary", "error")
    base = {
        "event_type": "kanban_motion",
        "category": "kanban",
        "why_it_matters": "Kanban motion shows observable work movement, but higher-quality signals should explain why the work matters.",
        "confidence_label": "high",
        "urgency": "silent",
        "autonomy_quality": "useful_but_noisy",
        "next_best_action": "watch",
    }
    if kind in {"completed", "done"}:
        base.update(
            why_it_matters="Completed work may unlock the next Kryden or Hermes capability step.",
            autonomy_quality="good_autonomous_action",
            next_best_action="watch",
        )
    elif kind == "blocked":
        if reason.startswith(("approval-required", "approval-needed")):
            base.update(
                event_type="approval_boundary",
                category="decision",
                why_it_matters="Hermes correctly stopped at a real approval boundary instead of taking consequential action silently.",
                urgency="needs_review",
                autonomy_quality="blocked_correctly",
                next_best_action="request_permission",
            )
        elif reason.startswith("review-required"):
            base.update(
                event_type="decision_signal",
                category="decision",
                why_it_matters="Reviewer-needed work is an internal quality gate, not necessarily a human approval gate.",
                urgency="needs_review",
                autonomy_quality="blocked_correctly",
                next_best_action="escalate",
            )
        else:
            base.update(
                event_type="uncertainty_signal",
                category="uncertainty",
                why_it_matters="An unclassified blocker creates ambiguity about risk, dependency, approval, or execution failure.",
                urgency="needs_review",
                autonomy_quality="unclear",
                next_best_action="escalate",
            )
    elif kind in {"crashed", "timed_out", "spawn_failed", "spawn_auto_blocked", "gave_up", "protocol_violation"}:
        base.update(
            event_type="risk_signal",
            category="decision",
            why_it_matters="Execution failures require recovery judgment before more autonomous churn accumulates.",
            urgency="needs_review",
            autonomy_quality="failed_recovery_needed",
            next_best_action="retry",
        )
    elif kind in {"claimed", "spawned", "promoted"}:
        base.update(
            why_it_matters="Dispatcher motion affects what Hermes is actively spending worker capacity on.",
            autonomy_quality="good_autonomous_action",
            next_best_action="watch",
        )
    return base


def _entry_dict(row: sqlite3.Row, *, board: str) -> dict[str, Any]:
    thought = _clean_text(_thought_for_event(row))
    quality = _quality_for_event(row, thought)
    return {
        "id": f"kanban:{row['id']}",
        "event_seq": int(row["id"]),
        "task_id": row["task_id"],
        "task_title": row["title"],
        "kind": row["kind"],
        "event_type": quality["event_type"],
        "category": quality["category"],
        "created_at": row["created_at"],
        "board": board,
        "source": "kanban",
        "thought": thought,
        "summary": thought,
        "why_it_matters": quality["why_it_matters"],
        "confidence_label": quality["confidence_label"],
        "urgency": quality["urgency"],
        "autonomy_quality": quality["autonomy_quality"],
        "next_best_action": quality["next_best_action"],
        "raw_chain_of_thought": False,
    }


def _entry_time(entry: dict[str, Any]) -> float:
    value = entry.get("created_at")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        from datetime import datetime

        text = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _merge_entries(*groups: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for group in groups:
        merged.extend(group)
    merged.sort(key=lambda entry: (_entry_time(entry), str(entry.get("id", ""))))
    return merged[-limit:]


def _profile_root_for(home: Path) -> Path:
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _profile_label_for(home: Path, root: Path) -> str:
    if home == root:
        return "default"
    if home.parent == root / "profiles":
        return home.name
    return _clean_text(home.name, limit=40) or "active"


def _configured_profile_homes(*, include_all: bool) -> list[tuple[str, Path]]:
    active_home = Path(get_hermes_home())
    if not include_all:
        return [(_profile_label_for(active_home, _profile_root_for(active_home)), active_home)]
    root = _profile_root_for(active_home)
    homes: list[tuple[str, Path]] = [("default", root)]
    profiles_dir = root / "profiles"
    if profiles_dir.exists():
        for child in sorted(profiles_dir.iterdir(), key=lambda p: p.name):
            if child.is_dir():
                homes.append((child.name, child))
    seen: set[Path] = set()
    unique: list[tuple[str, Path]] = []
    for label, home in homes:
        resolved = home.resolve() if home.exists() else home
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append((label, home))
    return unique


def _profile_mind_entries(*, include_all: bool, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    ledgers: list[dict[str, Any]] = []
    for profile, home in _configured_profile_homes(include_all=include_all):
        ledger = home / "state" / "mind" / "events.jsonl"
        ledgers.append({"profile": profile, "path": str(ledger), "exists": ledger.exists()})
        for entry in mind_events.read_events(limit=limit, path=ledger):
            entry = dict(entry)
            entry["profile"] = profile
            entry["id"] = f"mind:{profile}:{entry.get('event_seq', entry.get('id', '0'))}"
            refs = [str(ref) for ref in entry.get("evidence_refs", []) if ref]
            profile_ref = f"profile:{profile}"
            if profile_ref not in refs:
                refs.append(profile_ref)
            entry["evidence_refs"] = refs
            entry["raw_chain_of_thought"] = False
            groups.append(entry)
    scope = "configured_profiles" if include_all else "active_hermes_home_profile"
    active_path = str(mind_events.default_ledger_path())
    return groups, {"path": active_path, "scope": scope, "ledgers": ledgers}


_ROUTINE_SESSION_TOOLS = {
    "read_file", "search_files", "web_search", "web_extract", "browser_snapshot",
    "browser_console", "browser_get_images", "browser_vision", "todo", "kanban_show",
    "skills_list", "skill_view", "session_search", "terminal_status", "browser_navigate",
}
_EDIT_TOOLS = {"patch", "write_file"}
_TEST_COMMAND_RE = re.compile(r"\b(pytest|npm test|pnpm test|yarn test|go test|cargo test|swift test|python -m pytest)\b", re.I)
_ERROR_RE = re.compile(r"\b(error|failed|traceback|exception|permission denied|timed out|exit_code\D*[1-9])\b", re.I)
_EVENT_TYPE_CATEGORY_CONTRACT = {
    "kanban_motion": "kanban",
    "cron_silent": "cron",
    "cron_result": "cron",
    "cron_failure": "cron",
    "self_improvement_signal": "self_improvement",
    "policy_candidate": "self_improvement",
    "opportunity_signal": "revenue",
    "revenue_signal": "revenue",
    "uncertainty_signal": "uncertainty",
    "decision_signal": "decision",
    "approval_boundary": "decision",
    "risk_signal": "decision",
    "user_context_update": "mind",
    "mind_signal": "mind",
}
_ACTIVE_NEXT_BEST_ACTIONS = {
    "verify",
    "retry",
    "escalate",
    "create_task",
    "draft_for_review",
    "request_permission",
}
_FOLLOWTHROUGH_METADATA_KEYS = {
    "created_task_id",
    "existing_task_id",
    "duplicate_task_id",
    "related_task_id",
    "verification_ref",
    "verified_by",
    "verified_at",
    "outcome_ref",
    "outcome_event_id",
    "route_ref",
    "approval_request_id",
    "approval_message_id",
}


def _clean_ref(value: Any) -> str:
    return _clean_text(value, limit=180)


def _entry_refs(entry: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for ref in entry.get("evidence_refs") or []:
        text = _clean_ref(ref)
        if text and text not in seen:
            seen.add(text)
            refs.append(text)
    return refs


def _append_ref(refs: list[str], ref: Any) -> None:
    text = _clean_ref(ref)
    if text and text not in refs:
        refs.append(text)


def _expected_category_for(event_type: Any) -> str | None:
    return _EVENT_TYPE_CATEGORY_CONTRACT.get(_clean_text(event_type, limit=80))


def _metadata(entry: dict[str, Any]) -> dict[str, Any]:
    raw = entry.get("metadata")
    return raw if isinstance(raw, dict) else {}


def _has_followthrough_link(entry: dict[str, Any]) -> bool:
    metadata = _metadata(entry)
    if any(metadata.get(key) for key in _FOLLOWTHROUGH_METADATA_KEYS):
        return True
    refs = _entry_refs(entry)
    action = _clean_text(entry.get("next_best_action"), limit=40)
    if action == "verify":
        return any(ref.startswith(("verification:", "outcome:", "kanban-event:completed", "task-run:completed")) for ref in refs)
    if action == "request_permission":
        return any(ref.startswith(("approval:", "message:", "slack:", "telegram:")) for ref in refs)
    return bool(entry.get("related_task_id") or entry.get("task_id") or entry.get("related_session_id"))


def _validate_cognition_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Validate and annotate explicit cognition claims for the dashboard feed.

    This is a read-time output contract check, not hidden reasoning. It corrects
    category/event_type emission mismatches for filtering, adds sparse evidence
    refs where the reducer has observable source IDs, and labels whether a gap is
    producer-output hygiene or a real follow-through/action-quality issue.
    """
    entry = dict(entry)
    original_category = _clean_text(entry.get("category"), limit=80)
    event_type = _clean_text(entry.get("event_type"), limit=80)
    expected_category = _expected_category_for(event_type)
    findings: list[str] = []
    gap_type = "none"

    if expected_category and original_category != expected_category:
        findings.append("category_contract_mismatch")
        entry["category"] = expected_category
        gap_type = "output_emission_gap"

    refs = _entry_refs(entry)
    if entry.get("source") == "kanban" or str(entry.get("id", "")).startswith("kanban:"):
        _append_ref(refs, f"kanban-event:{entry.get('event_seq')}")
        if entry.get("task_id"):
            _append_ref(refs, f"kanban-task:{entry.get('task_id')}")
    elif str(entry.get("id", "")).startswith("churn:"):
        _append_ref(refs, f"kanban-event:{entry.get('event_seq')}")
        if entry.get("task_id"):
            _append_ref(refs, f"kanban-task:{entry.get('task_id')}")
    elif str(entry.get("id", "")).startswith("mind:"):
        _append_ref(refs, f"mind-line:{entry.get('event_seq')}")

    if not refs:
        findings.append("missing_evidence_refs")
        evidence_status = "missing"
    else:
        evidence_status = "present"
    entry["evidence_refs"] = refs

    action = _clean_text(entry.get("next_best_action"), limit=40)
    if action in _ACTIVE_NEXT_BEST_ACTIONS:
        followthrough_status = "linked" if _has_followthrough_link(entry) else "missing_followthrough_link"
        if followthrough_status == "missing_followthrough_link":
            findings.append("missing_followthrough_link")
            if gap_type == "none":
                gap_type = "action_quality_gap"
    else:
        followthrough_status = "not_required"

    entry["cognition_validation"] = {
        "claim_contract": "valid" if "category_contract_mismatch" not in findings else "corrected",
        "expected_category": expected_category or original_category,
        "original_category": original_category,
        "evidence_status": evidence_status,
        "followthrough_status": followthrough_status,
        "gap_type": gap_type,
        "findings": findings,
    }
    return entry


def _validate_cognition_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_validate_cognition_entry(entry) for entry in entries]


def _decode_tool_calls(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, dict) else []


def _tool_name_and_args(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    raw_fn = call.get("function")
    fn: dict[str, Any] = raw_fn if isinstance(raw_fn, dict) else {}
    name = _clean_text(fn.get("name") or call.get("name") or call.get("tool_name"), limit=80)
    raw_args = fn.get("arguments") or call.get("arguments") or call.get("args") or {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except Exception:
            raw_args = {}
    return name, raw_args if isinstance(raw_args, dict) else {}


def _is_test_terminal(tool_name: str, args: dict[str, Any]) -> bool:
    if tool_name != "terminal":
        return False
    return bool(_TEST_COMMAND_RE.search(str(args.get("command") or "")))


def _session_action_kind(tool_name: str, args: dict[str, Any], result: str) -> str | None:
    if tool_name in _EDIT_TOOLS:
        return "code_edit"
    if _is_test_terminal(tool_name, args):
        return "test_run"
    if tool_name == "delegate_task":
        return "delegation"
    if tool_name == "cronjob" and str(args.get("action") or "").lower() in {"create", "update", "remove", "pause", "resume", "run"}:
        return "cron_change"
    if tool_name == "send_message":
        return "message_delivery_attempt"
    if tool_name == "memory":
        return "memory_change"
    if tool_name == "skill_manage":
        return "skill_change"
    if tool_name == "process" and _ERROR_RE.search(result or ""):
        return "terminal_error"
    if tool_name == "terminal" and _ERROR_RE.search(result or ""):
        return "terminal_error"
    if tool_name in {"patch", "write_file"}:
        return "code_edit"
    return None


def _session_action_reducer(*, include_all_profiles: bool, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for profile, home in _configured_profile_homes(include_all=include_all_profiles):
        db_path = home / "state.db"
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                """
                SELECT id, session_id, tool_calls, timestamp
                FROM messages
                WHERE tool_calls IS NOT NULL AND tool_calls != ''
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(50, min(limit * 10, 1000)),),
            ).fetchall()
            result_rows = conn.execute(
                """
                SELECT tool_call_id, tool_name, content
                FROM messages
                WHERE role = 'tool' AND tool_call_id IS NOT NULL
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(100, min(limit * 20, 2000)),),
            ).fetchall()
        except sqlite3.Error:
            conn.close()
            continue
        finally:
            conn.close()
        results = {str(row["tool_call_id"]): str(row["content"] or "") for row in result_rows}
        by_session: dict[str, dict[str, Any]] = {}
        for row in reversed(rows):
            session_id = str(row["session_id"] or "")
            bucket = by_session.setdefault(session_id, {"created_at": row["timestamp"], "message_ids": [], "tools": {}, "kinds": set()})
            bucket["created_at"] = max(float(bucket["created_at"] or 0), float(row["timestamp"] or 0))
            for call in _decode_tool_calls(row["tool_calls"]):
                if not isinstance(call, dict):
                    continue
                tool_name, args = _tool_name_and_args(call)
                if not tool_name or tool_name in _ROUTINE_SESSION_TOOLS:
                    continue
                result = results.get(str(call.get("id") or ""), "")
                kind = _session_action_kind(tool_name, args, result)
                if not kind:
                    continue
                bucket["message_ids"].append(int(row["id"]))
                bucket["tools"][tool_name] = int(bucket["tools"].get(tool_name, 0)) + 1
                bucket["kinds"].add(kind)
        for session_id, bucket in by_session.items():
            kinds = set(bucket["kinds"])
            tools = sorted(bucket["tools"])
            refs = [f"session:{session_id}", f"profile:{profile}"] + [f"message:{mid}" for mid in sorted(set(bucket["message_ids"]))[:6]]

            def _append_action(kind: str, thought: str, why: str, autonomy: str = "good_autonomous_action") -> None:
                out.append({
                    "id": f"session-action:{profile}:{session_id}:{kind}:{max(bucket['message_ids']) if bucket['message_ids'] else 0}",
                    "event_seq": max(bucket["message_ids"]) if bucket["message_ids"] else 0,
                    "created_at": bucket["created_at"],
                    "source": "session_action_reducer",
                    "profile": profile,
                    "kind": kind,
                    "event_type": "decision_signal" if kind != "terminal_error" else "risk_signal",
                    "category": "decision",
                    "thought": _clean_text(thought),
                    "summary": _clean_text(thought),
                    "why_it_matters": why,
                    "evidence_refs": refs,
                    "related_session_id": session_id,
                    "confidence_label": "medium",
                    "urgency": "needs_review" if kind == "terminal_error" else "silent",
                    "autonomy_quality": autonomy,
                    "next_best_action": "retry" if kind == "terminal_error" else "watch",
                    "raw_chain_of_thought": False,
                })

            if "code_edit" in kinds and "test_run" in kinds:
                _append_action(
                    "code_edit_test_cycle",
                    "I observed a code edit plus test run cycle; this is stronger evidence than a prose status update.",
                    "Edit/test cycles are consequential verification signals and should orient review without exposing raw diffs, commands, or outputs.",
                )
                kinds.discard("code_edit")
                kinds.discard("test_run")
            for kind in sorted(kinds):
                if kind == "terminal_error":
                    _append_action(
                        kind,
                        "I observed a significant terminal/process error; recovery should inspect state before retrying.",
                        "Execution errors affect whether autonomous work is making progress or just accumulating churn.",
                        autonomy="failed_recovery_needed",
                    )
                else:
                    _append_action(
                        kind,
                        f"I observed consequential session actions via {', '.join(tools[:4])}; I’m keeping only sparse evidence refs, not raw tool data.",
                        "Sparse action summaries expose material work without turning Thoughts into raw tool-call spam.",
                    )
    out.sort(key=lambda entry: (_entry_time(entry), str(entry.get("id", ""))))
    return out[-limit:]


def _churn_meta_entries(conn: sqlite3.Connection, *, board: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            e.id, e.task_id, e.kind, e.payload, e.created_at,
            t.title, t.assignee, t.status,
            r.profile AS run_profile, r.summary AS run_summary
        FROM task_events e
        LEFT JOIN tasks t ON t.id = e.task_id
        LEFT JOIN task_runs r ON r.id = e.run_id
        ORDER BY e.id DESC
        LIMIT ?
        """,
        (max(100, min(limit * 4, 500)),),
    ).fetchall()
    churn_kinds = {
        "blocked", "unblocked", "reclaimed", "claimed", "spawned", "crashed",
        "timed_out", "spawn_failed", "spawn_auto_blocked", "gave_up",
        "protocol_violation", "promoted",
    }
    by_task: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if str(row["kind"] or "") in churn_kinds and row["task_id"]:
            by_task.setdefault(str(row["task_id"]), []).append(row)
    out: list[dict[str, Any]] = []
    for task_id, task_rows in by_task.items():
        task_rows = sorted(task_rows, key=lambda r: int(r["id"]))
        kinds = [str(r["kind"] or "") for r in task_rows]
        distinct = set(kinds)
        if len(task_rows) < 6 or len(distinct) < 3:
            continue
        if not ({"blocked", "unblocked", "reclaimed", "crashed", "gave_up", "protocol_violation"} & distinct):
            continue
        latest = task_rows[-1]
        title = _quote_title(latest["title"], task_id)
        likely = "routing semantics are unclear between human approval, reviewer review, dependency wait, execution failure, and risk boundary"
        thought = _clean_text(
            f"Repeated state churn detected on {title}. Likely issue: {likely}. Suggested fix: split blocked_reason into human_approval_required, reviewer_required, dependency_wait, execution_failure, and risk_boundary.",
            limit=360,
        )
        out.append({
            "id": f"churn:{task_id}:{latest['id']}",
            "event_seq": int(latest["id"]),
            "task_id": task_id,
            "task_title": latest["title"],
            "kind": "self_improvement_signal",
            "event_type": "policy_candidate",
            "category": "self_improvement",
            "created_at": latest["created_at"],
            "board": board,
            "source": "kanban_churn_compressor",
            "thought": thought,
            "summary": thought,
            "why_it_matters": "Compressed churn reveals where Hermes autonomy semantics are wasting worker cycles or hiding review/approval boundaries.",
            "confidence_label": "medium",
            "urgency": "needs_review",
            "autonomy_quality": "failed_recovery_needed",
            "next_best_action": "create_task",
            "raw_chain_of_thought": False,
        })
    return out[:10]


def _query_entries(
    *,
    board: Optional[str],
    profiles: str = "active",
    session_actions: bool = False,
    cursor: int = 0,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], int, str, dict[str, Any]]:
    resolved = _resolve_board(board)
    conn = kanban_db.connect(board=resolved)
    try:
        active_board = resolved or kanban_db.get_current_board()
        limit = max(1, min(int(limit), 500))
        rows = conn.execute(
            """
            SELECT
                e.id,
                e.task_id,
                e.kind,
                e.payload,
                e.created_at,
                t.title,
                t.assignee,
                t.status,
                r.profile AS run_profile,
                r.summary AS run_summary
            FROM task_events e
            LEFT JOIN tasks t ON t.id = e.task_id
            LEFT JOIN task_runs r ON r.id = e.run_id
            WHERE e.id > ?
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (max(0, int(cursor)), limit),
        ).fetchall()
        entries = [_entry_dict(row, board=active_board) for row in reversed(rows)]
        include_all_profiles = str(profiles or "active").lower() in {"all", "configured", "multi", "profiles"}
        mind_entries, mind_ledger = _profile_mind_entries(include_all=include_all_profiles, limit=limit)
        churn_entries = _churn_meta_entries(conn, board=active_board, limit=limit)
        action_entries = _session_action_reducer(include_all_profiles=include_all_profiles, limit=limit) if session_actions else []
        material_entries = material_action_reducer(include_all_profiles=include_all_profiles, limit=limit)
        entries = _merge_entries(entries, mind_entries, churn_entries, action_entries, material_entries, limit=limit)
        entries = _validate_cognition_entries(entries)
        latest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
        return entries, int(latest or 0), active_board, mind_ledger
    finally:
        conn.close()


@router.get("/thoughts")
def get_thoughts(
    board: Optional[str] = Query(default=None),
    profiles: str = Query(default="active"),
    session_actions: bool = Query(default=False),
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=120, ge=1, le=500),
):
    entries, latest, active_board, mind_ledger = _query_entries(
        board=board,
        profiles=profiles,
        session_actions=session_actions,
        cursor=cursor,
        limit=limit,
    )
    return {
        "board": active_board,
        "entries": entries,
        "latest_event_id": latest,
        "raw_chain_of_thought": False,
        "mind_ledger": mind_ledger,
        "note": "Thoughts are safe one-line operational summaries from Mind Event ledgers, sparse consequential session actions, material worker actions, and Kanban activity, not raw model chain-of-thought.",
    }


@router.websocket("/events")
async def thoughts_events(ws: WebSocket):
    token = ws.query_params.get("token")
    if not _check_ws_token(token):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    board = ws.query_params.get("board") or None
    profiles = ws.query_params.get("profiles") or "active"
    session_actions = str(ws.query_params.get("session_actions") or "").lower() in {"1", "true", "yes", "on"}
    cursor = int(ws.query_params.get("cursor") or 0)
    limit = int(ws.query_params.get("limit") or 100)
    try:
        while True:
            entries, latest, active_board, mind_ledger = _query_entries(
                board=board,
                profiles=profiles,
                session_actions=session_actions,
                cursor=cursor,
                limit=limit,
            )
            if entries:
                cursor = latest
                await ws.send_text(json.dumps({
                    "board": active_board,
                    "entries": entries,
                    "latest_event_id": latest,
                    "mind_ledger": mind_ledger,
                    "raw_chain_of_thought": False,
                }))
            await asyncio.sleep(_WS_POLL_SECONDS)
    except WebSocketDisconnect:
        return
