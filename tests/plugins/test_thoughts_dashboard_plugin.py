"""Tests for the Thoughts dashboard plugin backend."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_state import SessionDB
from hermes_cli import kanban_db as kb
from hermes_cli import mind_events


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "thoughts" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_thoughts_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_router():
    return _load_plugin_module().router


def test_action_template_compressor_aggregates_low_info_entries_but_preserves_high_signal():
    mod = _load_plugin_module()
    entries = [
        {
            "id": f"kanban:{i}",
            "event_seq": i,
            "created_at": 1000 + i,
            "source": "kanban",
            "kind": "completed",
            "event_type": "kanban_motion",
            "category": "kanban",
            "summary": f"worker finished task t_{i:08x}",
            "thought": f"worker finished task t_{i:08x}",
            "evidence_refs": [f"kanban-event:{i}"],
            "next_best_action": "watch",
            "autonomy_quality": "good_autonomous_action",
        }
        for i in range(4)
    ]
    entries.extend([
        {
            "id": "kanban:review",
            "event_seq": 20,
            "created_at": 2000,
            "source": "kanban",
            "kind": "blocked",
            "event_type": "decision_signal",
            "category": "decision",
            "summary": "review-required: local code change needs eyes",
            "thought": "review-required: local code change needs eyes",
            "evidence_refs": ["kanban-event:20"],
            "next_best_action": "escalate",
        },
        {
            "id": "mind:revenue",
            "event_seq": 21,
            "created_at": 2001,
            "source": "cron",
            "kind": "revenue_signal",
            "event_type": "revenue_signal",
            "category": "revenue",
            "summary": "Revenue signal for $50k MRR.",
            "thought": "Revenue signal for $50k MRR.",
            "evidence_refs": ["market:1"],
            "next_best_action": "create_task",
        },
    ])

    compressed, meta = mod._compress_repeated_low_information_actions(entries, min_count=3)

    assert len(meta) == 1
    aggregate = meta[0]
    assert aggregate["counts"]["total"] == 4
    assert aggregate["window"]["seconds"] == 3
    assert aggregate["representative_evidence_refs"] == ["kanban-event:0", "kanban-event:1", "kanban-event:2", "kanban-event:3"]
    assert aggregate["why_it_matters"]
    assert aggregate["autonomy_quality"] == "compressed_low_information_repeats"
    assert aggregate["next_best_action"] == "watch"
    assert not any(entry.get("id") == "kanban:0" for entry in compressed)
    assert any(entry.get("id") == "kanban:review" for entry in compressed)
    assert any(entry.get("id") == "mind:revenue" for entry in compressed)


def test_worker_liveness_compressor_drops_repeated_pid_not_alive_templates_but_keeps_real_signals():
    mod = _load_plugin_module()
    pid_rows = [
        {
            "id": f"kanban:{i}",
            "event_seq": i,
            "created_at": 1000 + i,
            "source": "kanban",
            "kind": "crashed",
            "event_type": "risk_signal",
            "category": "decision",
            "summary": f"Task {i} failed during execution. Detail: pid {3000 + i} not alive",
            "thought": f"Task {i} failed during execution. Detail: pid {3000 + i} not alive",
            "why_it_matters": "Execution failures require recovery judgment before more autonomous churn accumulates.",
            "evidence_refs": [f"kanban-event:{i}", f"kanban-task:t_pid{i}"],
            "task_id": f"t_pid{i}",
            "next_best_action": "retry",
            "autonomy_quality": "failed_recovery_needed",
        }
        for i in range(5)
    ]
    preserved = [
        {
            "id": "kanban:review",
            "event_seq": 20,
            "created_at": 2000,
            "source": "kanban",
            "kind": "blocked",
            "event_type": "decision_signal",
            "category": "decision",
            "summary": "review-required: local branch needs reviewer eyes",
            "thought": "review-required: local branch needs reviewer eyes",
            "evidence_refs": ["kanban-event:20"],
            "next_best_action": "escalate",
        },
        {
            "id": "kanban:traceback",
            "event_seq": 21,
            "created_at": 2001,
            "source": "kanban",
            "kind": "crashed",
            "event_type": "risk_signal",
            "category": "decision",
            "summary": "Task failed during execution. Detail: Traceback: boom",
            "thought": "Task failed during execution. Detail: Traceback: boom",
            "evidence_refs": ["kanban-event:21"],
            "next_best_action": "retry",
        },
        {
            "id": "mind:revenue",
            "event_seq": 22,
            "created_at": 2002,
            "source": "cron",
            "kind": "revenue_signal",
            "event_type": "revenue_signal",
            "category": "revenue",
            "summary": "Revenue signal for $50k MRR.",
            "thought": "Revenue signal for $50k MRR.",
            "evidence_refs": ["market:1"],
            "next_best_action": "create_task",
        },
    ]
    before_count = sum("not alive" in entry["summary"] for entry in pid_rows + preserved)

    compressed, meta = mod._compress_repeated_worker_liveness_failures(pid_rows + preserved, min_count=3)
    after_count = sum("not alive" in entry["summary"] for entry in compressed)

    assert before_count == 5
    assert after_count == 1
    assert len(meta) == 1
    aggregate = meta[0]
    assert aggregate["event_type"] == "risk_signal"
    assert aggregate["urgency"] == "needs_review"
    assert aggregate["next_best_action"] == "retry"
    assert aggregate["counts"] == {"total": 5, "template": "pid_not_alive_worker_outcome", "tasks": 5}
    assert aggregate["tasks"] == ["t_pid0", "t_pid1", "t_pid2", "t_pid3", "t_pid4"]
    assert not any(entry.get("id") == "kanban:0" for entry in compressed)
    assert any(entry.get("id") == "kanban:review" for entry in compressed)
    assert any(entry.get("id") == "kanban:traceback" for entry in compressed)
    assert any(entry.get("id") == "mind:revenue" for entry in compressed)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/thoughts")
    return TestClient(app)


def _append_profile_event(home: Path, *, summary: str, profile: str | None = None) -> None:
    ledger = home / "state" / "mind" / "events.jsonl"
    mind_events.append_event(
        source="agent",
        kind="decision_signal",
        event_type="decision_signal",
        category="decision",
        summary=summary,
        rationale="Explicit event summary, not hidden chain-of-thought.",
        why_it_matters="This lets Thoughts orient across configured Hermes profile ledgers without exposing raw reasoning.",
        evidence_refs=([f"profile:{profile}"] if profile else []),
        confidence_label="high",
        urgency="silent",
        autonomy_quality="good_autonomous_action",
        next_best_action="watch",
        path=ledger,
    )


def _append_tool_call(db: SessionDB, session_id: str, name: str, args: dict, *, result: str = "{}") -> int:
    message_id = db.append_message(
        session_id,
        "assistant",
        content="",
        tool_calls=[{"id": f"call-{name}", "function": {"name": name, "arguments": json.dumps(args)}}],
    )
    db.append_message(session_id, "tool", content=result, tool_name=name, tool_call_id=f"call-{name}")
    return message_id


def test_thoughts_endpoint_returns_human_operational_one_liners(client):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Review local worktree change",
            assignee="reviewer",
            created_by="test",
            initial_status="running",
        )
        kb._append_event(
            conn,
            task_id,
            "blocked",
            {"reason": "review-required: local branch needs eyes before merge"},
        )
    finally:
        conn.close()

    response = client.get("/api/plugins/thoughts/thoughts")
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["raw_chain_of_thought"] is False
    thoughts = [entry["thought"] for entry in data["entries"]]
    assert any("queued" in thought and "trackable unit of work" in thought for thought in thoughts)
    assert any("reviewer eyes" in thought and "not human approval" in thought for thought in thoughts)
    assert all("\n" not in thought for thought in thoughts)
    assert all(len(thought) <= 220 for thought in thoughts)


def test_thoughts_endpoint_summarizes_approval_gate_without_raw_payload_dump(client):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Run Gmail OAuth dev test",
            assignee="ops",
            created_by="test",
            initial_status="blocked",
        )
        payload = {
            "reason": "approval-needed: needs Google Cloud/OAuth credential setup",
            "secret_like_noise": "sk-test-do-not-dump-this-payload-verbatim",
        }
        kb._append_event(conn, task_id, "blocked", payload)
    finally:
        conn.close()

    response = client.get("/api/plugins/thoughts/thoughts")
    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    approval_thoughts = [entry["thought"] for entry in entries if entry["kind"] == "blocked"]

    assert any("real approval gate" in thought for thought in approval_thoughts)
    assert all("sk-tes...atim" not in thought for thought in approval_thoughts)
    assert all("secret_like_noise" not in thought for thought in approval_thoughts)


def test_thoughts_endpoint_merges_mind_events_with_kanban(client):
    mind_events.append_event(
        source="cron",
        kind="revenue_signal",
        event_type="revenue_signal",
        category="revenue",
        summary="Daily market scout found repeated Gmail cleanup pain; this should feed the $50k MRR opportunity loop.",
        rationale="Explicit event summary, not hidden chain-of-thought.",
        why_it_matters="This should feed the $50k MRR opportunity loop as a revenue-relevant signal.",
        evidence_refs=["cron:market-scout"],
        priority="revenue",
        confidence=0.74,
        confidence_label="medium",
        urgency="daily_brief",
        autonomy_quality="good_autonomous_action",
        next_best_action="create_task",
    )

    response = client.get("/api/plugins/thoughts/thoughts")
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["raw_chain_of_thought"] is False
    assert "Mind Event ledger plus Kanban" in data["note"] or "Mind Event ledger" in data["note"]
    assert data["mind_ledger"]["path"].endswith("state/mind/events.jsonl")
    assert data["mind_ledger"]["scope"] == "active_hermes_home_profile"
    entries = data["entries"]
    assert any(entry["source"] == "cron" for entry in entries)
    assert any(entry["kind"] == "revenue_signal" for entry in entries)
    revenue = next(entry for entry in entries if entry["kind"] == "revenue_signal")
    assert revenue["category"] == "revenue"
    assert revenue["event_type"] == "revenue_signal"
    assert revenue["why_it_matters"]
    assert revenue["confidence_label"] in {"low", "medium", "high"}
    assert revenue["urgency"] in {"silent", "daily_brief", "needs_review", "immediate"}
    assert revenue["autonomy_quality"]
    assert revenue["next_best_action"]
    assert any("$50k MRR" in entry["thought"] for entry in entries)
    assert all(entry.get("raw_chain_of_thought") is False for entry in entries)


def test_thoughts_endpoint_defaults_to_active_profile_mind_ledger(client, kanban_home):
    _append_profile_event(kanban_home, summary="Active profile decision should stay visible by default.", profile="default")
    other_home = kanban_home / "profiles" / "planner"
    _append_profile_event(other_home, summary="Planner profile decision should require explicit multi-profile mode.", profile="planner")

    response = client.get("/api/plugins/thoughts/thoughts?limit=20")
    assert response.status_code == 200, response.text
    data = response.json()
    thoughts = [entry["thought"] for entry in data["entries"]]

    assert data["mind_ledger"]["scope"] == "active_hermes_home_profile"
    assert any("Active profile decision" in thought for thought in thoughts)
    assert all("Planner profile decision" not in thought for thought in thoughts)


def test_thoughts_endpoint_explicitly_aggregates_profile_labeled_mind_ledgers(client, kanban_home):
    _append_profile_event(kanban_home, summary="Default ledger event is included in aggregate mode.", profile="default")
    planner_home = kanban_home / "profiles" / "planner"
    _append_profile_event(planner_home, summary="Planner ledger event is included in aggregate mode.", profile="planner")

    response = client.get("/api/plugins/thoughts/thoughts?profiles=all&limit=20")
    assert response.status_code == 200, response.text
    data = response.json()
    entries = data["entries"]

    assert data["mind_ledger"]["scope"] == "configured_profiles"
    assert {ledger["profile"] for ledger in data["mind_ledger"]["ledgers"]} >= {"default", "planner"}
    assert any(entry["profile"] == "default" and "Default ledger event" in entry["thought"] for entry in entries)
    assert any(entry["profile"] == "planner" and "Planner ledger event" in entry["thought"] for entry in entries)
    assert all("profile:" in " ".join(entry.get("evidence_refs", [])) for entry in entries if entry["source"] == "agent")
    assert all(entry.get("raw_chain_of_thought") is False for entry in entries)


def test_sparse_session_action_reducer_filters_noise_and_redacts_raw_tool_data(client, kanban_home):
    db = SessionDB(db_path=kanban_home / "state.db")
    db.create_session("sess-actions", "cli")
    _append_tool_call(db, "sess-actions", "read_file", {"path": "/tmp/boring.txt"}, result="full raw file contents")
    patch_msg = _append_tool_call(
        db,
        "sess-actions",
        "patch",
        {"path": "/repo/app.py", "old_string": "sk-live-secret", "new_string": "safe-value"},
        result="diff output with sk-live-secret and file contents",
    )
    _append_tool_call(
        db,
        "sess-actions",
        "terminal",
        {"command": "pytest tests/test_app.py -q --token sk-live-secret"},
        result="FAILED tests/test_app.py::test_x traceback with sk-live-secret",
    )

    response = client.get("/api/plugins/thoughts/thoughts?session_actions=true&limit=50")
    assert response.status_code == 200, response.text
    entries = [entry for entry in response.json()["entries"] if entry["source"] == "session_action_reducer"]

    assert entries
    assert not any("read_file" in entry["thought"] for entry in entries)
    assert any(entry["kind"] == "code_edit_test_cycle" for entry in entries)
    entry_blob = json.dumps(entries, sort_keys=True)
    assert "sk-live" not in entry_blob
    assert "old_string" not in entry_blob
    assert "new_string" not in entry_blob
    assert "full raw file contents" not in entry_blob
    assert "traceback" not in entry_blob.lower()
    assert f"message:{patch_msg}" in entry_blob
    assert "session:sess-actions" in entry_blob
    assert "profile:default" in entry_blob
    assert all(entry.get("raw_chain_of_thought") is False for entry in entries)


def test_sparse_session_action_reducer_keeps_consequential_non_edit_actions(client, kanban_home):
    db = SessionDB(db_path=kanban_home / "state.db")
    db.create_session("sess-material", "cli")
    _append_tool_call(db, "sess-material", "delegate_task", {"goal": "review"}, result="child summary")
    _append_tool_call(db, "sess-material", "cronjob", {"action": "create", "prompt": "daily"}, result='{"job_id":"j1"}')
    _append_tool_call(db, "sess-material", "send_message", {"target": "telegram", "message": "do not leak body"}, result="delivered")
    _append_tool_call(db, "sess-material", "memory", {"action": "add", "content": "private preference"}, result="saved")
    _append_tool_call(db, "sess-material", "skill_manage", {"action": "patch", "old_string": "secret"}, result="patched")
    _append_tool_call(db, "sess-material", "process", {"action": "poll"}, result="exit_code 1: background job failed")

    response = client.get("/api/plugins/thoughts/thoughts?session_actions=true&limit=50")
    assert response.status_code == 200, response.text
    entries = [entry for entry in response.json()["entries"] if entry["source"] == "session_action_reducer"]
    kinds = {entry["kind"] for entry in entries}
    blob = json.dumps(entries, sort_keys=True)

    assert {"delegation", "cron_change", "message_delivery_attempt", "memory_change", "skill_change", "terminal_error"} <= kinds
    assert "do not leak body" not in blob
    assert "private preference" not in blob
    assert "old_string" not in blob
    assert "background job failed" not in blob
    assert "session:sess-material" in blob
    assert all(entry.get("raw_chain_of_thought") is False for entry in entries)


def test_thoughts_endpoint_returns_all_top_level_mind_event_categories_with_quality_fields(client):
    category_cases = [
        ("mind", "mind_signal"),
        ("kanban", "kanban_motion"),
        ("cron", "cron_result"),
        ("revenue", "revenue_signal"),
        ("self_improvement", "self_improvement_signal"),
        ("uncertainty", "uncertainty_signal"),
        ("decision", "decision_signal"),
    ]
    for category, event_type in category_cases:
        mind_events.append_event(
            source="agent",
            kind=event_type,
            event_type=event_type,
            category=category,
            summary=f"API coverage event for {category} Thoughts filter.",
            rationale="Explicit event summary, not hidden chain-of-thought.",
            why_it_matters="This proves the Thoughts API returns thought-quality fields for every top-level category.",
            priority=category,
            confidence=0.88,
            confidence_label="high",
            urgency="daily_brief",
            autonomy_quality="good_autonomous_action",
            next_best_action="watch",
        )

    response = client.get("/api/plugins/thoughts/thoughts?limit=50")
    assert response.status_code == 200, response.text
    by_category = {entry["category"]: entry for entry in response.json()["entries"] if entry.get("source") == "agent"}

    assert set(by_category) >= {category for category, _ in category_cases}
    expected_actions = {
        "mind": "watch",
        "kanban": "watch",
        "cron": "watch",
        "revenue": "create_task",
        "self_improvement": "create_task",
        "uncertainty": "verify",
        "decision": "watch",
    }
    for category, event_type in category_cases:
        entry = by_category[category]
        assert entry["event_type"] == event_type
        assert entry["why_it_matters"]
        assert entry["confidence_label"] == "high"
        assert entry["confidence"] == 0.88
        assert entry["urgency"] == "daily_brief"
        assert entry["autonomy_quality"] == "good_autonomous_action"
        assert entry["next_best_action"] == expected_actions[category]
        assert entry["raw_chain_of_thought"] is False


def test_thoughts_endpoint_corrects_claim_contract_mismatches_as_emission_gaps(client, kanban_home):
    ledger = kanban_home / "state" / "mind" / "events.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(
        json.dumps(
            {
                "created_at": 1780200000.0,
                "source": "auditor",
                "kind": "synthesis",
                "event_type": "revenue_signal",
                "category": "cron",
                "summary": "Synthesis claims a revenue signal but was emitted with the wrong category.",
                "why_it_matters": "Contract validation should separate producer labeling bugs from real cognition quality.",
                "evidence_refs": ["audit:claim-mismatch"],
                "confidence_label": "high",
                "urgency": "daily_brief",
                "autonomy_quality": "good_autonomous_action",
                "next_best_action": "create_task",
                "metadata": {"created_task_id": "t_followup"},
            },
            sort_keys=True,
        )
        + "\n"
    )

    response = client.get("/api/plugins/thoughts/thoughts?limit=20")
    assert response.status_code == 200, response.text
    entry = next(entry for entry in response.json()["entries"] if entry["source"] == "auditor")

    assert entry["event_type"] == "revenue_signal"
    assert entry["category"] == "revenue"
    assert entry["cognition_validation"]["gap_type"] == "output_emission_gap"
    assert entry["cognition_validation"]["original_category"] == "cron"
    assert "category_contract_mismatch" in entry["cognition_validation"]["findings"]


def test_thoughts_endpoint_adds_validation_evidence_and_followthrough_status(client):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Review evidence refs",
            assignee="ops",
            created_by="test",
            initial_status="running",
        )
        kb._append_event(conn, task_id, "completed", {"summary": "done"})
    finally:
        conn.close()

    response = client.get("/api/plugins/thoughts/thoughts?limit=20")
    assert response.status_code == 200, response.text
    entries = response.json()["entries"]

    kanban_entry = next(entry for entry in entries if entry.get("task_id") == task_id and entry["kind"] == "completed")
    assert f"kanban-task:{task_id}" in kanban_entry["evidence_refs"]
    assert any(ref.startswith("kanban-event:") for ref in kanban_entry["evidence_refs"])
    assert kanban_entry["cognition_validation"]["evidence_status"] == "present"
    assert kanban_entry["cognition_validation"]["claim_contract"] == "valid"

    mind_events.append_event(
        source="agent",
        kind="uncertainty_signal",
        event_type="uncertainty_signal",
        category="uncertainty",
        summary="Need verify later evidence before trusting this route.",
        rationale="Explicit event summary, not hidden chain-of-thought.",
        why_it_matters="The validation layer should expose when verification is requested but not yet linked.",
        evidence_refs=["session:uncertain"],
        confidence_label="high",
        urgency="needs_review",
        autonomy_quality="unclear",
        next_best_action="verify",
    )
    response = client.get("/api/plugins/thoughts/thoughts?limit=20")
    assert response.status_code == 200, response.text
    uncertain = next(entry for entry in response.json()["entries"] if entry["source"] == "agent" and "Need verify" in entry["thought"])
    assert uncertain["cognition_validation"]["followthrough_status"] == "missing_followthrough_link"
    assert uncertain["cognition_validation"]["gap_type"] == "action_quality_gap"


def test_thoughts_endpoint_respects_board_param(client):
    kb.create_board("revenue-test", name="Revenue Test")
    default_conn = kb.connect()
    board_conn = kb.connect(board="revenue-test")
    try:
        kb.create_task(
            default_conn,
            title="Default board task should stay hidden",
            assignee="ops",
            created_by="test",
        )
        task_id = kb.create_task(
            board_conn,
            title="Revenue board task should appear",
            assignee="ops",
            created_by="test",
        )
    finally:
        default_conn.close()
        board_conn.close()

    response = client.get("/api/plugins/thoughts/thoughts?board=revenue-test")
    assert response.status_code == 200, response.text
    data = response.json()

    assert data["board"] == "revenue-test"
    assert any(entry["task_id"] == task_id for entry in data["entries"])
    assert all(entry.get("task_title") != "Default board task should stay hidden" for entry in data["entries"])


def test_thoughts_websocket_stays_live_with_mind_event_ids(client):
    mind_events.append_event(
        source="chat",
        kind="self_improvement_signal",
        event_type="self_improvement_signal",
        category="self_improvement",
        summary="A non-Kanban mind event should not break the live websocket cursor.",
        rationale="Explicit event summary, not hidden chain-of-thought.",
        why_it_matters="Websocket compatibility keeps Thoughts live updates useful outside Kanban-only events.",
        confidence_label="high",
        urgency="silent",
        autonomy_quality="good_autonomous_action",
        next_best_action="watch",
    )

    from hermes_cli import web_server

    token = getattr(web_server, "_SESSION_TOKEN", "test") or "test"
    with client.websocket_connect(f"/api/plugins/thoughts/events?token={token}&limit=10") as ws:
        frame = ws.receive_json()

    assert frame["raw_chain_of_thought"] is False
    assert any(entry["id"].startswith("mind:") for entry in frame["entries"])


def test_thoughts_dashboard_bundle_registers_plugin_page():
    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "thoughts" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text()

    assert "window.__HERMES_PLUGINS__.register(\"thoughts\", ThoughtsPage)" in js
    assert "not raw chain-of-thought" in js
    assert "Unified Mind/Event feed" in js
    assert "hermes-thought-filter--active" in js
    assert "role: \"radio\"" in js
    assert "entry.category === filter" in js
    assert "why_it_matters" in js


def test_churn_compression_emits_self_improvement_meta_event(client):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Review churny blocked routing",
            assignee="ops",
            created_by="test",
            initial_status="running",
        )
        for kind, payload in [
            ("blocked", {"reason": "review-required: needs eyes"}),
            ("unblocked", {"reason": "retry"}),
            ("claimed", {"run_id": 1}),
            ("crashed", {"error": "boom"}),
            ("reclaimed", {"reason": "stale"}),
            ("blocked", {"reason": "approval-needed: external"}),
        ]:
            kb._append_event(conn, task_id, kind, payload)
    finally:
        conn.close()

    response = client.get("/api/plugins/thoughts/thoughts?limit=200")
    assert response.status_code == 200, response.text
    entries = response.json()["entries"]
    churn = [entry for entry in entries if entry["id"].startswith("churn:")]

    assert churn
    assert churn[0]["category"] == "self_improvement"
    assert churn[0]["event_type"] == "policy_candidate"
    assert "split blocked_reason" in churn[0]["thought"]
    assert churn[0]["autonomy_quality"] == "failed_recovery_needed"


def test_thoughts_manifest_adds_left_nav_tab_after_kanban():
    repo_root = Path(__file__).resolve().parents[2]
    manifest = repo_root / "plugins" / "thoughts" / "dashboard" / "manifest.json"
    data = json.loads(manifest.read_text())

    assert data["label"] == "Thoughts"
    assert data["tab"]["path"] == "/thoughts"
    assert data["tab"]["position"] == "after:kanban"
