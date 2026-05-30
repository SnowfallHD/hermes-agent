"""Profile-local Mind Event ledger for safe cognitive telemetry.

This module stores explicit, structured operational cognition events — not raw
model chain-of-thought. Events are compact summaries of observations,
uncertainties, decisions, routes, and followups that other Hermes subsystems can
emit when they want the dashboard to show what Hermes is noticing or doing.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home

MAX_SUMMARY_CHARS = 280
MAX_RATIONALE_CHARS = 500
DEFAULT_LIMIT = 200
MIN_COMPRESSIBLE_TEMPLATE_EVENTS = 3
_KIND_DEFAULT = "observation"
_SOURCE_DEFAULT = "system"
_ALLOWED_SCALAR = str | int | float | bool | None

VALID_CATEGORIES = {
    "mind",
    "kanban",
    "cron",
    "revenue",
    "self_improvement",
    "uncertainty",
    "decision",
}
VALID_EVENT_TYPES = {
    "kanban_motion",
    "cron_silent",
    "self_improvement_signal",
    "opportunity_signal",
    "revenue_signal",
    "uncertainty_signal",
    "decision_signal",
    "approval_boundary",
    "policy_candidate",
    "risk_signal",
    "user_context_update",
    "cron_result",
    "cron_failure",
    "mind_signal",
}
VALID_CONFIDENCE_LABELS = {"low", "medium", "high"}
VALID_URGENCY = {"silent", "daily_brief", "needs_review", "immediate"}
VALID_AUTONOMY_QUALITY = {
    "good_autonomous_action",
    "useful_but_noisy",
    "questionable_priority",
    "blocked_correctly",
    "failed_recovery_needed",
    "unclear",
}
VALID_NEXT_BEST_ACTIONS = {
    "ignore",
    "watch",
    "verify",
    "retry",
    "escalate",
    "create_task",
    "draft_for_review",
    "request_permission",
}
PASSIVE_NEXT_BEST_ACTIONS = {"ignore", "watch"}
REQUIRED_QUALITY_FIELDS = (
    "event_type",
    "category",
    "why_it_matters",
    "confidence_label",
    "urgency",
    "autonomy_quality",
    "next_best_action",
)

_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "credential",
    "private_key",
)


def default_ledger_path() -> Path:
    """Return the active profile's append-only mind event JSONL path."""

    return get_hermes_home() / "state" / "mind" / "events.jsonl"


def clean_text(value: Any, *, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Normalize arbitrary text to one compact line."""

    text = " ".join(str(value or "").replace("\n", " ").split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def append_event(
    *,
    source: str,
    kind: str,
    summary: str,
    rationale: str = "",
    evidence_refs: Iterable[str] | None = None,
    related_task_id: str | None = None,
    related_session_id: str | None = None,
    priority: str = "normal",
    confidence: float | str | None = None,
    next_action: str = "",
    event_type: str | None = None,
    why_it_matters: str = "",
    confidence_label: str | None = None,
    urgency: str | None = None,
    autonomy_quality: str | None = None,
    next_best_action: str | None = None,
    category: str | None = None,
    metadata: dict[str, Any] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Append one sanitized mind event and return the stored event.

    The event contract is intentionally explicit. Callers provide a short
    human-facing summary and optional compact rationale/evidence. This keeps the
    feed useful without recording hidden chain-of-thought.
    """

    _validate_required_quality_fields(
        event_type=event_type,
        category=category,
        why_it_matters=why_it_matters,
        confidence_label=confidence_label,
        urgency=urgency,
        autonomy_quality=autonomy_quality,
        next_best_action=next_best_action,
    )
    out_path = Path(path) if path is not None else default_ledger_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    created_at = time.time()
    source_clean = clean_text(source, limit=48) or _SOURCE_DEFAULT
    kind_clean = clean_text(kind, limit=64) or _KIND_DEFAULT
    priority_clean = clean_text(priority, limit=24) or "normal"
    type_clean = _normalize_choice(event_type or kind_clean, VALID_EVENT_TYPES, default=_event_type_for_kind(kind_clean))
    category_clean = _normalize_category(category, source=source_clean, kind=kind_clean, priority=priority_clean, event_type=type_clean)
    confidence_value = _coerce_confidence(confidence)
    event = {
        "created_at": created_at,
        "source": source_clean,
        "kind": kind_clean,
        "event_type": type_clean,
        "category": category_clean,
        "summary": clean_text(summary, limit=MAX_SUMMARY_CHARS),
        "rationale": clean_text(rationale, limit=MAX_RATIONALE_CHARS),
        "why_it_matters": clean_text(why_it_matters or rationale, limit=MAX_RATIONALE_CHARS),
        "evidence_refs": [clean_text(ref, limit=180) for ref in (evidence_refs or ()) if clean_text(ref, limit=180)],
        "related_task_id": clean_text(related_task_id, limit=80) if related_task_id else None,
        "related_session_id": clean_text(related_session_id, limit=120) if related_session_id else None,
        "priority": priority_clean,
        "confidence": confidence_value,
        "confidence_label": _normalize_confidence_label(confidence_label, confidence_value),
        "urgency": _normalize_choice(urgency, VALID_URGENCY, default="silent"),
        "autonomy_quality": _normalize_choice(autonomy_quality, VALID_AUTONOMY_QUALITY, default="unclear"),
        "next_action": clean_text(next_action, limit=MAX_SUMMARY_CHARS),
        "next_best_action": _normalize_choice(next_best_action, VALID_NEXT_BEST_ACTIONS, default="watch"),
        "metadata": _sanitize_metadata(metadata or {}),
        "raw_chain_of_thought": False,
    }
    event["next_best_action"] = calibrate_next_best_action(event)
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    return event


def read_events(*, limit: int = DEFAULT_LIMIT, path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read recent mind events from newest-relevant JSONL, preserving order."""

    in_path = Path(path) if path is not None else default_ledger_path()
    if not in_path.exists():
        return []
    limit = max(1, min(int(limit), 1000))
    lines = in_path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    start_line = max(1, _line_count(in_path) - len(lines) + 1)
    events: list[dict[str, Any]] = []
    for offset, line in enumerate(lines, start=start_line):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        normalized = _normalize_event(parsed, line_no=offset)
        if normalized:
            events.append(normalized)
    return _compress_repeated_templates(events)


def _normalize_event(event: dict[str, Any], *, line_no: int) -> dict[str, Any] | None:
    summary = clean_text(event.get("summary") or event.get("thought") or event.get("message"))
    if not summary:
        return None
    created_at = event.get("created_at")
    if not isinstance(created_at, (int, float)):
        created_at = 0.0
    source = clean_text(event.get("source"), limit=48) or _SOURCE_DEFAULT
    kind = clean_text(event.get("kind"), limit=64) or _KIND_DEFAULT
    priority = clean_text(event.get("priority"), limit=24) or "normal"
    event_type = _normalize_choice(event.get("event_type") or kind, VALID_EVENT_TYPES, default=_event_type_for_kind(kind))
    confidence = _coerce_confidence(event.get("confidence"))
    next_action = clean_text(event.get("next_action"), limit=MAX_SUMMARY_CHARS)
    normalized = {
        "id": f"mind:{line_no}",
        "event_seq": int(line_no),
        "created_at": created_at,
        "source": source,
        "kind": kind,
        "event_type": event_type,
        "category": _normalize_category(event.get("category"), source=source, kind=kind, priority=priority, event_type=event_type),
        "thought": summary,
        "summary": summary,
        "rationale": clean_text(event.get("rationale"), limit=MAX_RATIONALE_CHARS),
        "why_it_matters": clean_text(event.get("why_it_matters") or event.get("rationale"), limit=MAX_RATIONALE_CHARS),
        "evidence_refs": event.get("evidence_refs") if isinstance(event.get("evidence_refs"), list) else [],
        "task_id": event.get("related_task_id"),
        "task_title": None,
        "related_task_id": event.get("related_task_id"),
        "related_session_id": event.get("related_session_id"),
        "priority": priority,
        "confidence": confidence,
        "confidence_label": _normalize_confidence_label(event.get("confidence_label"), confidence),
        "urgency": _normalize_choice(event.get("urgency"), VALID_URGENCY, default="silent"),
        "autonomy_quality": _normalize_choice(event.get("autonomy_quality"), VALID_AUTONOMY_QUALITY, default="unclear"),
        "next_action": next_action,
        "next_best_action": _normalize_choice(event.get("next_best_action") or next_action, VALID_NEXT_BEST_ACTIONS, default="watch"),
        "metadata": _sanitize_metadata(event.get("metadata") if isinstance(event.get("metadata"), dict) else {}),
        "raw_chain_of_thought": False,
    }
    normalized["next_best_action"] = calibrate_next_best_action(normalized)
    return normalized


def calibrate_next_best_action(event: dict[str, Any]) -> str:
    """Deterministically upgrade passive next actions for high-signal Thoughts.

    Producers still provide the first judgment, but the feed should not leave
    strong uncertainty, approval/risk, revenue, or self-improvement signals as
    passive ``watch`` rows. This calibration is deliberately conservative: it
    only upgrades ``ignore``/``watch`` rows, preserves explicit concrete actions,
    respects existing task links/dedupe markers, and honors metadata caps such as
    ``max_tasks_per_run`` before recommending new task creation.
    """

    action = _normalize_choice(event.get("next_best_action"), VALID_NEXT_BEST_ACTIONS, default="watch")
    if action not in PASSIVE_NEXT_BEST_ACTIONS:
        return action

    event_type = clean_text(event.get("event_type"), limit=80)
    category = clean_text(event.get("category"), limit=80)
    confidence_label = _normalize_confidence_label(event.get("confidence_label"), _coerce_confidence(event.get("confidence")))
    urgency = _normalize_choice(event.get("urgency"), VALID_URGENCY, default="silent")
    autonomy_quality = _normalize_choice(event.get("autonomy_quality"), VALID_AUTONOMY_QUALITY, default="unclear")

    # Low-information silent cron/status rows are intentionally compressible and
    # should not become work just because they are numerous.
    if urgency == "silent" and event_type == "cron_silent":
        return action

    text = _event_search_text(event)
    has_attention = urgency in {"daily_brief", "needs_review", "immediate"}
    high_signal = confidence_label in {"medium", "high"} and has_attention

    if event_type == "approval_boundary":
        return "request_permission"

    if event_type == "risk_signal" or "privacy review" in text or "sensitive" in text:
        return "escalate" if urgency in {"needs_review", "immediate"} else "watch"

    if event_type == "uncertainty_signal" or category == "uncertainty" or "missing_outcome_evidence" in text:
        return "verify" if high_signal else "watch"

    if event_type in {"revenue_signal", "opportunity_signal"} or category == "revenue":
        if not high_signal or _has_existing_followup(event) or _task_cap_exhausted(event):
            return "watch"
        if any(marker in text for marker in ("contact", "outreach", "dm", "email", "lead", "prospect", "customer")):
            return "draft_for_review"
        return "create_task"

    if event_type in {"self_improvement_signal", "policy_candidate"} or category == "self_improvement":
        if not high_signal or _has_existing_followup(event) or _task_cap_exhausted(event):
            return "watch"
        if autonomy_quality == "failed_recovery_needed":
            return "retry"
        return "create_task"

    return action


def _event_search_text(event: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("summary", "thought", "rationale", "why_it_matters", "next_action"):
        value = event.get(key)
        if value:
            parts.append(str(value))
    for ref in event.get("evidence_refs") or []:
        parts.append(str(ref))
    raw_metadata = event.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    parts.extend(str(value) for value in metadata.values() if isinstance(value, _ALLOWED_SCALAR))
    return " ".join(parts).lower()


def _has_existing_followup(event: dict[str, Any]) -> bool:
    if event.get("related_task_id") or event.get("task_id"):
        return True
    raw_metadata = event.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    for key in (
        "existing_task_id",
        "duplicate_task_id",
        "created_task_id",
        "related_task_id",
        "dedupe_key",
        "idempotency_key",
    ):
        if metadata.get(key):
            return True
    return False


def _task_cap_exhausted(event: dict[str, Any]) -> bool:
    raw_metadata = event.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    cap = _coerce_int(metadata.get("max_tasks_per_run"))
    if cap is None:
        return False
    emitted = max(
        _coerce_int(metadata.get("task_proposals_emitted")) or 0,
        _coerce_int(metadata.get("tasks_created_this_run")) or 0,
        _coerce_int(metadata.get("created_tasks_count")) or 0,
    )
    return emitted >= max(cap, 0)


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _compress_repeated_templates(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compress low-information repeated templates while preserving high-signal events.

    The raw JSONL ledger remains append-only and unmodified. This read-time reducer
    only turns repeated silent cron success rows into one aggregate feed item with
    counts, evidence refs, and the covered time/line window. Failures, approvals,
    revenue, decisions, uncertainty, and self-improvement events pass through.
    """

    silent_indexes = [idx for idx, event in enumerate(events) if _is_compressible_cron_silent(event)]
    if len(silent_indexes) < MIN_COMPRESSIBLE_TEMPLATE_EVENTS:
        return events

    silent_events = [events[idx] for idx in silent_indexes]
    aggregate = _aggregate_cron_silent_events(silent_events)
    first_silent_index = silent_indexes[0]
    silent_index_set = set(silent_indexes)
    out: list[dict[str, Any]] = []
    for idx, event in enumerate(events):
        if idx == first_silent_index:
            out.append(aggregate)
        if idx in silent_index_set:
            continue
        out.append(event)
    return out


def _is_compressible_cron_silent(event: dict[str, Any]) -> bool:
    if not (
        event.get("event_type") == "cron_silent"
        and event.get("category") == "cron"
        and event.get("urgency") == "silent"
    ):
        return False

    next_best_action = event.get("next_best_action")
    if next_best_action == "ignore":
        return True

    # Legacy/raw cron_silent rows predate the quality contract. During read-time
    # normalization they receive the default next_best_action="watch" and
    # autonomy_quality="unclear", even though the source row is the same low-signal
    # repeated status template. Compress only those recognizable status rows; leave
    # explicit watch/retry/escalation decisions and non-template cron events visible.
    return (
        next_best_action == "watch"
        and event.get("autonomy_quality") == "unclear"
        and _looks_like_silent_cron_status_template(event)
    )


def _looks_like_silent_cron_status_template(event: dict[str, Any]) -> bool:
    summary = clean_text(event.get("summary") or event.get("thought"), limit=MAX_SUMMARY_CHARS).lower()
    return (
        event.get("source") == "cron"
        and event.get("kind") == "cron_silent"
        and "ran silently" in summary
        and "no user-facing followup was needed" in summary
    )


def _aggregate_cron_silent_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(events)
    first = events[0]
    created_values = [float(event["created_at"]) for event in events if isinstance(event.get("created_at"), (int, float))]
    seq_values = [int(event.get("event_seq") or 0) for event in events if int(event.get("event_seq") or 0) > 0]
    evidence_refs = _unique_evidence_refs(events)
    if seq_values:
        evidence_refs.append(f"mind-lines:{min(seq_values)}-{max(seq_values)}")
    job_counts = _cron_job_counts(events)
    summary = f"{count} silent cron completions compressed from repeated status templates."
    why = "Silent cron successes are healthy background-loop evidence, but repeated per-job status rows create feed churn without needing Coop's attention."
    aggregate = dict(first)
    aggregate.update(
        {
            "id": f"mind:cron_silent_aggregate:{min(seq_values) if seq_values else first.get('event_seq', 0)}",
            "created_at": max(created_values) if created_values else first.get("created_at", 0.0),
            "source": "cron",
            "kind": "cron_silent",
            "event_type": "cron_silent",
            "category": "cron",
            "thought": summary,
            "summary": summary,
            "rationale": "Repeated silent cron status rows were compressed at read time; raw ledger entries remain available as evidence refs.",
            "why_it_matters": why,
            "evidence_refs": evidence_refs,
            "priority": "cron",
            "confidence": 1.0,
            "confidence_label": "high",
            "urgency": "silent",
            "autonomy_quality": "good_autonomous_action",
            "next_action": "",
            "next_best_action": "ignore",
            "metadata": {
                "compressed_count": count,
                "template": "cron_silent",
                "first_created_at": min(created_values) if created_values else first.get("created_at", 0.0),
                "last_created_at": max(created_values) if created_values else first.get("created_at", 0.0),
                "job_counts": job_counts,
            },
            "raw_chain_of_thought": False,
        }
    )
    return aggregate


def _unique_evidence_refs(events: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for event in events:
        for ref in event.get("evidence_refs") or []:
            text = clean_text(ref, limit=180)
            if text and text not in seen:
                seen.add(text)
                refs.append(text)
    return refs[:20]


def _cron_job_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        job_id = clean_text(metadata.get("job_id"), limit=80) if isinstance(metadata, dict) else ""
        if not job_id:
            for ref in event.get("evidence_refs") or []:
                text = clean_text(ref, limit=180)
                if text.startswith("cron:"):
                    job_id = text.removeprefix("cron:")
                    break
        job_id = job_id or "unknown"
        counts[job_id] = counts.get(job_id, 0) + 1
    return dict(sorted(counts.items()))



def _event_type_for_kind(kind: str) -> str:
    if kind in VALID_EVENT_TYPES:
        return kind
    if kind in {"route", "decision"}:
        return "decision_signal"
    if kind == "uncertainty":
        return "uncertainty_signal"
    if kind == "observation":
        return "mind_signal"
    return "mind_signal"


def _normalize_choice(value: Any, allowed: set[str], *, default: str) -> str:
    text = clean_text(value, limit=80).lower().replace(" ", "_")
    return text if text in allowed else default


def _validate_required_quality_fields(**fields: Any) -> None:
    missing = [field for field in REQUIRED_QUALITY_FIELDS if not clean_text(fields.get(field))]
    if missing:
        raise ValueError(f"missing required quality fields: {', '.join(missing)}")


def _normalize_confidence_label(value: Any, confidence: float | None) -> str:
    text = clean_text(value, limit=20).lower()
    if text in VALID_CONFIDENCE_LABELS:
        return text
    if confidence is None:
        return "medium"
    if confidence >= 0.75:
        return "high"
    if confidence <= 0.4:
        return "low"
    return "medium"


def _normalize_category(value: Any, *, source: str, kind: str, priority: str, event_type: str) -> str:
    explicit = clean_text(value, limit=40).lower().replace(" ", "_")
    if explicit in VALID_CATEGORIES:
        return explicit
    if event_type in {"revenue_signal", "opportunity_signal"}:
        return "revenue"
    if event_type == "uncertainty_signal":
        return "uncertainty"
    if event_type == "decision_signal":
        return "decision"
    if event_type in {"self_improvement_signal", "policy_candidate"}:
        return "self_improvement"
    if event_type in {"approval_boundary", "risk_signal"}:
        return "decision"
    if event_type.startswith("cron_"):
        return "cron"
    if event_type == "kanban_motion":
        return "kanban"
    for candidate in (kind, priority, source):
        text = clean_text(candidate, limit=64).lower().replace(" ", "_")
        if text in VALID_CATEGORIES:
            return text
    return "mind"

def _line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _coerce_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return number


def _sanitize_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = clean_text(key, limit=80)
            if _is_sensitive_key(key_text):
                clean[key_text] = "[REDACTED]"
            else:
                clean[key_text] = _sanitize_metadata(item, depth=depth + 1)
        return clean
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_metadata(item, depth=depth + 1) for item in list(value)[:50]]
    if isinstance(value, _ALLOWED_SCALAR):
        if isinstance(value, str) and _looks_sensitive(value):
            return "[REDACTED]"
        return value
    return clean_text(repr(value), limit=160)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS) or "bearer " in lowered or "sk-" in value
