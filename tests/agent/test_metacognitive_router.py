import json
from dataclasses import dataclass
from pathlib import Path

from hermes_cli.config import DEFAULT_CONFIG

from agent.metacognitive_router import (
    Attempt,
    BehaviorActionCandidate,
    Evidence,
    FailureMode,
    Intent,
    IntentRisk,
    RouteAction,
    RouteDecision,
    append_decision_jsonl,
    default_state_path,
    evaluate_route,
    load_feature_config,
    replay_jsonl,
    route_behavior_change,
    status_summary,
    redact_payload,
)


def test_feature_config_defaults_to_disabled_and_profile_safe_state_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = load_feature_config({})

    assert config.enabled is False
    assert config.dry_run is True
    assert config.flag_name == "metacognitive_router.enabled"
    assert DEFAULT_CONFIG["metacognitive_router"] == {
        "enabled": False,
        "dry_run": True,
        "record_paths": {"tool_results": False},
        "behavior_routing": {
            "enabled": False,
            "allow_internal_kanban": False,
            "allow_internal_execution_recommendations": False,
            "approval_assignee": "builder",
            "internal_board": None,
            "internal_assignee": "builder",
        },
    }
    assert default_state_path() == tmp_path / "state" / "metacognitive_router" / "events.jsonl"


def test_feature_config_parses_only_explicit_booleans():
    config = load_feature_config({"metacognitive_router": {"enabled": " YES ", "dry_run": "off"}})

    assert config.enabled is True
    assert config.dry_run is False

    config = load_feature_config({"metacognitive_router": {"enabled": "0", "dry_run": "1"}})

    assert config.enabled is False
    assert config.dry_run is True


def test_feature_config_malformed_values_use_safe_defaults():
    for value in ("false-but-truthy", "", "maybe", 1, 0, None, [], {}):
        config = load_feature_config({"metacognitive_router": {"enabled": value, "dry_run": value}})

        assert config.enabled is False
        assert config.dry_run is True

    config = load_feature_config({"metacognitive_router": {"enabled": "false", "dry_run": "no"}})

    assert config.enabled is False
    assert config.dry_run is False


def test_feature_config_non_dict_root_uses_safe_defaults():
    for root_config in ("bad", [], 1):
        config = load_feature_config(root_config)

        assert config.enabled is False
        assert config.dry_run is True


def test_feature_config_accepts_actual_bool_values():
    config = load_feature_config({"metacognitive_router": {"enabled": True, "dry_run": False}})

    assert config.enabled is True
    assert config.dry_run is False


def test_feature_config_tool_result_recording_is_explicit_opt_in():
    config = load_feature_config({})

    assert config.record_tool_results is False

    config = load_feature_config(
        {
            "metacognitive_router": {
                "enabled": True,
                "dry_run": True,
                "record_paths": {"tool_results": "yes"},
            }
        }
    )

    assert config.enabled is True
    assert config.dry_run is True
    assert config.record_tool_results is True


def test_feature_config_tool_result_recording_malformed_values_use_safe_default():
    values = ("maybe", "", 1, 0, None, [], {}, {"tool_results": "maybe"})

    for value in values:
        config = load_feature_config({"metacognitive_router": {"record_paths": value}})

        assert config.record_tool_results is False


def test_missing_outcome_evidence_routes_to_passive_status_check():
    decision = evaluate_route(
        Intent(kind="send_gateway_message", risk=IntentRisk.INTERNAL),
        Attempt(action_success=True, delivery_success=True, outcome_success=None),
        Evidence(outcome_present=False, delivery_confirmed=True),
    )

    assert decision == RouteDecision(
        action=RouteAction.PASSIVE_STATUS_CHECK,
        failure_modes=(FailureMode.MISSING_OUTCOME_EVIDENCE,),
        passive_only=True,
        external_allowed=False,
        reason="action and delivery succeeded but outcome evidence is missing",
    )


def test_gateway_fallback_is_passive_dry_run_only():
    decision = evaluate_route(
        Intent(kind="notify_user", risk=IntentRisk.INTERNAL),
        Attempt(action_success=True, delivery_success=False, channel="slack"),
        Evidence(delivery_confirmed=False, fallback_available=True),
    )

    assert decision.action == RouteAction.PASSIVE_GATEWAY_FALLBACK
    assert FailureMode.GATEWAY_DELIVERY_GAP in decision.failure_modes
    assert decision.passive_only is True
    assert decision.external_allowed is False


def test_approval_block_hard_gates_external_or_core_changes():
    decision = evaluate_route(
        Intent(kind="modify_core_runtime", risk=IntentRisk.CORE_RUNTIME, requires_approval=True),
        Attempt(action_success=None, delivery_success=None, outcome_success=None),
        Evidence(approval_present=False),
    )

    assert decision.action == RouteAction.BLOCK_FOR_APPROVAL
    assert decision.failure_modes == (FailureMode.APPROVAL_REQUIRED,)
    assert decision.external_allowed is False


def test_spam_budget_suppresses_additional_notifications():
    decision = evaluate_route(
        Intent(kind="follow_up", risk=IntentRisk.INTERNAL, max_notifications=2),
        Attempt(action_success=False, delivery_success=False, sent_notifications=2),
        Evidence(delivery_confirmed=False, fallback_available=True),
    )

    assert decision.action == RouteAction.SUPPRESS_SPAM
    assert FailureMode.SPAM_BUDGET_EXHAUSTED in decision.failure_modes
    assert decision.external_allowed is False


def test_worker_stall_routes_to_passive_kanban_status_check():
    decision = evaluate_route(
        Intent(kind="kanban_worker", risk=IntentRisk.INTERNAL),
        Attempt(action_success=None, delivery_success=None, outcome_success=None, status="running"),
        Evidence(last_heartbeat_age_seconds=7200),
    )

    assert decision.action == RouteAction.PASSIVE_KANBAN_STATUS_CHECK
    assert FailureMode.WORKER_STALL in decision.failure_modes


def test_quiet_cron_no_change_is_noop_success():
    decision = evaluate_route(
        Intent(kind="cron_watchdog", risk=IntentRisk.INTERNAL),
        Attempt(action_success=True, delivery_success=None, outcome_success=True, status="completed"),
        Evidence(no_change=True, outcome_present=True),
    )

    assert decision.action == RouteAction.NOOP_SUCCESS
    assert decision.failure_modes == ()
    assert decision.should_record is True


def test_kanban_notification_gap_routes_to_subscription_check():
    decision = evaluate_route(
        Intent(kind="kanban_block_notification", risk=IntentRisk.INTERNAL),
        Attempt(action_success=True, delivery_success=None, outcome_success=None),
        Evidence(has_notification_subscription=False, outcome_present=False),
    )

    assert decision.action == RouteAction.PASSIVE_SUBSCRIPTION_CHECK
    assert FailureMode.KANBAN_NOTIFICATION_GAP in decision.failure_modes


def test_sensitive_privacy_redacts_state_and_blocks_external_action(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    intent = Intent(
        kind="support_debug",
        risk=IntentRisk.EXTERNAL,
        sensitive=True,
        context={"token": "sk-secret-token", "email": "coop@example.com"},
    )
    attempt = Attempt(action_success=False, delivery_success=False)
    evidence = Evidence(sensitive_fields=("token", "email"), fallback_available=True)
    decision = evaluate_route(intent, attempt, evidence)

    assert decision.action == RouteAction.BLOCK_PRIVACY_REVIEW
    assert FailureMode.SENSITIVE_PRIVACY in decision.failure_modes
    assert decision.external_allowed is False

    path = append_decision_jsonl(intent, attempt, evidence, decision)
    raw = path.read_text()
    assert "***" not in raw
    assert "coop@example.com" not in raw
    assert "[REDACTED]" in raw


def test_sensitive_context_is_replaced_even_without_sensitive_fields(tmp_path):
    intent = Intent(
        kind="support_debug",
        risk=IntentRisk.EXTERNAL,
        sensitive=True,
        context={
            "safe_label": "do-not-persist-raw",
            "nested": {"notes": "customer coop@example.com has token sk-testsecret123456"},
        },
    )
    attempt = Attempt(action_success=False, delivery_success=False)
    evidence = Evidence(fallback_available=True)
    decision = evaluate_route(intent, attempt, evidence)

    path = append_decision_jsonl(intent, attempt, evidence, decision, tmp_path / "events.jsonl")
    raw = path.read_text(encoding="utf-8")
    event = json.loads(raw)

    assert event["intent"]["context"] == "[REDACTED_CONTEXT]"
    assert "do-not-persist-raw" not in raw
    assert "coop@example.com" not in raw
    assert "sk-testsecret123456" not in raw


def test_redact_payload_redacts_common_sensitive_keys_and_values():
    payload = {
        "authorization": "Bearer abcdefghijklmnop",
        "headers": {"cookie": "session=abc123", "x_note": "contact coop@example.com"},
        "body": "api_key=super-secret-value",
        "items": [{"private_key": "-----BEGIN PRIVATE KEY-----"}],
    }

    redacted = redact_payload(payload)

    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["headers"]["cookie"] == "[REDACTED]"
    assert redacted["headers"]["x_note"] == "[REDACTED]"
    assert redacted["body"] == "[REDACTED]"
    assert redacted["items"][0]["private_key"] == "[REDACTED]"


def test_redact_payload_string_sensitive_fields_redacts_custom_key():
    redacted = redact_payload({"ssn": "123-45-6789", "safe": "ok"}, sensitive_fields="ssn")

    assert redacted["ssn"] == "[REDACTED]"
    assert redacted["safe"] == "ok"


def test_redact_payload_non_string_sensitive_fields_and_keys_do_not_crash():
    payload = {1: "integer key", "nested": {2: "nested integer key", "token": "raw"}}

    redacted = redact_payload(payload, sensitive_fields=(None, 3, "token"))

    assert redacted["1"] == "integer key"
    assert redacted["nested"]["2"] == "nested integer key"
    assert redacted["nested"]["token"] == "[REDACTED]"
    json.dumps(redacted, sort_keys=True)


def test_redact_payload_matches_non_string_sensitive_field_to_non_string_key():
    redacted = redact_payload({3: "123-45-6789", "safe": "ok"}, sensitive_fields=(3,))

    assert redacted["3"] == "[REDACTED]"
    assert redacted["safe"] == "ok"


def test_redact_payload_redacts_bytes_key_containing_token():
    redacted = redact_payload({b"token": "abc", "safe": "ok"})

    assert redacted["[BYTES_KEY_REDACTED]"] == "[REDACTED]"
    assert redacted["safe"] == "ok"


def test_redact_payload_redacts_sensitive_fields_inside_dataclass_values():
    @dataclass
    class Diagnostic:
        token: str
        note: str

    direct = redact_payload(Diagnostic(token="supersecret", note="ok"))
    nested = redact_payload({"d": Diagnostic(token="supersecret", note="ok")})

    assert direct == {"token": "[REDACTED]", "note": "ok"}
    assert nested["d"] == {"token": "[REDACTED]", "note": "ok"}


def test_redact_payload_bytes_sensitive_fields_treats_bytes_as_scalar_field_name():
    redacted = redact_payload({"ssn": "123-45-6789", "safe": "ok"}, sensitive_fields=b"ssn")

    assert redacted["ssn"] == "[REDACTED]"
    assert redacted["safe"] == "ok"


def test_append_decision_jsonl_with_non_json_native_context_values_succeeds(tmp_path):
    class CustomObject:
        def __repr__(self):
            return "CustomObject(value=ok)"

    intent = Intent(
        kind="diagnostic",
        context={
            "set_value": {"b", "a"},
            "bytes_value": b"not-json-native",
            "path_value": tmp_path / "artifact.txt",
            "custom_value": CustomObject(),
        },
    )
    decision = RouteDecision(RouteAction.PASSIVE_STATUS_CHECK)

    path = append_decision_jsonl(intent, Attempt(), Evidence(), decision, tmp_path / "events.jsonl")
    raw = path.read_text(encoding="utf-8")
    event = json.loads(raw)

    assert event["intent"]["context"]["set_value"] == ["a", "b"]
    assert event["intent"]["context"]["bytes_value"] == "[BYTES_REDACTED]"
    assert event["intent"]["context"]["path_value"] == str(tmp_path / "artifact.txt")
    assert event["intent"]["context"]["custom_value"] == "[UNSERIALIZABLE:CustomObject]"


def test_unknown_object_context_persists_safe_placeholder_not_repr_contents(tmp_path):
    class LeakyObject:
        def __repr__(self):
            return "LeakyObject(token=do-not-persist)"

    intent = Intent(kind="diagnostic", context={"custom_value": LeakyObject()})
    decision = RouteDecision(RouteAction.PASSIVE_STATUS_CHECK)

    path = append_decision_jsonl(intent, Attempt(), Evidence(), decision, tmp_path / "events.jsonl")
    raw = path.read_text(encoding="utf-8")
    event = json.loads(raw)

    assert event["intent"]["context"]["custom_value"] == "[UNSERIALIZABLE:LeakyObject]"
    assert "do-not-persist" not in raw
    assert "LeakyObject(token=" not in raw


def test_cyclic_context_does_not_crash_and_persists_cycle_placeholder(tmp_path):
    cyclic_context = {"label": "ok"}
    cyclic_context["self"] = cyclic_context
    intent = Intent(kind="diagnostic", context=cyclic_context)
    decision = RouteDecision(RouteAction.PASSIVE_STATUS_CHECK)

    path = append_decision_jsonl(intent, Attempt(), Evidence(), decision, tmp_path / "events.jsonl")
    event = json.loads(path.read_text(encoding="utf-8"))

    assert event["intent"]["context"]["label"] == "ok"
    assert event["intent"]["context"]["self"] == "[CYCLE]"


def test_replay_and_status_harness_reports_counts(tmp_path):
    path = tmp_path / "events.jsonl"
    decisions = [
        RouteDecision(RouteAction.NOOP_SUCCESS, (), reason="ok"),
        RouteDecision(RouteAction.PASSIVE_STATUS_CHECK, (FailureMode.MISSING_OUTCOME_EVIDENCE,), reason="missing"),
        RouteDecision(RouteAction.BLOCK_FOR_APPROVAL, (FailureMode.APPROVAL_REQUIRED,), reason="approval"),
    ]
    for decision in decisions:
        payload = {
            "intent": {"kind": "x", "risk": "internal"},
            "attempt": {},
            "evidence": {},
            "decision": decision.to_json(),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")

    replay = replay_jsonl(path)
    status = status_summary(path)

    assert replay["total"] == 3
    assert replay["invalid_lines"] == 0
    assert replay["actions"] == {
        "noop_success": 1,
        "passive_status_check": 1,
        "block_for_approval": 1,
    }
    assert status["total"] == 3
    assert status["invalid_lines"] == 0
    assert status["needs_review"] == 1


def test_replay_and_status_skip_malformed_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    valid = {
        "intent": {"kind": "x", "risk": "internal"},
        "attempt": {},
        "evidence": {},
        "decision": RouteDecision(
            RouteAction.BLOCK_PRIVACY_REVIEW,
            (FailureMode.SENSITIVE_PRIVACY,),
            reason="sensitive",
        ).to_json(),
    }
    path.write_text(
        json.dumps(valid) + "\n"
        "{truncated\n"
        "[]\n"
        "\n"
        "not-json\n",
        encoding="utf-8",
    )

    replay = replay_jsonl(path)
    status = status_summary(path)

    assert replay["total"] == 1
    assert replay["invalid_lines"] == 3
    assert replay["actions"] == {"block_privacy_review": 1}
    assert replay["failure_modes"] == {"sensitive_privacy": 1}
    assert status["invalid_lines"] == 3
    assert status["needs_review"] == 1


def test_behavior_changing_public_action_routes_to_blocked_kanban_approval_card():
    calls = []

    def fake_kanban_create(**payload):
        calls.append(payload)
        return {"task_id": "t_approval"}

    result = route_behavior_change(
        BehaviorActionCandidate(
            kind="publish_post",
            title="Publish Kryden launch post",
            body="Post launch announcement to X.",
            risk=IntentRisk.PUBLIC,
            evidence=("draft exists", "human has not approved publishing"),
            default_fallback="Do not publish; keep draft local.",
            approval_options=("approve publish", "revise draft", "reject"),
        ),
        config={
            "metacognitive_router": {
                "enabled": True,
                "dry_run": False,
                "behavior_routing": {"enabled": True},
            }
        },
        kanban_create=fake_kanban_create,
    )

    assert result.route == "blocked_approval_kanban"
    assert result.created_task_id == "t_approval"
    assert result.external_executed is False
    assert len(calls) == 1
    payload = calls[0]
    assert payload["board"] == "kryden-50k-mrr"
    assert payload["initial_status"] == "blocked"
    assert payload["idempotency_key"].startswith("metacognitive-approval:")
    assert payload["title"] == "approval-required: Publish Kryden launch post"
    assert "approval-required: public action requires Coop approval" in payload["body"]
    assert "Risk: public" in payload["body"]
    assert "Evidence:\n- draft exists\n- human has not approved publishing" in payload["body"]
    assert "Default fallback: Do not publish; keep draft local." in payload["body"]
    assert "Approval options:\n- approve publish\n- revise draft\n- reject" in payload["body"]


def test_blocked_approval_card_includes_adjacent_safe_prep_handoff():
    calls = []

    def fake_kanban_create(**payload):
        calls.append(payload)
        return {"task_id": "t_approval"}

    result = route_behavior_change(
        BehaviorActionCandidate(
            kind="publish_post",
            title="Publish launch post",
            body="Human approval required before publishing.",
            risk=IntentRisk.PUBLIC,
            adjacent_safe_prep=(
                "Draft saved at /tmp/launch.md",
                "Suggested approval command: approve publish_post after editing headline",
                "Missing field: final URL",
            ),
        ),
        config={
            "metacognitive_router": {
                "enabled": True,
                "dry_run": False,
                "behavior_routing": {"enabled": True},
            }
        },
        kanban_create=fake_kanban_create,
    )

    assert result.route == "blocked_approval_kanban"
    assert len(calls) == 1
    assert (
        "Adjacent safe prep:\n"
        "- Draft saved at /tmp/launch.md\n"
        "- Suggested approval command: approve publish_post after editing headline\n"
        "- Missing field: final URL"
    ) in calls[0]["body"]

def test_gated_behavior_kind_routes_to_blocked_approval_even_if_risk_is_misclassified_internal():
    calls = []

    def fake_kanban_create(**payload):
        calls.append(payload)
        return {"task_id": "t_approval"}

    result = route_behavior_change(
        BehaviorActionCandidate(
            kind="outreach_publish_client_case_study",
            title="Publish client case study",
            body="Send a public client-facing case study.",
            risk=IntentRisk.INTERNAL,
            default_fallback="Keep the draft local.",
        ),
        config={
            "metacognitive_router": {
                "enabled": True,
                "dry_run": False,
                "behavior_routing": {"enabled": True},
            }
        },
        kanban_create=fake_kanban_create,
    )

    assert result.route == "blocked_approval_kanban"
    assert result.external_executed is False
    assert calls[0]["board"] == "kryden-50k-mrr"
    assert calls[0]["initial_status"] == "blocked"
    assert calls[0]["title"] == "approval-required: Publish client case study"


def test_gated_behavior_text_routes_to_blocked_approval_before_internal_kanban_sink():
    calls = []

    def fake_kanban_create(**payload):
        calls.append(payload)
        return {"task_id": f"t_{len(calls)}"}

    config = {
        "metacognitive_router": {
            "enabled": True,
            "dry_run": False,
            "behavior_routing": {
                "enabled": True,
                "allow_internal_kanban": True,
                "internal_board": "kryden-50k-mrr",
            },
        }
    }
    candidates = (
        BehaviorActionCandidate(
            kind="create_internal_task",
            title="Publish public launch post",
            body="Prepare the launch post.",
            risk=IntentRisk.INTERNAL,
        ),
        BehaviorActionCandidate(
            kind="create_internal_task",
            title="Draft vendor decision",
            body="Spend money on a paid data source.",
            risk=IntentRisk.INTERNAL,
        ),
        BehaviorActionCandidate(
            kind="create_internal_task",
            title="Draft ops runbook",
            body="Restart the production service after approval.",
            risk=IntentRisk.INTERNAL,
        ),
    )

    results = [
        route_behavior_change(candidate, config=config, kanban_create=fake_kanban_create)
        for candidate in candidates
    ]

    assert [result.route for result in results] == [
        "blocked_approval_kanban",
        "blocked_approval_kanban",
        "blocked_approval_kanban",
    ]
    assert all(result.external_executed is False for result in results)
    assert [call["initial_status"] for call in calls] == ["blocked", "blocked", "blocked"]
    assert all(call["board"] == "kryden-50k-mrr" for call in calls)


def test_gated_behavior_context_routes_to_blocked_approval_before_execute_recommendation_sink():
    calls = []

    def fake_kanban_create(**payload):
        calls.append(payload)
        return {"task_id": "t_approval"}

    result = route_behavior_change(
        BehaviorActionCandidate(
            kind="write_local_summary",
            title="Summarize approval plan",
            body="Local-looking body should not override gated context.",
            risk=IntentRisk.INTERNAL,
            context={
                "next_action": "send legal outreach to a client",
                "requires": ["credential", "account change"],
            },
            execute_recommendation="write_local_file",
        ),
        config={
            "metacognitive_router": {
                "enabled": True,
                "dry_run": False,
                "behavior_routing": {
                    "enabled": True,
                    "allow_internal_execution_recommendations": True,
                },
            }
        },
        kanban_create=fake_kanban_create,
    )

    assert result.route == "blocked_approval_kanban"
    assert result.execute_recommendation is None
    assert result.external_executed is False
    assert calls[0]["initial_status"] == "blocked"
    assert calls[0]["board"] == "kryden-50k-mrr"


def test_gated_approval_route_ignores_configured_approval_board_override():
    calls = []

    def fake_kanban_create(**payload):
        calls.append(payload)
        return {"task_id": "t_approval"}

    result = route_behavior_change(
        BehaviorActionCandidate(
            kind="spend_money",
            title="Buy dataset",
            body="Purchase a lead dataset.",
            risk=IntentRisk.SPEND,
            default_fallback="Do not spend money.",
        ),
        config={
            "metacognitive_router": {
                "enabled": True,
                "dry_run": False,
                "behavior_routing": {
                    "enabled": True,
                    "approval_board": "unsafe-side-board",
                },
            }
        },
        kanban_create=fake_kanban_create,
    )

    assert result.route == "blocked_approval_kanban"
    assert calls[0]["board"] == "kryden-50k-mrr"
    assert calls[0]["initial_status"] == "blocked"


def test_behavior_changing_approval_route_uses_stable_idempotency_key():
    keys = []

    def fake_kanban_create(**payload):
        keys.append(payload["idempotency_key"])
        return {"task_id": "t_same"}

    candidate = BehaviorActionCandidate(
        kind="spend_money",
        title="Buy dataset",
        body="Purchase a lead dataset.",
        risk=IntentRisk.SPEND,
        default_fallback="Do not spend money.",
    )
    config = {
        "metacognitive_router": {
            "enabled": True,
            "dry_run": False,
            "behavior_routing": {"enabled": True},
        }
    }

    first = route_behavior_change(candidate, config=config, kanban_create=fake_kanban_create)
    second = route_behavior_change(candidate, config=config, kanban_create=fake_kanban_create)

    assert first.created_task_id == "t_same"
    assert second.created_task_id == "t_same"
    assert keys == [keys[0], keys[0]]


def test_safe_internal_behavior_can_route_to_internal_kanban_when_explicitly_enabled():
    calls = []

    def fake_kanban_create(**payload):
        calls.append(payload)
        return {"task_id": "t_internal"}

    result = route_behavior_change(
        BehaviorActionCandidate(
            kind="create_internal_research_task",
            title="Research pricing pages",
            body="Compare three public pricing pages and summarize locally.",
            risk=IntentRisk.INTERNAL,
            evidence=("local research only",),
        ),
        config={
            "metacognitive_router": {
                "enabled": True,
                "dry_run": False,
                "behavior_routing": {
                    "enabled": True,
                    "allow_internal_kanban": True,
                    "internal_board": "kryden-50k-mrr",
                },
            }
        },
        kanban_create=fake_kanban_create,
    )

    assert result.route == "internal_kanban"
    assert result.created_task_id == "t_internal"
    assert result.external_executed is False
    assert calls == [
        {
            "title": "Research pricing pages",
            "body": "Compare three public pricing pages and summarize locally.\n\nMetacognitive internal routing evidence:\n- local research only",
            "assignee": "builder",
            "board": "kryden-50k-mrr",
            "initial_status": "running",
            "idempotency_key": result.idempotency_key,
        }
    ]


def test_safe_internal_behavior_can_return_execute_recommendation_under_explicit_flag_without_external_call():
    def forbidden_kanban_create(**payload):  # pragma: no cover - should never be called
        raise AssertionError(payload)

    result = route_behavior_change(
        BehaviorActionCandidate(
            kind="write_local_summary",
            title="Write local summary",
            body="Update a local scratch artifact.",
            risk=IntentRisk.INTERNAL,
            execute_recommendation="write_local_file",
        ),
        config={
            "metacognitive_router": {
                "enabled": True,
                "dry_run": False,
                "behavior_routing": {
                    "enabled": True,
                    "allow_internal_execution_recommendations": True,
                },
            }
        },
        kanban_create=forbidden_kanban_create,
    )

    assert result.route == "internal_execute_recommendation"
    assert result.execute_recommendation == "write_local_file"
    assert result.external_executed is False


def test_behavior_routing_conservative_defaults_do_not_change_behavior():
    def forbidden_kanban_create(**payload):  # pragma: no cover - should never be called
        raise AssertionError(payload)

    result = route_behavior_change(
        BehaviorActionCandidate(
            kind="publish_post",
            title="Publish Kryden launch post",
            body="Post launch announcement to X.",
            risk=IntentRisk.PUBLIC,
        ),
        config={},
        kanban_create=forbidden_kanban_create,
    )

    assert result.route == "disabled"
    assert result.created_task_id is None
    assert result.external_executed is False
