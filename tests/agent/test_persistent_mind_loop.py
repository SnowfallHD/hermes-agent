import json
from pathlib import Path

from hermes_cli.config import DEFAULT_CONFIG

from agent.persistent_mind_loop import (
    ActionRoute,
    PlannedEffectKind,
    PolicyCandidate,
    RiskTier,
    UnknownRoute,
    _main,
    default_state_dir,
    policy_candidate_matches_proven_behavior,
    load_feature_config,
    plan_action_effects,
    reduce_persistent_mind_loop,
)


def test_feature_config_defaults_disabled_and_missing_inputs_write_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state_dir = tmp_path / "state-out"

    config = load_feature_config({})
    state = reduce_persistent_mind_loop(input_paths=[tmp_path / "missing.jsonl"], state_dir=state_dir)

    assert config.enabled is False
    assert config.dry_run is True
    assert config.write_state is False
    assert DEFAULT_CONFIG["persistent_mind_loop"] == {
        "enabled": False,
        "dry_run": True,
        "write_state": False,
    }
    assert default_state_dir() == tmp_path / "state" / "persistent_mind_loop"
    assert state.mission.mrr_target == 50000
    assert state.mission.known_mrr is None
    assert state_dir.exists() is False


def test_zero_external_revenue_signal_excludes_internal_activity(tmp_path):
    internal = tmp_path / "internal.jsonl"
    internal.write_text(
        "\n".join(
            [
                json.dumps({"kind": "kanban", "title": "build landing page", "amount": 5000}),
                json.dumps({"kind": "cron_report", "summary": "MRR maybe closer"}),
                json.dumps({"kind": "draft", "title": "pricing page copy"}),
                json.dumps({"kind": "internal_report", "metric": "pipeline", "amount": 10000}),
            ]
        ),
        encoding="utf-8",
    )

    state = reduce_persistent_mind_loop(input_paths=[internal], state_dir=tmp_path / "state", write_state=True)

    assert state.opportunity.external_revenue_signal_total == 0
    assert state.opportunity.internal_activity_count == 4
    assert state.mission.known_mrr is None
    assert state.action.actions == ()
    assert json.loads((tmp_path / "state" / "opportunity_state.json").read_text())["external_revenue_signal_total"] == 0


def test_approval_gated_action_has_required_route_fields(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "kind": "action_candidate",
                "id": "post-launch-thread",
                "text": "Post the Nocturnal demo publicly",
                "risk_tier": "public",
                "owner": "hermes",
                "expected_outcome": "collect external buyer interest",
                "evidence_refs": [{"source": "drafts/nocturnal.md", "kind": "draft", "id": "d1"}],
            }
        ),
        encoding="utf-8",
    )

    state = reduce_persistent_mind_loop(input_paths=[events], state_dir=tmp_path / "state")

    assert len(state.action.actions) == 1
    action = state.action.actions[0]
    assert action.route is ActionRoute.BLOCKED_KANBAN_APPROVAL
    assert action.risk_tier is RiskTier.PUBLIC
    assert action.owner == "hermes"
    assert action.permission_required is True
    assert action.expected_outcome == "collect external buyer interest"
    assert action.evidence_refs[0].source == "drafts/nocturnal.md"


def test_gated_action_defaults_to_blocked_kanban_approval_for_linear_sync(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "kind": "action_candidate",
                "id": "dm-prospects",
                "text": "DM ten prospects about Leadbox",
                "risk_tier": "public",
                "owner": "profile:growth",
                "expected_outcome": "validate buyer pain with external replies",
            }
        ),
        encoding="utf-8",
    )

    state = reduce_persistent_mind_loop(input_paths=[events], state_dir=tmp_path / "state")
    action = state.action.actions[0]
    effects = plan_action_effects(state.action)

    assert action.route is ActionRoute.BLOCKED_KANBAN_APPROVAL
    assert action.permission_required is True
    assert effects[0].kind is PlannedEffectKind.BLOCKED_KANBAN_CARD
    assert effects[0].board == "kryden-50k-mrr"
    assert effects[0].initial_status == "blocked"
    assert effects[0].reason.startswith("approval-required:")


def test_safe_internal_action_can_plan_execution_without_approval(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "kind": "action_candidate",
                "id": "refresh-local-artifact",
                "text": "Regenerate the local Kryden status artifact",
                "risk_tier": "sandbox",
                "owner": "hermes",
                "expected_outcome": "local status JSON is fresh",
            }
        ),
        encoding="utf-8",
    )

    state = reduce_persistent_mind_loop(input_paths=[events], state_dir=tmp_path / "state")
    action = state.action.actions[0]
    effects = plan_action_effects(state.action)

    assert action.route is ActionRoute.EXECUTE
    assert action.permission_required is False
    assert effects[0].kind is PlannedEffectKind.LOCAL_EXECUTION
    assert effects[0].requires_permission is False


def test_kanban_route_plans_internal_followup_card_without_external_approval(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "kind": "action_candidate",
                "id": "write-probe-task",
                "route": "kanban",
                "text": "Create an internal probe task for the next buyer segment",
                "risk_tier": "draft_only",
                "owner": "profile:researcher",
                "expected_outcome": "research task survives restart",
            }
        ),
        encoding="utf-8",
    )

    state = reduce_persistent_mind_loop(input_paths=[events], state_dir=tmp_path / "state")
    action = state.action.actions[0]
    effects = plan_action_effects(state.action)

    assert action.route is ActionRoute.KANBAN
    assert action.permission_required is False
    assert effects[0].kind is PlannedEffectKind.KANBAN_CARD
    assert effects[0].initial_status == "ready"


def test_gated_risk_overrides_explicit_execute_route_to_blocked_approval(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "kind": "action_candidate",
                "id": "unsafe-explicit-route",
                "route": "execute",
                "text": "Publish launch announcement from Hermes",
                "risk_tier": "public",
                "owner": "hermes",
                "expected_outcome": "external attention",
            }
        ),
        encoding="utf-8",
    )

    state = reduce_persistent_mind_loop(input_paths=[events], state_dir=tmp_path / "state")
    action = state.action.actions[0]
    effects = plan_action_effects(state.action)

    assert action.route is ActionRoute.BLOCKED_KANBAN_APPROVAL
    assert action.permission_required is True
    assert effects[0].kind is PlannedEffectKind.BLOCKED_KANBAN_CARD


def test_silent_and_ignore_routing_prevents_every_thought_becoming_notification(tmp_path):
    events = tmp_path / "thoughts.jsonl"
    events.write_text(
        "\n".join(
            [
                json.dumps({"kind": "thought", "id": "weak", "text": "maybe rewrite this later", "strength": "weak"}),
                json.dumps({"kind": "thought", "id": "noise", "text": "random mental exhaust", "route": "ignore"}),
            ]
        ),
        encoding="utf-8",
    )

    state = reduce_persistent_mind_loop(input_paths=[events], state_dir=tmp_path / "state")

    routes = [action.route for action in state.action.actions]
    assert routes == [ActionRoute.SILENT_LOG, ActionRoute.IGNORE]
    assert all(action.permission_required is False for action in state.action.actions)


def test_cli_dry_run_write_state_writes_only_local_state_files(tmp_path, capsys):
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"kind": "market_pain", "text": "manual lead follow-up hurts"}), encoding="utf-8")
    state_dir = tmp_path / "local-state"

    assert _main(["reduce", "--dry-run", "--write-state", "--state-dir", str(state_dir), "--input", str(events)]) == 0

    stdout = capsys.readouterr().out
    assert "manual lead follow-up hurts" in stdout
    assert (state_dir / "opportunity_state.json").exists()
    assert sorted(path.name for path in state_dir.iterdir()) == [
        "action_state.json",
        "learning_state.json",
        "mission_state.json",
        "opportunity_state.json",
        "policy_state.json",
        "self_state.json",
        "synthesis_state.json",
        "unknown_state.json",
    ]


def test_unknown_to_probe_conversion(tmp_path):
    events = tmp_path / "unknowns.jsonl"
    events.write_text(
        json.dumps(
            {
                "kind": "unknown",
                "id": "who-buys-first",
                "text": "which buyer segment has urgent pain",
                "route": "probe",
                "probe": "interview 5 target users before building more",
                "evidence_refs": [{"source": "strategy", "kind": "assumption", "id": "a1"}],
            }
        ),
        encoding="utf-8",
    )

    state = reduce_persistent_mind_loop(input_paths=[events], state_dir=tmp_path / "state")

    assert len(state.unknown.known_unknowns) == 1
    assert state.unknown.known_unknowns[0].route is UnknownRoute.PROBE
    assert len(state.unknown.probes) == 1
    assert state.unknown.probes[0].question == "which buyer segment has urgent pain"
    assert state.unknown.probes[0].method == "interview 5 target users before building more"


def test_policy_candidate_records_behavior_identity_and_consequence_class(tmp_path):
    events = tmp_path / "policies.jsonl"
    events.write_text(
        json.dumps(
            {
                "kind": "policy_candidate",
                "id": "private-status-to-home-channel",
                "action": "deliver_review_required_status",
                "surface": "slack_home_channel",
                "authority": "self_reviewed_internal",
                "output": "private_status_message",
                "consequence_class": "low_consequence_private_delivery",
                "actual_risk_indicators": ["private channel", "status-only", "no external user"],
                "fake_risk_rationale": "Not a public post just because Slack is a message surface.",
            }
        ),
        encoding="utf-8",
    )

    state = reduce_persistent_mind_loop(input_paths=[events], state_dir=tmp_path / "state")

    candidate = state.policy.candidates[0]
    assert candidate.action == "deliver_review_required_status"
    assert candidate.surface == "slack_home_channel"
    assert candidate.authority == "self_reviewed_internal"
    assert candidate.output == "private_status_message"
    assert candidate.consequence_class == "low_consequence_private_delivery"
    assert candidate.policy_exception_class is None
    assert candidate.actual_risk_indicators == ("private channel", "status-only", "no external user")
    assert candidate.fake_risk_rationale == "Not a public post just because Slack is a message surface."


def test_policy_promotion_requires_same_action_surface_authority_and_output():
    proven = PolicyCandidate(
        id="proven",
        action="deliver_review_required_status",
        surface="slack_home_channel",
        authority="self_reviewed_internal",
        output="private_status_message",
        consequence_class="low_consequence_private_delivery",
    )
    same_behavior = PolicyCandidate(
        id="same",
        action="deliver_review_required_status",
        surface="slack_home_channel",
        authority="self_reviewed_internal",
        output="private_status_message",
        consequence_class="low_consequence_private_delivery",
    )
    expanded_output = PolicyCandidate(
        id="expanded",
        action="deliver_review_required_status",
        surface="slack_home_channel",
        authority="self_reviewed_internal",
        output="public_launch_post",
        consequence_class="public_reputation_surface",
    )

    assert policy_candidate_matches_proven_behavior(same_behavior, proven) is True
    assert policy_candidate_matches_proven_behavior(expanded_output, proven) is False
