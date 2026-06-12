"""Persistent mind-loop typed state and safe local reducer.

This module is deliberately side-effect constrained. It reads optional local JSON
or JSONL inputs, reduces them into typed state snapshots, and writes only local
JSON state files when ``write_state=True`` is passed explicitly. It can also
plan routed effects for future executors, but it never sends messages, directly
creates Kanban cards, changes cron/gateway behavior, or performs external
actions.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home


FEATURE_FLAG_NAME = "persistent_mind_loop.enabled"
_SAFE_DEFAULT_ENABLED = False
_SAFE_DEFAULT_DRY_RUN = True
_SAFE_DEFAULT_WRITE_STATE = False
_BOOLEAN_TRUE_STRINGS = {"true", "yes", "on", "1"}
_BOOLEAN_FALSE_STRINGS = {"false", "no", "off", "0"}
_INTERNAL_REVENUE_KINDS = {"kanban", "cron_report", "draft", "internal_report", "task", "plan", "status"}
_EXTERNAL_REVENUE_KINDS = {
    "external_revenue",
    "payment",
    "invoice_paid",
    "customer_payment",
    "customer_commitment",
    "signed_contract",
    "paid_subscription",
}


class UnknownRoute(str, Enum):
    RESEARCH = "research"
    PROBE = "probe"
    ASSUMPTION = "assumption"
    IGNORE = "ignore"


class ActionRoute(str, Enum):
    SILENT_LOG = "silent_log"
    DAILY_BRIEF = "daily_brief"
    KANBAN = "kanban"
    LINEAR = "linear"
    APPROVAL = "approval"
    BLOCKED_KANBAN_APPROVAL = "blocked_kanban_approval"
    EXECUTE = "execute"
    IGNORE = "ignore"


class RiskTier(str, Enum):
    READ_ONLY = "read_only"
    DRAFT_ONLY = "draft_only"
    SANDBOX = "sandbox"
    APPROVAL_GATED = "approval_gated"
    SPEND_CAPPED = "spend_capped"
    PRODUCTION = "production"
    PUBLIC = "public"


class EvidenceKind(str, Enum):
    INTERNAL = "internal"
    EXTERNAL_REVENUE = "external_revenue"
    MARKET = "market"
    ASSUMPTION = "assumption"
    DRAFT = "draft"


class PlannedEffectKind(str, Enum):
    NONE = "none"
    LOCAL_EXECUTION = "local_execution"
    KANBAN_CARD = "kanban_card"
    BLOCKED_KANBAN_CARD = "blocked_kanban_card"
    DAILY_BRIEF_ITEM = "daily_brief_item"
    SILENT_LOG = "silent_log"


@dataclass(frozen=True)
class MindLoopFeatureConfig:
    enabled: bool = False
    dry_run: bool = True
    write_state: bool = False
    flag_name: str = FEATURE_FLAG_NAME


@dataclass(frozen=True)
class EvidenceRef:
    source: str
    kind: str = EvidenceKind.INTERNAL.value
    id: str | None = None
    external: bool = False


@dataclass(frozen=True)
class Goal:
    text: str
    created_at: str | None = None
    source: str = "local_reducer"
    confidence: float = 0.5
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class MissionState:
    active_goals: tuple[Goal, ...] = ()
    mrr_target: int = 50000
    known_mrr: int | None = None
    today_win: str | None = None
    week_win: str | None = None
    current_bottleneck: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class SelfState:
    capabilities: tuple[str, ...] = ()
    access_gaps: tuple[str, ...] = ()
    permission_tiers: tuple[str, ...] = ()
    recent_failures: tuple[str, ...] = ()
    recent_corrections: tuple[str, ...] = ()
    improvement_candidates: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class OpportunityState:
    bets: tuple[str, ...] = ()
    market_pains: tuple[str, ...] = ()
    market_language: tuple[str, ...] = ()
    experiments: tuple[str, ...] = ()
    external_revenue_signal_total: int = 0
    internal_activity_count: int = 0
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class Unknown:
    id: str | None
    question: str
    route: UnknownRoute
    created_at: str | None = None
    source: str = "local_reducer"
    confidence: float = 0.5
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class Probe:
    question: str
    method: str
    owner: str = "hermes"
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class UnknownState:
    known_facts: tuple[str, ...] = ()
    assumed_knowns: tuple[Unknown, ...] = ()
    known_unknowns: tuple[Unknown, ...] = ()
    unknown_unknown_candidates: tuple[Unknown, ...] = ()
    probes: tuple[Probe, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class SynthesisState:
    signals: tuple[str, ...] = ()
    collisions: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class ActionCandidate:
    route: ActionRoute
    risk_tier: RiskTier
    owner: str
    next_action: str
    permission_required: bool
    expected_outcome: str
    evidence_refs: tuple[EvidenceRef, ...] = ()
    id: str | None = None
    source: str = "local_reducer"
    confidence: float = 0.5
    default_fallback: str = "do_nothing"


@dataclass(frozen=True)
class PlannedEffect:
    """Deterministic behavior-routing plan for an action candidate.

    This is intentionally a plan, not an executor. Callers can inspect it in
    dry-run mode; future schedulers may apply it with explicit injected Kanban or
    local-execution adapters. The default reducer still writes no external state.
    """

    kind: PlannedEffectKind
    title: str
    owner: str
    expected_outcome: str
    requires_permission: bool
    action_id: str | None = None
    board: str | None = None
    initial_status: str | None = None
    reason: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class ActionState:
    actions: tuple[ActionCandidate, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class Learning:
    action_id: str | None
    expected_outcome: str
    observed_outcome: str | None = None
    delta: str | None = None
    memory_update: str | None = None
    skill_update: str | None = None
    task_update: str | None = None
    metric_update: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class LearningState:
    learnings: tuple[Learning, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class PolicyCandidate:
    """Candidate for durable autonomous policy compounding.

    Policy promotion is intentionally scoped by behavior identity. A proven
    behavior only authorizes future repeats with the same action, surface,
    authority, and output. Any expansion on one of those axes is a new proposal,
    even if the general consequence class remains low-risk.
    """

    id: str | None
    action: str
    surface: str
    authority: str
    output: str
    consequence_class: str
    policy_exception_class: str | None = None
    actual_risk_indicators: tuple[str, ...] = ()
    fake_risk_rationale: str | None = None
    confidence: float = 0.5
    evidence_refs: tuple[EvidenceRef, ...] = ()

    @property
    def behavior_identity(self) -> tuple[str, str, str, str]:
        return (self.action, self.surface, self.authority, self.output)


@dataclass(frozen=True)
class PolicyState:
    candidates: tuple[PolicyCandidate, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class PersistentMindState:
    mission: MissionState = field(default_factory=MissionState)
    self: SelfState = field(default_factory=SelfState)
    opportunity: OpportunityState = field(default_factory=OpportunityState)
    unknown: UnknownState = field(default_factory=UnknownState)
    synthesis: SynthesisState = field(default_factory=SynthesisState)
    action: ActionState = field(default_factory=ActionState)
    learning: LearningState = field(default_factory=LearningState)
    policy: PolicyState = field(default_factory=PolicyState)

    def to_json(self) -> dict[str, Any]:
        return _to_jsonable(self)


def load_feature_config(config: Any) -> MindLoopFeatureConfig:
    """Load the default-disabled feature gate from a config dictionary."""

    root = config if isinstance(config, dict) else {}
    section = root.get("persistent_mind_loop", {})
    if not isinstance(section, dict):
        section = {}
    return MindLoopFeatureConfig(
        enabled=_parse_bool(section.get("enabled"), default=_SAFE_DEFAULT_ENABLED),
        dry_run=_parse_bool(section.get("dry_run"), default=_SAFE_DEFAULT_DRY_RUN),
        write_state=_parse_bool(section.get("write_state"), default=_SAFE_DEFAULT_WRITE_STATE),
    )


def default_state_dir() -> Path:
    """Return the profile-safe local directory for state snapshots."""

    return get_hermes_home() / "state" / "persistent_mind_loop"


def reduce_persistent_mind_loop(
    input_paths: Iterable[str | Path] | None = None,
    *,
    state_dir: str | Path | None = None,
    write_state: bool = False,
) -> PersistentMindState:
    """Reduce local optional inputs into the seven typed mind-loop states.

    Missing paths, malformed JSON lines, and non-object records are ignored. The
    reducer writes no files unless ``write_state`` is explicitly true.
    """

    events = list(_read_local_events(input_paths or ()))
    state = _reduce_events(events)
    if write_state:
        _write_state_files(state, Path(state_dir) if state_dir is not None else default_state_dir())
    return state


def plan_action_effects(action_state: ActionState | Iterable[ActionCandidate]) -> tuple[PlannedEffect, ...]:
    """Convert action candidates into deterministic side-effect plans.

    The reducer itself remains dry-run/local. This policy is the seam future
    schedulers can use to execute safe local work or create Kanban cards with
    explicit adapters. Approval-gated actions always route to a blocked
    ``kryden-50k-mrr`` Kanban card, relying on the existing blocked-card Linear
    sync rather than inventing a parallel approval store.
    """

    actions = action_state.actions if isinstance(action_state, ActionState) else tuple(action_state)
    return tuple(_planned_effect_for_action(action) for action in actions)


def policy_candidate_matches_proven_behavior(candidate: PolicyCandidate, proven: PolicyCandidate) -> bool:
    """Return true only when a candidate repeats the exact proven behavior.

    This deliberately ignores destination-specific allowlists and checks the
    behavior contract Coop called out: action, surface, authority, and output.
    Expanding any of those is a separate behavior proposal.
    """

    return candidate.behavior_identity == proven.behavior_identity


def _reduce_events(events: list[dict[str, Any]]) -> PersistentMindState:
    goals: list[Goal] = []
    evidence_refs: list[EvidenceRef] = []
    known_mrr: int | None = None
    current_bottleneck: str | None = None
    bets: list[str] = []
    market_pains: list[str] = []
    market_language: list[str] = []
    experiments: list[str] = []
    external_revenue_total = 0
    internal_activity_count = 0
    known_facts: list[str] = []
    assumptions: list[Unknown] = []
    known_unknowns: list[Unknown] = []
    unknown_candidates: list[Unknown] = []
    probes: list[Probe] = []
    signals: list[str] = []
    collisions: list[str] = []
    patterns: list[str] = []
    contradictions: list[str] = []
    hypotheses: list[str] = []
    actions: list[ActionCandidate] = []
    learnings: list[Learning] = []
    policy_candidates: list[PolicyCandidate] = []

    for event in events:
        kind = _clean_string(event.get("kind")) or "unknown"
        event_refs = _parse_evidence_refs(event.get("evidence_refs"))
        evidence_refs.extend(event_refs)

        if kind in _INTERNAL_REVENUE_KINDS:
            internal_activity_count += 1
        elif kind in _EXTERNAL_REVENUE_KINDS:
            amount = _safe_int(event.get("amount")) or 0
            external_revenue_total += max(amount, 0)
            evidence_refs.extend(event_refs or (_event_evidence_ref(event, kind, external=True),))

        if kind == "mission":
            goal_text = _clean_string(event.get("goal") or event.get("text"))
            if goal_text:
                goals.append(Goal(text=goal_text, evidence_refs=event_refs))
            known_mrr = _safe_int(event.get("known_mrr")) if event.get("known_mrr") is not None else known_mrr
            current_bottleneck = _clean_string(event.get("current_bottleneck")) or current_bottleneck
        elif kind == "market_pain":
            _append_clean(market_pains, event.get("text") or event.get("pain"))
        elif kind == "market_language":
            _append_clean(market_language, event.get("text") or event.get("phrase"))
        elif kind == "bet":
            _append_clean(bets, event.get("text") or event.get("title"))
        elif kind == "experiment":
            _append_clean(experiments, event.get("text") or event.get("title"))
        elif kind == "fact":
            _append_clean(known_facts, event.get("text"))
        elif kind == "unknown":
            unknown = _unknown_from_event(event, event_refs)
            if unknown.route is UnknownRoute.ASSUMPTION:
                assumptions.append(unknown)
            elif unknown.route is UnknownRoute.IGNORE:
                unknown_candidates.append(unknown)
            else:
                known_unknowns.append(unknown)
            if unknown.route is UnknownRoute.PROBE:
                probes.append(
                    Probe(
                        question=unknown.question,
                        method=_clean_string(event.get("probe") or event.get("method")) or f"Probe: {unknown.question}",
                        evidence_refs=event_refs,
                    )
                )
        elif kind in {"signal", "thought"}:
            action = _action_from_thought(event, event_refs)
            if action is not None:
                actions.append(action)
            else:
                _append_clean(signals, event.get("text"))
        elif kind == "collision":
            _append_clean(collisions, event.get("text"))
        elif kind == "pattern":
            _append_clean(patterns, event.get("text"))
        elif kind == "contradiction":
            _append_clean(contradictions, event.get("text"))
        elif kind == "hypothesis":
            _append_clean(hypotheses, event.get("text"))
        elif kind == "action_candidate":
            actions.append(_action_from_event(event, event_refs))
        elif kind == "policy_candidate":
            policy_candidates.append(_policy_candidate_from_event(event, event_refs))
        elif kind == "learning":
            learnings.append(
                Learning(
                    action_id=_clean_string(event.get("action_id") or event.get("id")),
                    expected_outcome=_clean_string(event.get("expected_outcome")) or "unspecified expected outcome",
                    observed_outcome=_clean_string(event.get("observed_outcome")),
                    delta=_clean_string(event.get("delta")),
                    memory_update=_clean_string(event.get("memory_update")),
                    skill_update=_clean_string(event.get("skill_update")),
                    task_update=_clean_string(event.get("task_update")),
                    metric_update=_clean_string(event.get("metric_update")),
                    evidence_refs=event_refs,
                )
            )

    if external_revenue_total > 0:
        known_mrr = external_revenue_total

    return PersistentMindState(
        mission=MissionState(
            active_goals=tuple(goals),
            known_mrr=known_mrr,
            current_bottleneck=current_bottleneck,
            evidence_refs=tuple(evidence_refs),
        ),
        opportunity=OpportunityState(
            bets=tuple(bets),
            market_pains=tuple(market_pains),
            market_language=tuple(market_language),
            experiments=tuple(experiments),
            external_revenue_signal_total=external_revenue_total,
            internal_activity_count=internal_activity_count,
            evidence_refs=tuple(evidence_refs),
        ),
        unknown=UnknownState(
            known_facts=tuple(known_facts),
            assumed_knowns=tuple(assumptions),
            known_unknowns=tuple(known_unknowns),
            unknown_unknown_candidates=tuple(unknown_candidates),
            probes=tuple(probes),
            evidence_refs=tuple(evidence_refs),
        ),
        synthesis=SynthesisState(
            signals=tuple(signals),
            collisions=tuple(collisions),
            patterns=tuple(patterns),
            contradictions=tuple(contradictions),
            hypotheses=tuple(hypotheses),
            evidence_refs=tuple(evidence_refs),
        ),
        action=ActionState(actions=tuple(actions), evidence_refs=tuple(evidence_refs)),
        learning=LearningState(learnings=tuple(learnings), evidence_refs=tuple(evidence_refs)),
        policy=PolicyState(candidates=tuple(policy_candidates), evidence_refs=tuple(evidence_refs)),
    )


def _planned_effect_for_action(action: ActionCandidate) -> PlannedEffect:
    if action.route is ActionRoute.EXECUTE:
        return PlannedEffect(
            kind=PlannedEffectKind.LOCAL_EXECUTION,
            title=action.next_action,
            owner=action.owner,
            expected_outcome=action.expected_outcome,
            requires_permission=action.permission_required,
            action_id=action.id,
            evidence_refs=action.evidence_refs,
        )
    if action.route is ActionRoute.KANBAN:
        return PlannedEffect(
            kind=PlannedEffectKind.KANBAN_CARD,
            title=action.next_action,
            owner=action.owner,
            expected_outcome=action.expected_outcome,
            requires_permission=False,
            action_id=action.id,
            initial_status="ready",
            evidence_refs=action.evidence_refs,
        )
    if action.route in {ActionRoute.APPROVAL, ActionRoute.BLOCKED_KANBAN_APPROVAL, ActionRoute.LINEAR}:
        return PlannedEffect(
            kind=PlannedEffectKind.BLOCKED_KANBAN_CARD,
            title=action.next_action,
            owner=action.owner,
            expected_outcome=action.expected_outcome,
            requires_permission=True,
            action_id=action.id,
            board="kryden-50k-mrr",
            initial_status="blocked",
            reason=f"approval-required: {action.next_action}",
            evidence_refs=action.evidence_refs,
        )
    if action.route is ActionRoute.DAILY_BRIEF:
        return PlannedEffect(
            kind=PlannedEffectKind.DAILY_BRIEF_ITEM,
            title=action.next_action,
            owner=action.owner,
            expected_outcome=action.expected_outcome,
            requires_permission=False,
            action_id=action.id,
            evidence_refs=action.evidence_refs,
        )
    if action.route is ActionRoute.SILENT_LOG:
        return PlannedEffect(
            kind=PlannedEffectKind.SILENT_LOG,
            title=action.next_action,
            owner=action.owner,
            expected_outcome=action.expected_outcome,
            requires_permission=False,
            action_id=action.id,
            evidence_refs=action.evidence_refs,
        )
    return PlannedEffect(
        kind=PlannedEffectKind.NONE,
        title=action.next_action,
        owner=action.owner,
        expected_outcome=action.expected_outcome,
        requires_permission=False,
        action_id=action.id,
        evidence_refs=action.evidence_refs,
    )


def _action_from_thought(event: dict[str, Any], evidence_refs: tuple[EvidenceRef, ...]) -> ActionCandidate | None:
    explicit_route = _clean_string(event.get("route"))
    strength = (_clean_string(event.get("strength")) or "").lower()
    if explicit_route == ActionRoute.IGNORE.value:
        route = ActionRoute.IGNORE
    elif explicit_route in {ActionRoute.SILENT_LOG.value, None} or strength in {"weak", "low"}:
        route = ActionRoute.SILENT_LOG
    else:
        return None
    return ActionCandidate(
        route=route,
        risk_tier=RiskTier.READ_ONLY,
        owner="hermes",
        next_action=_clean_string(event.get("text")) or "record private thought",
        permission_required=False,
        expected_outcome="kept internal without notifying Coop",
        evidence_refs=evidence_refs,
        id=_clean_string(event.get("id")),
    )


def _action_from_event(event: dict[str, Any], evidence_refs: tuple[EvidenceRef, ...]) -> ActionCandidate:
    risk_tier = _parse_risk_tier(event.get("risk_tier"))
    route = _parse_action_route(event.get("route")) or _route_for_risk(risk_tier)
    if _is_gated_risk(risk_tier):
        route = ActionRoute.BLOCKED_KANBAN_APPROVAL
    permission_required = _requires_permission(route, risk_tier)
    return ActionCandidate(
        route=route,
        risk_tier=risk_tier,
        owner=_clean_string(event.get("owner")) or "hermes",
        next_action=_clean_string(event.get("next_action") or event.get("text")) or "review local action candidate",
        permission_required=permission_required,
        expected_outcome=_clean_string(event.get("expected_outcome")) or "unspecified expected outcome",
        evidence_refs=evidence_refs,
        id=_clean_string(event.get("id")),
        confidence=_safe_float(event.get("confidence"), default=0.5),
        default_fallback=_clean_string(event.get("default_fallback")) or "do_nothing",
    )


def _policy_candidate_from_event(event: dict[str, Any], evidence_refs: tuple[EvidenceRef, ...]) -> PolicyCandidate:
    return PolicyCandidate(
        id=_clean_string(event.get("id")),
        action=_clean_string(event.get("action")) or "unspecified_action",
        surface=_clean_string(event.get("surface")) or "unspecified_surface",
        authority=_clean_string(event.get("authority")) or "unspecified_authority",
        output=_clean_string(event.get("output")) or "unspecified_output",
        consequence_class=_clean_string(event.get("consequence_class")) or "unspecified_consequence",
        policy_exception_class=_clean_string(event.get("policy_exception_class")),
        actual_risk_indicators=_clean_string_tuple(event.get("actual_risk_indicators")),
        fake_risk_rationale=_clean_string(event.get("fake_risk_rationale")),
        confidence=_safe_float(event.get("confidence"), default=0.5),
        evidence_refs=evidence_refs,
    )


def _unknown_from_event(event: dict[str, Any], evidence_refs: tuple[EvidenceRef, ...]) -> Unknown:
    route = _parse_unknown_route(event.get("route"))
    return Unknown(
        id=_clean_string(event.get("id")),
        question=_clean_string(event.get("question") or event.get("text")) or "unspecified unknown",
        route=route,
        created_at=_clean_string(event.get("created_at")),
        source=_clean_string(event.get("source")) or "local_reducer",
        confidence=_safe_float(event.get("confidence"), default=0.5),
        evidence_refs=evidence_refs,
    )


def _parse_unknown_route(value: Any) -> UnknownRoute:
    if isinstance(value, UnknownRoute):
        return value
    normalized = (_clean_string(value) or "research").lower()
    try:
        return UnknownRoute(normalized)
    except ValueError:
        return UnknownRoute.RESEARCH


def _parse_action_route(value: Any) -> ActionRoute | None:
    if isinstance(value, ActionRoute):
        return value
    normalized = _clean_string(value)
    if not normalized:
        return None
    try:
        return ActionRoute(normalized.lower())
    except ValueError:
        return None


def _parse_risk_tier(value: Any) -> RiskTier:
    if isinstance(value, RiskTier):
        return value
    normalized = (_clean_string(value) or RiskTier.READ_ONLY.value).lower()
    try:
        return RiskTier(normalized)
    except ValueError:
        return RiskTier.APPROVAL_GATED


def _route_for_risk(risk_tier: RiskTier) -> ActionRoute:
    if _is_gated_risk(risk_tier):
        return ActionRoute.BLOCKED_KANBAN_APPROVAL
    if risk_tier in {RiskTier.READ_ONLY, RiskTier.DRAFT_ONLY, RiskTier.SANDBOX}:
        return ActionRoute.EXECUTE
    return ActionRoute.BLOCKED_KANBAN_APPROVAL


def _requires_permission(route: ActionRoute, risk_tier: RiskTier) -> bool:
    return route in {ActionRoute.APPROVAL, ActionRoute.BLOCKED_KANBAN_APPROVAL, ActionRoute.LINEAR} or _is_gated_risk(risk_tier)


def _is_gated_risk(risk_tier: RiskTier) -> bool:
    return risk_tier in {
        RiskTier.APPROVAL_GATED,
        RiskTier.SPEND_CAPPED,
        RiskTier.PRODUCTION,
        RiskTier.PUBLIC,
    }


def _read_local_events(paths: Iterable[str | Path]) -> Iterable[dict[str, Any]]:
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists() or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            continue
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            yield from _events_from_payload(payload)
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield from _events_from_payload(payload)


def _events_from_payload(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("events"), list):
            for event in payload["events"]:
                if isinstance(event, dict):
                    yield event
        else:
            yield payload
    elif isinstance(payload, list):
        for event in payload:
            if isinstance(event, dict):
                yield event


def _write_state_files(state: PersistentMindState, state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (
        ("mission_state.json", state.mission),
        ("self_state.json", state.self),
        ("opportunity_state.json", state.opportunity),
        ("unknown_state.json", state.unknown),
        ("synthesis_state.json", state.synthesis),
        ("action_state.json", state.action),
        ("learning_state.json", state.learning),
        ("policy_state.json", state.policy),
    ):
        (state_dir / name).write_text(json.dumps(_to_jsonable(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_evidence_refs(value: Any) -> tuple[EvidenceRef, ...]:
    if not isinstance(value, list):
        return ()
    refs: list[EvidenceRef] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        source = _clean_string(item.get("source"))
        if not source:
            continue
        refs.append(
            EvidenceRef(
                source=source,
                kind=_clean_string(item.get("kind")) or EvidenceKind.INTERNAL.value,
                id=_clean_string(item.get("id")),
                external=bool(item.get("external", False)),
            )
        )
    return tuple(refs)


def _event_evidence_ref(event: dict[str, Any], kind: str, *, external: bool) -> EvidenceRef:
    return EvidenceRef(
        source=_clean_string(event.get("source")) or "local_input",
        kind=kind,
        id=_clean_string(event.get("id")),
        external=external,
    )


def _append_clean(items: list[str], value: Any) -> None:
    cleaned = _clean_string(value)
    if cleaned:
        items.append(cleaned)


def _clean_string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(cleaned for item in value if (cleaned := _clean_string(item)))


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _safe_float(value: Any, *, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _parse_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _BOOLEAN_TRUE_STRINGS:
            return True
        if normalized in _BOOLEAN_FALSE_STRINGS:
            return False
    return default


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field_def.name: _to_jsonable(getattr(value, field_def.name)) for field_def in fields(value)}
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return f"[UNSERIALIZABLE:{type(value).__name__}]"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reduce local inputs into persistent mind-loop state JSON")
    parser.add_argument("command", choices=("reduce",))
    parser.add_argument("--input", action="append", dest="inputs", default=[])
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--write-state", action="store_true", default=False)
    args = parser.parse_args(argv)
    write_state = bool(args.write_state)
    state = reduce_persistent_mind_loop(input_paths=args.inputs, state_dir=args.state_dir, write_state=write_state)
    print(json.dumps(state.to_json(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
