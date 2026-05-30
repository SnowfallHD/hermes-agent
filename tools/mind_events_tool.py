"""Tool for explicitly recording safe Mind/Event feed signals.

This exposes operational telemetry only: observations, decisions, uncertainties,
risk/approval boundaries, revenue signals, and self-improvement candidates. It
must not be used to record raw hidden chain-of-thought.
"""

from __future__ import annotations

import json
from typing import Any

from hermes_cli import mind_events
from tools.registry import registry, tool_error


MIND_EVENT_SCHEMA = {
    "name": "record_mind_event",
    "description": (
        "Record one explicit safe operational thought-quality event in the "
        "Thoughts/Mind feed. Use for meaningful non-trivial observations, "
        "decisions, uncertainties, approval/risk boundaries, revenue signals, "
        "policy candidates, user-context updates, or self-improvement signals. "
        "Do NOT record raw chain-of-thought; provide a concise observable summary "
        "and why it matters."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": sorted(mind_events.VALID_CATEGORIES),
                "description": "Top-level Thoughts filter category.",
            },
            "event_type": {
                "type": "string",
                "enum": sorted(mind_events.VALID_EVENT_TYPES),
                "description": "Typed signal surfaced in the feed.",
            },
            "summary": {
                "type": "string",
                "description": "One concise sentence describing the explicit observation/decision/signal.",
            },
            "why_it_matters": {
                "type": "string",
                "description": "One concise sentence explaining relevance to Coop, Kryden, Hermes capability, the board, or $50k MRR.",
            },
            "confidence": {
                "type": "string",
                "enum": sorted(mind_events.VALID_CONFIDENCE_LABELS),
                "description": "Confidence label: low, medium, or high.",
            },
            "urgency": {
                "type": "string",
                "enum": sorted(mind_events.VALID_URGENCY),
                "description": "Attention routing level.",
            },
            "autonomy_quality": {
                "type": "string",
                "enum": sorted(mind_events.VALID_AUTONOMY_QUALITY),
                "description": "Quality judgment for Hermes' autonomous behavior.",
            },
            "next_best_action": {
                "type": "string",
                "enum": sorted(mind_events.VALID_NEXT_BEST_ACTIONS),
                "description": "Compressed next action recommendation.",
            },
            "evidence_refs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional compact evidence refs like task IDs, cron IDs, session IDs, URLs, or artifact paths.",
            },
            "related_task_id": {"type": "string"},
            "related_session_id": {"type": "string"},
            "source": {
                "type": "string",
                "description": "Optional emitter name; defaults to agent.",
            },
        },
        "required": [
            "category",
            "event_type",
            "summary",
            "why_it_matters",
            "confidence",
            "urgency",
            "autonomy_quality",
            "next_best_action",
        ],
    },
}


def record_mind_event_tool(
    *,
    category: str,
    event_type: str,
    summary: str,
    why_it_matters: str,
    confidence: str,
    urgency: str,
    autonomy_quality: str,
    next_best_action: str,
    evidence_refs: list[str] | None = None,
    related_task_id: str | None = None,
    related_session_id: str | None = None,
    source: str = "agent",
) -> str:
    try:
        event = mind_events.append_event(
            source=source or "agent",
            kind=event_type,
            event_type=event_type,
            category=category,
            summary=summary,
            rationale="Agent-emitted explicit operational telemetry, not raw chain-of-thought.",
            why_it_matters=why_it_matters,
            evidence_refs=evidence_refs or [],
            related_task_id=related_task_id,
            related_session_id=related_session_id,
            priority=category,
            confidence_label=confidence,
            urgency=urgency,
            autonomy_quality=autonomy_quality,
            next_best_action=next_best_action,
        )
    except Exception as exc:  # pragma: no cover - defensive tool boundary
        return tool_error(str(exc), tool="record_mind_event")
    return json.dumps({"success": True, "event": event}, ensure_ascii=False)


def check_requirements() -> bool:
    return True


registry.register(
    name="record_mind_event",
    toolset="mind_events",
    schema=MIND_EVENT_SCHEMA,
    handler=lambda args, **kw: record_mind_event_tool(
        category=args.get("category"),
        event_type=args.get("event_type"),
        summary=args.get("summary", ""),
        why_it_matters=args.get("why_it_matters"),
        confidence=args.get("confidence"),
        urgency=args.get("urgency"),
        autonomy_quality=args.get("autonomy_quality"),
        next_best_action=args.get("next_best_action"),
        evidence_refs=args.get("evidence_refs"),
        related_task_id=args.get("related_task_id"),
        related_session_id=args.get("related_session_id"),
        source=args.get("source", "agent"),
    ),
    check_fn=check_requirements,
    emoji="🧠",
)
