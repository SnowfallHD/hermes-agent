"""Tests for cron-to-Mind/Event classification."""

from __future__ import annotations

import json

from cron import scheduler
from hermes_cli import mind_events


def _job(**overrides):
    job = {
        "id": "synthesis-job",
        "name": "Daily Kryden commander synthesis",
        "prompt": "Review recent sessions and synthesize a commander orientation update.",
        "schedule_display": "0 17 * * *",
    }
    job.update(overrides)
    return job


def test_cron_success_emits_mind_signal_for_cross_time_synthesis(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    scheduler._append_cron_mind_event(
        _job(),
        (
            True,
            "",
            "Synthesized recent sessions, board state, and cron outputs into one orientation: ship the validation card next.",
            None,
        ),
    )

    events = mind_events.read_events(path=tmp_path / "state" / "mind" / "events.jsonl")
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "mind_signal"
    assert event["category"] == "mind"
    assert event["urgency"] == "daily_brief"
    assert event["next_best_action"] == "watch"
    assert event["metadata"]["signal_reason"] == "cross_time_synthesis"
    assert "cron:synthesis-job" in event["evidence_refs"]
    assert event["raw_chain_of_thought"] is False


def test_cron_success_keeps_plain_execution_status_as_cron_not_mind(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    scheduler._append_cron_mind_event(
        _job(name="Disk cleanup watchdog", prompt="Run cleanup script and report status."),
        (True, "", "Cleanup completed. Removed 2 temporary files.", None),
    )

    events = mind_events.read_events(path=tmp_path / "state" / "mind" / "events.jsonl")
    assert len(events) == 1
    assert events[0]["event_type"] == "cron_result"
    assert events[0]["category"] == "cron"
    assert events[0]["metadata"].get("signal_reason") != "cross_time_synthesis"


def test_mind_events_reader_still_compresses_repeated_raw_cron_status(tmp_path):
    path = tmp_path / "events.jsonl"
    rows = []
    for index in range(3):
        rows.append(
            {
                "created_at": 100.0 + index,
                "source": "cron",
                "kind": "cron_silent",
                "event_type": "cron_silent",
                "category": "cron",
                "summary": "Cron job “quiet-loop” ran silently; no user-facing followup was needed.",
                "why_it_matters": "Silent cron success confirms a background loop ran without needing Coop's attention.",
                "evidence_refs": ["cron:quiet-loop"],
                "priority": "cron",
                "confidence": 1.0,
                "confidence_label": "high",
                "urgency": "silent",
                "autonomy_quality": "good_autonomous_action",
                "next_best_action": "ignore",
                "metadata": {"job_id": "quiet-loop"},
                "raw_chain_of_thought": False,
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    events = mind_events.read_events(path=path)

    assert len(events) == 1
    assert events[0]["event_type"] == "cron_silent"
    assert events[0]["category"] == "cron"
    assert events[0]["metadata"]["compressed_count"] == 3
