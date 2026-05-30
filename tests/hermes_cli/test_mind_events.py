"""Tests for profile-local Mind Event ledger quality contracts."""

from __future__ import annotations

import json

import pytest

from hermes_cli import mind_events


def test_append_event_rejects_missing_required_quality_fields(tmp_path):
    with pytest.raises(ValueError, match="missing required quality fields"):
        mind_events.append_event(
            source="test",
            kind="revenue_signal",
            summary="A producer tried to write a Thoughts event without the complete quality contract.",
            rationale="This should not be written because it would create dashboard quality gaps.",
            path=tmp_path / "events.jsonl",
        )

    assert not (tmp_path / "events.jsonl").exists()


def test_append_event_normalizes_complete_quality_fields(tmp_path):
    event = mind_events.append_event(
        source="test",
        kind="revenue_signal",
        event_type="Revenue Signal",
        category="Revenue",
        summary="Market scout found repeated billing-audit pain.",
        rationale="Explicit operational telemetry, not hidden chain-of-thought.",
        why_it_matters="This can feed the Kryden revenue opportunity loop.",
        priority="revenue",
        confidence=0.82,
        confidence_label="HIGH",
        urgency="Daily Brief",
        autonomy_quality="Good Autonomous Action",
        next_best_action="Create Task",
        path=tmp_path / "events.jsonl",
    )

    assert event["event_type"] == "revenue_signal"
    assert event["category"] == "revenue"
    assert event["why_it_matters"].startswith("This can feed")
    assert event["confidence_label"] == "high"
    assert event["urgency"] == "daily_brief"
    assert event["autonomy_quality"] == "good_autonomous_action"
    assert event["next_best_action"] == "create_task"

    raw = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8"))
    for field in mind_events.REQUIRED_QUALITY_FIELDS:
        assert raw[field]


def test_next_best_action_calibration_keeps_passive_noop_rows_passive(tmp_path):
    event = mind_events.append_event(
        source="cron",
        kind="cron_silent",
        event_type="cron_silent",
        category="cron",
        summary="Cron job “quiet-loop” ran silently; no user-facing followup was needed.",
        why_it_matters="Silent cron success confirms a background loop ran without needing Coop's attention.",
        confidence=1.0,
        confidence_label="high",
        urgency="silent",
        autonomy_quality="good_autonomous_action",
        next_best_action="ignore",
        path=tmp_path / "events.jsonl",
    )

    assert event["next_best_action"] == "ignore"


def test_next_best_action_calibration_upgrades_uncertainty_to_verify(tmp_path):
    event = mind_events.append_event(
        source="metacognitive_router",
        kind="uncertainty_signal",
        event_type="uncertainty_signal",
        category="uncertainty",
        summary="Action and delivery succeeded but missing_outcome_evidence remains.",
        why_it_matters="Hermes needs verification before trusting this outcome.",
        evidence_refs=["failure:missing_outcome_evidence"],
        confidence=1.0,
        confidence_label="high",
        urgency="daily_brief",
        autonomy_quality="unclear",
        next_best_action="watch",
        path=tmp_path / "events.jsonl",
    )

    assert event["next_best_action"] == "verify"


def test_next_best_action_calibration_upgrades_revenue_market_contact_to_draft(tmp_path):
    event = mind_events.append_event(
        source="market_scout",
        kind="revenue_signal",
        event_type="revenue_signal",
        category="revenue",
        summary="Buyer scout found a lead asking for billing-audit help; contact should be drafted first.",
        why_it_matters="A market-contact signal can advance Kryden revenue, but outreach needs review before sending.",
        confidence=0.9,
        confidence_label="high",
        urgency="needs_review",
        autonomy_quality="good_autonomous_action",
        next_best_action="watch",
        path=tmp_path / "events.jsonl",
    )

    assert event["next_best_action"] == "draft_for_review"


def test_next_best_action_calibration_respects_max_tasks_per_run_cap(tmp_path):
    event = mind_events.append_event(
        source="market_scout",
        kind="revenue_signal",
        event_type="revenue_signal",
        category="revenue",
        summary="Buyer scout found another strong revenue signal.",
        why_it_matters="This is useful, but the run has already emitted its task budget.",
        confidence=0.9,
        confidence_label="high",
        urgency="needs_review",
        autonomy_quality="good_autonomous_action",
        next_best_action="watch",
        metadata={"max_tasks_per_run": 1, "task_proposals_emitted": 1},
        path=tmp_path / "events.jsonl",
    )

    assert event["next_best_action"] == "watch"


def test_next_best_action_calibration_upgrades_approval_boundary_to_permission(tmp_path):
    event = mind_events.append_event(
        source="metacognitive_router",
        kind="approval_boundary",
        event_type="approval_boundary",
        category="decision",
        summary="External outreach requires approval before Hermes can continue.",
        why_it_matters="Approval-gated work should ask Coop instead of sitting as a passive watch item.",
        confidence=1.0,
        confidence_label="high",
        urgency="needs_review",
        autonomy_quality="blocked_correctly",
        next_best_action="watch",
        related_task_id="t_review_gate",
        path=tmp_path / "events.jsonl",
    )

    assert event["next_best_action"] == "request_permission"


def test_next_best_action_calibration_preserves_existing_followup_to_avoid_duplicates(tmp_path):
    event = mind_events.append_event(
        source="market_scout",
        kind="revenue_signal",
        event_type="revenue_signal",
        category="revenue",
        summary="Buyer scout found a repeated billing-audit opportunity.",
        why_it_matters="A follow-up task already exists, so this row should not create noisy duplicate work.",
        confidence=0.9,
        confidence_label="high",
        urgency="needs_review",
        autonomy_quality="good_autonomous_action",
        next_best_action="watch",
        metadata={"existing_task_id": "t_existing"},
        path=tmp_path / "events.jsonl",
    )

    assert event["next_best_action"] == "watch"


def test_read_events_calibrates_legacy_passive_high_signal_rows(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "created_at": 123.0,
                "source": "metacognitive_router",
                "kind": "approval_boundary",
                "event_type": "approval_boundary",
                "category": "decision",
                "summary": "External account change requires approval.",
                "why_it_matters": "The dashboard should surface permission needs as action, not passive watch.",
                "confidence": 1.0,
                "confidence_label": "high",
                "urgency": "needs_review",
                "autonomy_quality": "blocked_correctly",
                "next_best_action": "watch",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events = mind_events.read_events(path=path)

    assert events[0]["next_best_action"] == "request_permission"


def test_read_events_backfills_legacy_events_missing_quality_fields(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "created_at": 123.0,
                "source": "legacy",
                "kind": "revenue_signal",
                "summary": "Legacy event predates Thoughts quality fields.",
                "rationale": "Still useful historical context.",
                "priority": "revenue",
                "confidence": 0.8,
                "raw_chain_of_thought": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    events = mind_events.read_events(path=path)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "revenue_signal"
    assert event["category"] == "revenue"
    assert event["why_it_matters"] == "Still useful historical context."
    assert event["confidence_label"] == "high"
    assert event["urgency"] == "silent"
    assert event["autonomy_quality"] == "unclear"
    assert event["next_best_action"] == "watch"


def test_read_events_compresses_repeated_silent_cron_templates(tmp_path):
    path = tmp_path / "events.jsonl"
    rows = []
    for index, job_id in enumerate(("cron-a", "cron-b", "cron-c", "cron-a"), start=1):
        rows.append(
            {
                "created_at": 100.0 + index,
                "source": "cron",
                "kind": "cron_silent",
                "event_type": "cron_silent",
                "category": "cron",
                "summary": f"Cron job “{job_id}” ran silently; no user-facing followup was needed.",
                "rationale": "Cron completion emitted as explicit operational telemetry, not raw chain-of-thought.",
                "why_it_matters": "Silent cron success confirms a background loop ran without needing Coop's attention.",
                "evidence_refs": [f"cron:{job_id}"],
                "priority": "cron",
                "confidence": 1.0,
                "confidence_label": "high",
                "urgency": "silent",
                "autonomy_quality": "good_autonomous_action",
                "next_best_action": "ignore",
                "metadata": {"job_id": job_id},
                "raw_chain_of_thought": False,
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    events = mind_events.read_events(path=path)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "cron_silent"
    assert event["summary"] == "4 silent cron completions compressed from repeated status templates."
    assert event["why_it_matters"] == "Silent cron successes are healthy background-loop evidence, but repeated per-job status rows create feed churn without needing Coop's attention."
    assert event["evidence_refs"] == ["cron:cron-a", "cron:cron-b", "cron:cron-c", "mind-lines:1-4"]
    assert event["metadata"]["compressed_count"] == 4
    assert event["metadata"]["template"] == "cron_silent"
    assert event["metadata"]["first_created_at"] == 101.0
    assert event["metadata"]["last_created_at"] == 104.0
    assert event["metadata"]["job_counts"] == {"cron-a": 2, "cron-b": 1, "cron-c": 1}
    assert event["raw_chain_of_thought"] is False


def test_read_events_compresses_legacy_silent_cron_rows_missing_quality_fields(tmp_path):
    path = tmp_path / "events.jsonl"
    rows = []
    for index, job_id in enumerate(("legacy-a", "legacy-b", "legacy-a"), start=1):
        rows.append(
            {
                "created_at": 200.0 + index,
                "source": "cron",
                "kind": "cron_silent",
                "summary": f"Cron job “{job_id}” ran silently; no user-facing followup was needed.",
                "rationale": "Cron completion emitted as explicit operational telemetry, not raw chain-of-thought.",
                "evidence_refs": [f"cron:{job_id}"],
                "priority": "normal",
                "confidence": 1.0,
                "metadata": {"job_id": job_id},
                "raw_chain_of_thought": False,
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    events = mind_events.read_events(path=path)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "cron_silent"
    assert event["summary"] == "3 silent cron completions compressed from repeated status templates."
    assert event["evidence_refs"] == ["cron:legacy-a", "cron:legacy-b", "mind-lines:1-3"]
    assert event["metadata"]["compressed_count"] == 3
    assert event["metadata"]["job_counts"] == {"legacy-a": 2, "legacy-b": 1}
    assert event["next_best_action"] == "ignore"


def test_read_events_keeps_high_signal_cron_and_revenue_events_visible(tmp_path):
    path = tmp_path / "events.jsonl"
    silent = {
        "created_at": 100.0,
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
    rows: list[dict[str, object]] = [dict(silent, created_at=101.0), dict(silent, created_at=102.0), dict(silent, created_at=103.0)]
    rows.extend(
        [
            {
                "created_at": 104.0,
                "source": "cron",
                "kind": "cron_failure",
                "event_type": "risk_signal",
                "category": "decision",
                "summary": "Cron job “buyer-scout” failed; I should inspect the failure before trusting this loop.",
                "why_it_matters": "A broken scheduled loop can create blind spots in Hermes monitoring or revenue/autonomy routines.",
                "evidence_refs": ["cron:buyer-scout"],
                "priority": "self_improvement",
                "confidence": 1.0,
                "confidence_label": "high",
                "urgency": "needs_review",
                "autonomy_quality": "failed_recovery_needed",
                "next_best_action": "retry",
                "raw_chain_of_thought": False,
            },
            {
                "created_at": 105.0,
                "source": "cron",
                "kind": "revenue_signal",
                "event_type": "revenue_signal",
                "category": "revenue",
                "summary": "Cron job “buyer-scout” found a paid customer signal.",
                "why_it_matters": "This can feed the Kryden revenue opportunity loop.",
                "evidence_refs": ["cron:buyer-scout"],
                "priority": "revenue",
                "confidence": 0.9,
                "confidence_label": "high",
                "urgency": "daily_brief",
                "autonomy_quality": "good_autonomous_action",
                "next_best_action": "create_task",
                "raw_chain_of_thought": False,
            },
        ]
    )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    events = mind_events.read_events(path=path)

    assert [event["event_type"] for event in events] == ["cron_silent", "risk_signal", "revenue_signal"]
    assert events[0]["metadata"]["compressed_count"] == 3
    assert events[1]["summary"].startswith("Cron job “buyer-scout” failed")
    assert events[1]["urgency"] == "needs_review"
    assert events[2]["category"] == "revenue"
    assert events[2]["next_best_action"] == "create_task"
