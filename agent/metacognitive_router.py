"""Passive metacognitive routing prototype for Hermes Agent.

This module is intentionally side-effect-light: it only evaluates structured
state and, when explicitly called, writes redacted local JSONL records under the
active profile's Hermes home. It does not send messages, retry operations,
modify gateway behavior, or restart services.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from hermes_constants import get_config_path, get_hermes_home


FEATURE_FLAG_NAME = "metacognitive_router.enabled"
_CANONICAL_APPROVAL_BOARD = "kryden-50k-mrr"
_SAFE_DEFAULT_ENABLED = False
_SAFE_DEFAULT_DRY_RUN = True
_BOOLEAN_TRUE_STRINGS = {"true", "yes", "on", "1"}
_BOOLEAN_FALSE_STRINGS = {"false", "no", "off", "0"}
_SENSITIVE_KEYS = {
    "token",
    "secret",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "authorization",
    "auth",
    "cookie",
    "email",
    "access_token",
    "refresh_token",
    "bearer",
    "credential",
    "private_key",
}
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE)
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_SECRET_LIKE_RE = re.compile(
    r"\b(?:token|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|private[_-]?key)\b\s*[:=]\s*\S+",
    re.IGNORECASE,
)


class IntentRisk(str, Enum):
    """Risk tier for the requested action."""

    INTERNAL = "internal"
    EXTERNAL = "external"
    PUBLIC = "public"
    SPEND = "spend"
    ACCOUNT = "account"
    PRODUCTION = "production"
    CORE_RUNTIME = "core_runtime"


_GATED_BEHAVIOR_KIND_MARKERS: tuple[str, ...] = (
    "account",
    "client",
    "core",
    "credential",
    "deploy",
    "external",
    "legal",
    "merge",
    "money",
    "outreach",
    "post",
    "production",
    "publish",
    "publishing",
    "purchase",
    "reputation",
    "restart",
    "spend",
)

_GATED_BEHAVIOR_TEXT_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:"
    + "|".join(re.escape(marker) for marker in _GATED_BEHAVIOR_KIND_MARKERS)
    + r")(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


class FailureMode(str, Enum):
    """Deterministic failure categories observed by the passive router."""

    MISSING_OUTCOME_EVIDENCE = "missing_outcome_evidence"
    GATEWAY_DELIVERY_GAP = "gateway_delivery_gap"
    APPROVAL_REQUIRED = "approval_required"
    SPAM_BUDGET_EXHAUSTED = "spam_budget_exhausted"
    WORKER_STALL = "worker_stall"
    KANBAN_NOTIFICATION_GAP = "kanban_notification_gap"
    SENSITIVE_PRIVACY = "sensitive_privacy"


class RouteAction(str, Enum):
    """Passive dry-run recommendation returned by the router."""

    NOOP_SUCCESS = "noop_success"
    PASSIVE_STATUS_CHECK = "passive_status_check"
    PASSIVE_GATEWAY_FALLBACK = "passive_gateway_fallback"
    BLOCK_FOR_APPROVAL = "block_for_approval"
    SUPPRESS_SPAM = "suppress_spam"
    PASSIVE_KANBAN_STATUS_CHECK = "passive_kanban_status_check"
    PASSIVE_SUBSCRIPTION_CHECK = "passive_subscription_check"
    BLOCK_PRIVACY_REVIEW = "block_privacy_review"


@dataclass(frozen=True)
class RouterFeatureConfig:
    """Feature gate for the prototype.

    ``enabled`` defaults false. ``dry_run`` defaults true and is intentionally
    orthogonal: Phase 1/2 callers may collect local records when explicitly
    enabled, but this module still never performs external side effects.
    """

    enabled: bool = False
    dry_run: bool = True
    record_tool_results: bool = False
    behavior_routing_enabled: bool = False
    allow_internal_kanban: bool = False
    allow_internal_execution_recommendations: bool = False
    approval_board: str = _CANONICAL_APPROVAL_BOARD
    approval_assignee: str = "builder"
    internal_board: str | None = None
    internal_assignee: str = "builder"
    flag_name: str = FEATURE_FLAG_NAME


@dataclass(frozen=True)
class BehaviorActionCandidate:
    """Typed candidate for behavior-changing metacognitive routing."""

    kind: str
    title: str
    body: str
    risk: IntentRisk = IntentRisk.INTERNAL
    requires_approval: bool = False
    evidence: tuple[str, ...] = ()
    default_fallback: str = "Do nothing until a human approves."
    approval_options: tuple[str, ...] = ("approve", "reject", "revise")
    adjacent_safe_prep: tuple[str, ...] = ()
    execute_recommendation: str | None = None
    idempotency_key: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BehaviorRouteResult:
    """Result of routing a behavior-changing metacognitive candidate."""

    route: str
    idempotency_key: str
    created_task_id: str | None = None
    kanban_payload: dict[str, Any] | None = None
    execute_recommendation: str | None = None
    external_executed: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Intent:
    """Structured description of what Hermes was trying to do."""

    kind: str
    risk: IntentRisk = IntentRisk.INTERNAL
    requires_approval: bool = False
    sensitive: bool = False
    max_notifications: int = 3
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Attempt:
    """Structured result of a concrete action attempt.

    ``None`` means unknown/not observed; this is distinct from ``False``.
    """

    action_success: bool | None = None
    delivery_success: bool | None = None
    outcome_success: bool | None = None
    sent_notifications: int = 0
    status: str | None = None
    channel: str | None = None


@dataclass(frozen=True)
class Evidence:
    """Evidence available to evaluate the attempt."""

    outcome_present: bool | None = None
    delivery_confirmed: bool | None = None
    approval_present: bool = False
    fallback_available: bool = False
    no_change: bool = False
    last_heartbeat_age_seconds: int | None = None
    has_notification_subscription: bool | None = None
    sensitive_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteDecision:
    """Deterministic passive routing decision.

    ``external_allowed`` is false for every Phase 1/2 decision; the prototype
    recommends local checks/blocks only.
    """

    action: RouteAction
    failure_modes: tuple[FailureMode, ...] = ()
    passive_only: bool = True
    external_allowed: bool = False
    should_record: bool = True
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return _to_jsonable(self)


def load_feature_config(config: Any) -> RouterFeatureConfig:
    """Load the metacognitive router feature gate from a config dict.

    Expected shape::

        metacognitive_router:
          enabled: false
          dry_run: true

    Missing or malformed values fall back to safest defaults.
    """

    root = config if isinstance(config, dict) else {}
    section = root.get("metacognitive_router", {})
    if not isinstance(section, dict):
        section = {}
    record_paths = section.get("record_paths", {})
    if not isinstance(record_paths, dict):
        record_paths = {}
    behavior_routing = section.get("behavior_routing", {})
    if not isinstance(behavior_routing, dict):
        behavior_routing = {}
    approval_board = _CANONICAL_APPROVAL_BOARD
    approval_assignee = behavior_routing.get("approval_assignee", "builder")
    if not isinstance(approval_assignee, str) or not approval_assignee.strip():
        approval_assignee = "builder"
    internal_board = behavior_routing.get("internal_board")
    if not isinstance(internal_board, str) or not internal_board.strip():
        internal_board = None
    internal_assignee = behavior_routing.get("internal_assignee", "builder")
    if not isinstance(internal_assignee, str) or not internal_assignee.strip():
        internal_assignee = "builder"
    return RouterFeatureConfig(
        enabled=_parse_bool(section.get("enabled"), default=_SAFE_DEFAULT_ENABLED),
        dry_run=_parse_bool(section.get("dry_run"), default=_SAFE_DEFAULT_DRY_RUN),
        record_tool_results=_parse_bool(
            record_paths.get("tool_results"),
            default=False,
        ),
        behavior_routing_enabled=_parse_bool(behavior_routing.get("enabled"), default=False),
        allow_internal_kanban=_parse_bool(behavior_routing.get("allow_internal_kanban"), default=False),
        allow_internal_execution_recommendations=_parse_bool(
            behavior_routing.get("allow_internal_execution_recommendations"),
            default=False,
        ),
        approval_board=approval_board.strip(),
        approval_assignee=approval_assignee.strip(),
        internal_board=internal_board.strip() if internal_board else None,
        internal_assignee=internal_assignee.strip(),
    )


def load_feature_config_from_file(path: str | Path | None = None) -> RouterFeatureConfig:
    """Load feature config from config.yaml, falling back to safe defaults."""

    config_path = Path(path) if path is not None else get_config_path()
    try:
        import yaml

        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        raw = {}
    return load_feature_config(raw)


def maybe_record_tool_result(
    *,
    tool_name: str,
    args: Any,
    result: Any,
    action_success: bool,
    task_id: str | None = None,
    session_id: str | None = None,
    tool_call_id: str | None = None,
    duration_ms: int | None = None,
    status: str | None = None,
    config: Any = None,
    path: str | Path | None = None,
) -> Path | None:
    """Append a dry-run router event for a tool result when explicitly enabled.

    Recording is gated by all three safe flags: ``enabled``, ``dry_run``, and
    ``record_paths.tool_results``. It is fail-open for tool execution: disabled
    or failed recording returns ``None`` and never mutates the tool result.
    """

    feature_config = load_feature_config_from_file() if config is None else load_feature_config(config)
    if not (
        feature_config.enabled
        and feature_config.dry_run
        and feature_config.record_tool_results
    ):
        return None

    intent = Intent(
        kind="tool_call_result",
        risk=IntentRisk.INTERNAL,
        context={
            "tool_name": tool_name,
            "tool_args": args,
            "result": result,
            "result_shape": _result_shape(result),
            "task_id": task_id,
            "session_id": session_id,
            "tool_call_id": tool_call_id,
            "duration_ms": duration_ms,
        },
    )
    attempt = Attempt(
        action_success=action_success,
        outcome_success=action_success,
        status=status or ("completed" if action_success else "error"),
    )
    evidence = Evidence(outcome_present=result is not None)
    decision = evaluate_route(intent, attempt, evidence)

    try:
        return append_decision_jsonl(intent, attempt, evidence, decision, path)
    except Exception:
        return None


def route_behavior_change(
    candidate: BehaviorActionCandidate,
    *,
    config: Any = None,
    kanban_create: Callable[..., Any] | None = None,
) -> BehaviorRouteResult:
    """Route behavior-changing metacognitive candidates through safe sinks.

    This function never executes external actions itself. Public/reputation/
    money/account/credential/legal/production/client/core-style risk tiers are
    converted into blocked Kanban approval-card payloads on the canonical
    ``kryden-50k-mrr`` board when behavior routing is explicitly enabled and
    dry-run is disabled. Safe internal actions require separate opt-in flags for
    internal Kanban creation or local execute recommendations.
    """

    feature_config = load_feature_config_from_file() if config is None else load_feature_config(config)
    idempotency_key = candidate.idempotency_key or _behavior_idempotency_key(candidate)
    if not (feature_config.enabled and feature_config.behavior_routing_enabled):
        return BehaviorRouteResult(
            route="disabled",
            idempotency_key=idempotency_key,
            reason="metacognitive behavior routing is disabled",
        )
    if feature_config.dry_run:
        return BehaviorRouteResult(
            route="dry_run",
            idempotency_key=idempotency_key,
            reason="metacognitive behavior routing dry_run is enabled",
        )

    if _requires_behavior_human_approval(candidate):
        payload = _approval_kanban_payload(candidate, feature_config, idempotency_key)
        created_task_id = _call_kanban_create(kanban_create, payload)
        return BehaviorRouteResult(
            route="blocked_approval_kanban",
            idempotency_key=idempotency_key,
            created_task_id=created_task_id,
            kanban_payload=payload,
            reason="gated risk routed to blocked Kanban approval card",
        )

    if candidate.execute_recommendation and feature_config.allow_internal_execution_recommendations:
        return BehaviorRouteResult(
            route="internal_execute_recommendation",
            idempotency_key=idempotency_key,
            execute_recommendation=candidate.execute_recommendation,
            reason="safe internal execute recommendation returned under explicit flag",
        )

    if feature_config.allow_internal_kanban:
        payload = _internal_kanban_payload(candidate, feature_config, idempotency_key)
        created_task_id = _call_kanban_create(kanban_create, payload)
        return BehaviorRouteResult(
            route="internal_kanban",
            idempotency_key=idempotency_key,
            created_task_id=created_task_id,
            kanban_payload=payload,
            reason="safe internal action routed to Kanban under explicit flag",
        )

    return BehaviorRouteResult(
        route="internal_policy_noop",
        idempotency_key=idempotency_key,
        reason="safe internal behavior routing has no enabled sink",
    )


def _call_kanban_create(kanban_create: Callable[..., Any] | None, payload: dict[str, Any]) -> str | None:
    if kanban_create is None:
        return None
    result = kanban_create(**payload)
    if isinstance(result, dict):
        task_id = result.get("task_id") or result.get("id")
        return str(task_id) if task_id else None
    if isinstance(result, str):
        return result
    return None


def _approval_kanban_payload(
    candidate: BehaviorActionCandidate,
    config: RouterFeatureConfig,
    idempotency_key: str,
) -> dict[str, Any]:
    reason = f"approval-required: {candidate.risk.value} action requires Coop approval"
    body = "\n".join(
        [
            candidate.body,
            "",
            reason,
            f"Risk: {candidate.risk.value}",
            _bullet_section("Evidence", candidate.evidence),
            _bullet_section("Adjacent safe prep", candidate.adjacent_safe_prep),
            f"Default fallback: {candidate.default_fallback}",
            _bullet_section("Approval options", candidate.approval_options),
        ]
    )
    return {
        "title": f"approval-required: {candidate.title}",
        "body": body,
        "assignee": config.approval_assignee,
        "board": _CANONICAL_APPROVAL_BOARD,
        "initial_status": "blocked",
        "idempotency_key": idempotency_key,
    }


def _internal_kanban_payload(
    candidate: BehaviorActionCandidate,
    config: RouterFeatureConfig,
    idempotency_key: str,
) -> dict[str, Any]:
    body = candidate.body
    if candidate.evidence:
        body += "\n\n" + _bullet_section("Metacognitive internal routing evidence", candidate.evidence)
    payload = {
        "title": candidate.title,
        "body": body,
        "assignee": config.internal_assignee,
        "initial_status": "running",
        "idempotency_key": idempotency_key,
    }
    if config.internal_board:
        payload["board"] = config.internal_board
    return payload


def _bullet_section(title: str, items: tuple[str, ...]) -> str:
    if not items:
        return f"{title}:\n- none"
    return f"{title}:\n" + "\n".join(f"- {item}" for item in items)


def _behavior_idempotency_key(candidate: BehaviorActionCandidate) -> str:
    raw = json.dumps(
        {
            "kind": candidate.kind,
            "title": candidate.title,
            "risk": candidate.risk.value,
            "body": candidate.body,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "metacognitive-approval:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _parse_bool(value: Any, *, default: bool) -> bool:
    """Parse a strict boolean config value with an explicit safe default."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _BOOLEAN_TRUE_STRINGS:
            return True
        if normalized in _BOOLEAN_FALSE_STRINGS:
            return False
    return default


def _result_shape(result: Any) -> dict[str, Any]:
    """Return a compact JSON-safe description of a tool result's shape."""

    parsed = result
    parsed_json = False
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            parsed_json = True
        except json.JSONDecodeError:
            parsed = result
    if isinstance(parsed, dict):
        return {
            "type": "object",
            "json": parsed_json,
            "keys": sorted(str(key) for key in parsed.keys())[:20],
        }
    if isinstance(parsed, list):
        return {"type": "array", "json": parsed_json, "length": len(parsed)}
    return {"type": type(parsed).__name__, "json": parsed_json}


def default_state_path() -> Path:
    """Return the profile-safe local JSONL path for passive router state."""

    return get_hermes_home() / "state" / "metacognitive_router" / "events.jsonl"


def evaluate_route(intent: Intent, attempt: Attempt, evidence: Evidence) -> RouteDecision:
    """Evaluate a deterministic passive route decision.

    Precedence is safety-first:
    privacy > approval gates > spam budget > stalls/subscription/delivery gaps
    > missing outcome evidence > no-op success.
    """

    if intent.sensitive or evidence.sensitive_fields:
        return RouteDecision(
            action=RouteAction.BLOCK_PRIVACY_REVIEW,
            failure_modes=(FailureMode.SENSITIVE_PRIVACY,),
            reason="sensitive context requires privacy review before any external route",
        )

    if _requires_human_approval(intent) and not evidence.approval_present:
        return RouteDecision(
            action=RouteAction.BLOCK_FOR_APPROVAL,
            failure_modes=(FailureMode.APPROVAL_REQUIRED,),
            reason="risk tier or explicit intent requires human approval",
        )

    if attempt.sent_notifications >= max(intent.max_notifications, 0):
        return RouteDecision(
            action=RouteAction.SUPPRESS_SPAM,
            failure_modes=(FailureMode.SPAM_BUDGET_EXHAUSTED,),
            reason="notification budget exhausted; suppressing passive fallback",
        )

    if (
        attempt.status == "running"
        and evidence.last_heartbeat_age_seconds is not None
        and evidence.last_heartbeat_age_seconds >= 3600
    ):
        return RouteDecision(
            action=RouteAction.PASSIVE_KANBAN_STATUS_CHECK,
            failure_modes=(FailureMode.WORKER_STALL,),
            reason="worker has been running without recent heartbeat",
        )

    if (
        intent.kind.startswith("kanban")
        and evidence.has_notification_subscription is False
    ):
        return RouteDecision(
            action=RouteAction.PASSIVE_SUBSCRIPTION_CHECK,
            failure_modes=(FailureMode.KANBAN_NOTIFICATION_GAP,),
            reason="kanban event lacks a notification subscription path",
        )

    if attempt.delivery_success is False or evidence.delivery_confirmed is False:
        action = (
            RouteAction.PASSIVE_GATEWAY_FALLBACK
            if evidence.fallback_available
            else RouteAction.PASSIVE_STATUS_CHECK
        )
        return RouteDecision(
            action=action,
            failure_modes=(FailureMode.GATEWAY_DELIVERY_GAP,),
            reason="delivery evidence is missing or negative",
        )

    if evidence.no_change and attempt.outcome_success is True:
        return RouteDecision(action=RouteAction.NOOP_SUCCESS, reason="quiet no-change success")

    if attempt.action_success is True and attempt.delivery_success is True and not evidence.outcome_present:
        return RouteDecision(
            action=RouteAction.PASSIVE_STATUS_CHECK,
            failure_modes=(FailureMode.MISSING_OUTCOME_EVIDENCE,),
            reason="action and delivery succeeded but outcome evidence is missing",
        )

    if attempt.outcome_success is True:
        return RouteDecision(action=RouteAction.NOOP_SUCCESS, reason="outcome success observed")

    return RouteDecision(
        action=RouteAction.PASSIVE_STATUS_CHECK,
        failure_modes=(FailureMode.MISSING_OUTCOME_EVIDENCE,),
        reason="outcome evidence is incomplete",
    )


def append_decision_jsonl(
    intent: Intent,
    attempt: Attempt,
    evidence: Evidence,
    decision: RouteDecision,
    path: str | Path | None = None,
) -> Path:
    """Append one redacted passive router event to local JSONL state."""

    out_path = Path(path) if path is not None else default_state_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = redact_payload(
        {
            "intent": _to_jsonable(intent),
            "attempt": _to_jsonable(attempt),
            "evidence": _to_jsonable(evidence),
            "decision": decision.to_json(),
        },
        evidence.sensitive_fields,
    )
    with out_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return out_path


def replay_jsonl(path: str | Path | None = None) -> dict[str, Any]:
    """Replay local router JSONL and return aggregate counts."""

    in_path = Path(path) if path is not None else default_state_path()
    actions: dict[str, int] = {}
    failures: dict[str, int] = {}
    total = 0
    invalid_lines = 0
    if not in_path.exists():
        return {"path": str(in_path), "total": 0, "invalid_lines": 0, "actions": {}, "failure_modes": {}}

    with in_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                invalid_lines += 1
                continue
            if not isinstance(event, dict):
                invalid_lines += 1
                continue
            total += 1
            decision = event.get("decision", {})
            if not isinstance(decision, dict):
                decision = {}
            action = decision.get("action", "unknown")
            if not isinstance(action, str):
                action = "unknown"
            actions[action] = actions.get(action, 0) + 1
            failure_modes = decision.get("failure_modes", [])
            if not isinstance(failure_modes, list):
                failure_modes = []
            for mode in failure_modes:
                if not isinstance(mode, str):
                    continue
                failures[mode] = failures.get(mode, 0) + 1
    return {
        "path": str(in_path),
        "total": total,
        "invalid_lines": invalid_lines,
        "actions": actions,
        "failure_modes": failures,
    }


def status_summary(path: str | Path | None = None) -> dict[str, Any]:
    """Return a compact status summary for the replay harness."""

    replay = replay_jsonl(path)
    actions = replay["actions"]
    needs_review = sum(
        count
        for action, count in actions.items()
        if action.startswith("block_") or action == RouteAction.SUPPRESS_SPAM.value
    )
    return {**replay, "needs_review": needs_review}


def redact_payload(payload: Any, sensitive_fields: Any = ()) -> Any:
    """Redact sensitive field names and scalar values before local storage."""

    sensitive = _SENSITIVE_KEYS | _normalize_sensitive_fields(sensitive_fields)
    visited: set[int] = set()

    if (
        isinstance(payload, dict)
        and isinstance(payload.get("intent"), dict)
        and payload["intent"].get("sensitive") is True
        and "context" in payload["intent"]
    ):
        payload = dict(payload)
        payload["intent"] = dict(payload["intent"])
        payload["intent"]["context"] = "[REDACTED_CONTEXT]"

    def walk(value: Any, key: Any = None) -> Any:
        if _is_sensitive_key(key, sensitive):
            return "[REDACTED]"
        if isinstance(value, dict):
            value_id = id(value)
            if value_id in visited:
                return "[CYCLE]"
            visited.add(value_id)
            try:
                return {_json_key(k): walk(v, k) for k, v in value.items()}
            finally:
                visited.remove(value_id)
        if isinstance(value, list):
            value_id = id(value)
            if value_id in visited:
                return "[CYCLE]"
            visited.add(value_id)
            try:
                return [walk(v) for v in value]
            finally:
                visited.remove(value_id)
        if isinstance(value, tuple):
            value_id = id(value)
            if value_id in visited:
                return "[CYCLE]"
            visited.add(value_id)
            try:
                return [walk(v) for v in value]
            finally:
                visited.remove(value_id)
        if isinstance(value, (set, frozenset)):
            value_id = id(value)
            if value_id in visited:
                return "[CYCLE]"
            visited.add(value_id)
            try:
                return [walk(v) for v in _sorted_iterable(value)]
            finally:
                visited.remove(value_id)
        if isinstance(value, str) and _looks_sensitive(value):
            return "[REDACTED]"
        jsonable = _to_jsonable(value, visited=visited)
        if jsonable is value:
            return jsonable
        return walk(jsonable, key)

    return walk(payload)


def _normalize_sensitive_fields(sensitive_fields: Iterable[str] | Any) -> set[str]:
    """Return lower-cased custom sensitive field names without raising."""

    if isinstance(sensitive_fields, (str, bytes, bytearray, memoryview)):
        fields = (sensitive_fields,)
    else:
        try:
            fields = tuple(sensitive_fields)
        except TypeError:
            fields = ()
    normalized: set[str] = set()
    for field_name in fields:
        normalized.update(_normalized_key_names(field_name))
    return normalized


def _requires_behavior_human_approval(candidate: BehaviorActionCandidate) -> bool:
    intent = Intent(
        kind=candidate.kind,
        risk=candidate.risk,
        requires_approval=candidate.requires_approval,
        context=candidate.context,
    )
    if _requires_human_approval(intent):
        return True
    return _contains_gated_behavior_marker(
        {
            "kind": candidate.kind,
            "title": candidate.title,
            "body": candidate.body,
            "context": candidate.context,
        }
    )


def _requires_human_approval(intent: Intent) -> bool:
    gated = {
        IntentRisk.EXTERNAL,
        IntentRisk.PUBLIC,
        IntentRisk.SPEND,
        IntentRisk.ACCOUNT,
        IntentRisk.PRODUCTION,
        IntentRisk.CORE_RUNTIME,
    }
    return (
        intent.requires_approval
        or intent.risk in gated
        or _contains_gated_behavior_marker({"kind": intent.kind, "context": intent.context})
    )


def _contains_gated_behavior_marker(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return False
    if isinstance(value, Enum):
        return _contains_gated_behavior_marker(value.value) or _contains_gated_behavior_marker(value.name)
    if isinstance(value, str):
        return bool(_GATED_BEHAVIOR_TEXT_RE.search(value.replace("_", " ").replace("-", " ")))
    if isinstance(value, dict):
        return any(
            _contains_gated_behavior_marker(key) or _contains_gated_behavior_marker(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_gated_behavior_marker(item) for item in value)
    return False


def _looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    return bool(
        _EMAIL_RE.search(value)
        or _BEARER_RE.search(value)
        or _SK_KEY_RE.search(value)
        or _SECRET_LIKE_RE.search(value)
        or any(marker in lowered for marker in ("token", "secret", "api_key", "apikey"))
    )


def _is_sensitive_key(key: Any, sensitive: set[str]) -> bool:
    if key is None:
        return False
    key_names = _normalized_key_names(key)
    return any(name in sensitive or any(marker in name for marker in _SENSITIVE_KEYS) for name in key_names)


def _normalized_key_names(key: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(key, str):
        names.add(key.lower())
    elif isinstance(key, (bytes, bytearray, memoryview)):
        raw = bytes(key)
        names.add(raw.decode("utf-8", errors="replace").lower())
        names.add(str(raw).lower())
    elif isinstance(key, Enum):
        names.add(str(key.value).lower())
        names.add(key.name.lower())
    elif key is not None:
        try:
            names.add(str(key).lower())
        except Exception:
            names.add(type(key).__name__.lower())
    return names


def _json_key(key: Any) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, Enum):
        return str(key.value)
    if isinstance(key, (bytes, bytearray, memoryview)):
        return "[BYTES_KEY_REDACTED]"
    if key is None or isinstance(key, (bool, int, float)):
        return str(key)
    if isinstance(key, Path):
        return str(key)
    return f"[UNSERIALIZABLE_KEY:{type(key).__name__}]"


def _to_jsonable(value: Any, visited: set[int] | None = None) -> Any:
    if visited is None:
        visited = set()
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[BYTES_REDACTED]"
    if is_dataclass(value) and not isinstance(value, type):
        value_id = id(value)
        if value_id in visited:
            return "[CYCLE]"
        visited.add(value_id)
        try:
            return {field_def.name: _to_jsonable(getattr(value, field_def.name), visited) for field_def in fields(value)}
        finally:
            visited.remove(value_id)
    if isinstance(value, dict):
        value_id = id(value)
        if value_id in visited:
            return "[CYCLE]"
        visited.add(value_id)
        try:
            return {_json_key(k): _to_jsonable(v, visited) for k, v in value.items()}
        finally:
            visited.remove(value_id)
    if isinstance(value, (set, frozenset)):
        value_id = id(value)
        if value_id in visited:
            return "[CYCLE]"
        visited.add(value_id)
        try:
            return [_to_jsonable(v, visited) for v in _sorted_iterable(value)]
        finally:
            visited.remove(value_id)
    if isinstance(value, tuple):
        value_id = id(value)
        if value_id in visited:
            return "[CYCLE]"
        visited.add(value_id)
        try:
            return [_to_jsonable(v, visited) for v in value]
        finally:
            visited.remove(value_id)
    if isinstance(value, list):
        value_id = id(value)
        if value_id in visited:
            return "[CYCLE]"
        visited.add(value_id)
        try:
            return [_to_jsonable(v, visited) for v in value]
        finally:
            visited.remove(value_id)
    return f"[UNSERIALIZABLE:{type(value).__name__}]"


def _sorted_iterable(values: Iterable[Any]) -> list[Any]:
    """Sort arbitrary values deterministically for JSON persistence."""

    return sorted(values, key=_safe_sort_key)


def _safe_sort_key(item: Any) -> tuple[str, str]:
    if item is None or isinstance(item, (bool, int, float, str)):
        return (type(item).__name__, str(item))
    if isinstance(item, Enum):
        return (type(item).__name__, str(item.value))
    if isinstance(item, Path):
        return (type(item).__name__, str(item))
    if isinstance(item, (bytes, bytearray, memoryview)):
        return (type(item).__name__, "[BYTES]")
    return (type(item).__name__, "")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay passive metacognitive router JSONL state")
    parser.add_argument("command", choices=("replay", "status"))
    parser.add_argument("--path", type=Path, default=None)
    args = parser.parse_args(argv)
    result = replay_jsonl(args.path) if args.command == "replay" else status_summary(args.path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
