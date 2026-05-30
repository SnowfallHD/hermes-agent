"""Tests for explicit Mind/Event emission tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import mind_events
from tools.mind_events_tool import record_mind_event_tool
from tools.registry import registry


def test_record_mind_event_tool_registers_and_writes_quality_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = json.loads(record_mind_event_tool(
        category="mind",
        event_type="mind_signal",
        summary="I connected recurring audit/review themes across Kryden product candidates.",
        why_it_matters="This helps Coop see the strategic pattern behind task selection, not just task motion.",
        confidence="medium",
        urgency="daily_brief",
        autonomy_quality="good_autonomous_action",
        next_best_action="watch",
        evidence_refs=["session:test"],
        source="test_agent",
    ))

    assert registry.get_entry("record_mind_event") is not None
    assert result["success"] is True
    stored = mind_events.read_events(limit=10)
    assert stored[-1]["category"] == "mind"
    assert stored[-1]["event_type"] == "mind_signal"
    assert stored[-1]["why_it_matters"].startswith("This helps Coop")
    assert stored[-1]["confidence_label"] == "medium"
    assert stored[-1]["raw_chain_of_thought"] is False


def test_record_mind_event_registry_handler_rejects_incomplete_quality_signal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    entry = registry.get_entry("record_mind_event")
    assert entry is not None

    result = json.loads(entry.handler({"summary": "Incomplete Thoughts event"}))

    assert "missing required quality fields" in result["error"]
    assert mind_events.read_events(limit=10) == []


@pytest.mark.parametrize(
    ("category", "event_type", "expected_next_best_action"),
    [
        ("mind", "mind_signal", "watch"),
        ("kanban", "kanban_motion", "watch"),
        ("cron", "cron_result", "watch"),
        ("revenue", "revenue_signal", "create_task"),
        ("self_improvement", "self_improvement_signal", "create_task"),
        ("uncertainty", "uncertainty_signal", "verify"),
        ("decision", "decision_signal", "watch"),
    ],
)
def test_record_mind_event_tool_can_emit_every_top_level_category(
    tmp_path, monkeypatch, category, event_type, expected_next_best_action
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = json.loads(record_mind_event_tool(
        category=category,
        event_type=event_type,
        summary=f"Test emitted {category} signal for Thoughts feed coverage.",
        why_it_matters="Coverage proves the explicit event tool can populate each top-level Thoughts filter.",
        confidence="high",
        urgency="daily_brief",
        autonomy_quality="good_autonomous_action",
        next_best_action="watch",
        evidence_refs=[f"category:{category}"],
        source="test_agent",
    ))

    assert result["success"] is True
    stored = mind_events.read_events(limit=10)
    event = stored[-1]
    assert event["category"] == category
    assert event["event_type"] == event_type
    assert event["why_it_matters"]
    assert event["confidence_label"] == "high"
    assert event["confidence"] is None
    assert event["urgency"] == "daily_brief"
    assert event["autonomy_quality"] == "good_autonomous_action"
    assert event["next_best_action"] == expected_next_best_action
    assert event["raw_chain_of_thought"] is False
