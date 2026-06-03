"""Producer for material/operational actions not captured by routine filtering.

Emits self_improvement_signal or uncertainty_signal entries for terminal,
file operations (read/write/patch), and skill management tools.

These actions represent real work happening in Hermes that should be visible
in the Thoughts feed for operational awareness without exposing raw contents.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db
from hermes_constants import get_hermes_home

_ROUTINE_SESSION_TOOLS = {
    "search_files", "web_search", "web_extract", "browser_snapshot",
    "browser_console", "browser_get_images", "browser_vision", "todo", "kanban_show",
    "session_search", "terminal_status", "browser_navigate",
}

_EDIT_TOOLS = {"patch", "write_file"}

_TEST_COMMAND_RE = re.compile(r"\b(pytest|npm test|pnpm test|yarn test|go test|cargo test|swift test|python -m pytest)\b", re.I)
_ERROR_RE = re.compile(r"\b(error|failed|traceback|exception|permission denied|timed out|exit_code\D*[1-9])\b", re.I)

# Tools tracked as material actions for operational awareness
_MATERIAL_TOOLS = {
    "terminal", "read_file", "write_file", "patch",
    "skill_manage", "skill_view"
}


def _clean_text(value: Any, *, limit: int = 220) -> str:
    text = " ".join(str(value or "").replace("\n", " ").split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


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


def _configured_profile_homes(*, include_all: bool) -> list[tuple[str, Path]]:
    from pathlib import Path

    active_home = Path(get_hermes_home())
    if not include_all:
        return [(_clean_text(active_home.name, limit=40) or "active", active_home)]

    root = active_home.parent if active_home.parent.name == "profiles" else active_home
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


def _entry_time(entry: dict[str, Any]) -> float:
    value = entry.get("created_at")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            from datetime import datetime
            text = value.replace("Z", "+00:00")
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def material_action_reducer(*, include_all_profiles: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    """Producer for material/operational actions not captured by routine filtering.

    Emits self_improvement_signal or uncertainty_signal entries for terminal,
    file operations (read/write/patch), and skill management tools.

    Args:
        include_all_profiles: If True, scan all configured profiles. Otherwise, just active profile.
        limit: Max number of entries to return.

    Returns:
        List of sparse operational thought entries.
    """
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
            # Get messages with tool calls
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
                if not tool_name or tool_name not in _MATERIAL_TOOLS:
                    continue

                result = results.get(str(call.get("id") or ""), "")

                # Determine kind based on tool
                if tool_name == "terminal":
                    if _ERROR_RE.search(result or ""):
                        kind = "terminal_error"
                    else:
                        kind = "terminal"
                elif tool_name in {"read_file", "write_file", "patch"}:
                    kind = "file_operation"
                elif tool_name in {"skill_manage", "skill_view"}:
                    kind = "skill_change"
                else:
                    kind = tool_name

                bucket["message_ids"].append(int(row["id"]))
                bucket["tools"][tool_name] = int(bucket["tools"].get(tool_name, 0)) + 1
                bucket["kinds"].add(kind)

        for session_id, bucket in by_session.items():
            kinds = set(bucket["kinds"])
            refs = [f"session:{session_id}", f"profile:{profile}"] + [f"message:{mid}" for mid in sorted(set(bucket["message_ids"]))[:6]]

            for kind in sorted(kinds):
                if kind == "terminal":
                    out.append({
                        "id": f"material:{profile}:{session_id}:{kind}:{max(bucket['message_ids']) if bucket['message_ids'] else 0}",
                        "event_seq": max(bucket["message_ids"]) if bucket["message_ids"] else 0,
                        "created_at": bucket["created_at"],
                        "source": "material_action_reducer",
                        "profile": profile,
                        "kind": kind,
                        "event_type": "decision_signal",
                        "category": "decision",
                        "thought": "I observed a terminal command execution; this is material operational work.",
                        "summary": "I observed a terminal command execution; this is material operational work.",
                        "why_it_matters": "Terminal commands represent real system actions Hermes is performing.",
                        "evidence_refs": refs,
                        "related_session_id": session_id,
                        "confidence_label": "medium",
                        "urgency": "silent",
                        "autonomy_quality": "good_autonomous_action",
                        "next_best_action": "watch",
                        "raw_chain_of_thought": False,
                    })
                elif kind == "terminal_error":
                    out.append({
                        "id": f"material:{profile}:{session_id}:{kind}:{max(bucket['message_ids']) if bucket['message_ids'] else 0}",
                        "event_seq": max(bucket["message_ids"]) if bucket["message_ids"] else 0,
                        "created_at": bucket["created_at"],
                        "source": "material_action_reducer",
                        "profile": profile,
                        "kind": kind,
                        "event_type": "risk_signal",
                        "category": "decision",
                        "thought": "I observed a terminal command error; this needs review before autonomous retries.",
                        "summary": "I observed a terminal command error; this needs review before autonomous retries.",
                        "why_it_matters": "Terminal failures affect system state and require recovery judgment.",
                        "evidence_refs": refs,
                        "related_session_id": session_id,
                        "confidence_label": "medium",
                        "urgency": "needs_review",
                        "autonomy_quality": "failed_recovery_needed",
                        "next_best_action": "retry",
                        "raw_chain_of_thought": False,
                    })
                elif kind == "file_operation":
                    out.append({
                        "id": f"material:{profile}:{session_id}:{kind}:{max(bucket['message_ids']) if bucket['message_ids'] else 0}",
                        "event_seq": max(bucket["message_ids"]) if bucket["message_ids"] else 0,
                        "created_at": bucket["created_at"],
                        "source": "material_action_reducer",
                        "profile": profile,
                        "kind": kind,
                        "event_type": "decision_signal",
                        "category": "decision",
                        "thought": "I observed file read/write/patch operations; Hermes is making persistent changes.",
                        "summary": "I observed file read/write/patch operations; Hermes is making persistent changes.",
                        "why_it_matters": "File operations are material work that should be visible in operational awareness.",
                        "evidence_refs": refs,
                        "related_session_id": session_id,
                        "confidence_label": "medium",
                        "urgency": "silent",
                        "autonomy_quality": "good_autonomous_action",
                        "next_best_action": "watch",
                        "raw_chain_of_thought": False,
                    })
                elif kind == "skill_change":
                    out.append({
                        "id": f"material:{profile}:{session_id}:{kind}:{max(bucket['message_ids']) if bucket['message_ids'] else 0}",
                        "event_seq": max(bucket["message_ids"]) if bucket["message_ids"] else 0,
                        "created_at": bucket["created_at"],
                        "source": "material_action_reducer",
                        "profile": profile,
                        "kind": kind,
                        "event_type": "self_improvement_signal",
                        "category": "self_improvement",
                        "thought": "I observed skill management operations; Hermes is evolving its own capabilities.",
                        "summary": "I observed skill management operations; Hermes is evolving its own capabilities.",
                        "why_it_matters": "Skill changes affect Hermes autonomy and should be tracked as self-improvement signals.",
                        "evidence_refs": refs,
                        "related_session_id": session_id,
                        "confidence_label": "medium",
                        "urgency": "silent",
                        "autonomy_quality": "good_autonomous_action",
                        "next_best_action": "watch",
                        "raw_chain_of_thought": False,
                    })

        conn.close()

    out.sort(key=lambda entry: (_entry_time(entry), str(entry.get("id", ""))))
    return out[-limit:]
