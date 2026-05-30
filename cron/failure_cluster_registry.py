"""Failure cluster registry for self-improvement reflection.

Tracks repeated failure patterns from API errors and cron job failures,
clusters them by semantic pattern, and emits self_improvement events when
the same failure type repeats beyond a threshold.

This produces policy_candidate events that guide Hermes autonomy improvements.
"""

from __future__ import annotations

import json
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

# Use hermes_constants from the parent directory if available
try:
    from hermes_constants import get_hermes_home
except ImportError:
    import sys
    from pathlib import Path as _Path
    # Try to import from parent hermes_agent directory
    _parent = _Path(__file__).parent.parent
    sys.path.insert(0, str(_parent))
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        get_hermes_home = lambda: _Path.home() / ".hermes"

# Threshold for emitting a self_improvement event when pattern repeats
DEFAULT_CLUSTER_THRESHOLD = 3

# High-signal failure patterns that deserve policy attention
HIGH_SIGNAL_FAILURE_TYPES = {
    "rate_limit": {
        "keywords": ["rate limit", "rate_limit", "too many requests", "throttled"],
        "why_it_matters": "Frequent rate limiting indicates Hermes is hitting provider quotas too aggressively.",
        "actionable_followup": "Consider implementing exponential backoff with jitter between requests, or adding provider-level rate limiting that respects token bucket quotas.",
    },
    "auth": {
        "keywords": ["auth", "unauthorized", "401", "403", "invalid token", "invalid credentials"],
        "why_it_matters": "Repeated auth failures suggest credential pool rotation or refresh issues.",
        "actionable_followup": "The credential pool may need more frequent rotation, or the provider refresh mechanism may need improvement.",
    },
    "billing": {
        "keywords": ["billing", "quota", "insufficient", "balance", "credit", "funds"],
        "why_it_matters": "Billing failures halt Hermes autonomy and require operator intervention.",
        "actionable_followup": "Add proactive balance monitoring with alerts before funds are exhausted.",
    },
    "timeout": {
        "keywords": ["timeout", "timed out", "deadline", "stale"],
        "why_it_matters": "Timeouts indicate network instability or provider slowness.",
        "actionable_followup": "Consider adding circuit breakers for consistently slow endpoints, or increasing per-call timeouts for large-context requests.",
    },
    "context_overflow": {
        "keywords": ["context", "token", "overflow", "max context", "exceed context"],
        "why_it_matters": "Context overflow reveals compression or trimming issues in Hermes memory management.",
        "actionable_followup": "Improve context compression, add sliding window eviction, or implement request-level contextbudgeting.",
    },
    "model_not_found": {
        "keywords": ["model not found", "invalid model", "404"],
        "why_it_matters": "Model routing misconfiguration or provider catalog drift.",
        "actionable_followup": "Add model availability pre-flight checks or fallback routing that adapts to provider catalog changes.",
    },
}


@dataclass
class FailureCluster:
    """A cluster of similar failures that should trigger self-improvement attention."""

    failure_type: str
    pattern_hash: str
    first_seen: float
    last_seen: float
    count: int
    examples: list[str] = field(default_factory=list)
    last_error: Optional[str] = None


@dataclass
class FailureClusterRegistry:
    """Registry for tracking failure clusters across Hermes runs."""

    clusters: Dict[str, FailureCluster] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_failure(
        self,
        *,
        error_message: str,
        failure_type: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[FailureCluster]:
        """Record a failure and return the updated cluster if threshold reached."""

        # Sanitize error message for pattern matching
        error_lower = error_message.lower().strip()

        # Classify the failure pattern
        detected_type = failure_type or _classify_failure_pattern(error_lower)

        # Create a normalized pattern hash
        pattern_key = self._make_pattern_key(detected_type, error_lower)

        with self._lock:
            if pattern_key in self.clusters:
                cluster = self.clusters[pattern_key]
                cluster.count += 1
                cluster.last_seen = time.time()
                if len(cluster.examples) < 3:  # Keep up to 3 examples per cluster
                    cluster.examples.append(error_message)
                cluster.last_error = error_message
            else:
                # Create new cluster
                cluster = FailureCluster(
                    failure_type=detected_type,
                    pattern_hash=pattern_key,
                    first_seen=time.time(),
                    last_seen=time.time(),
                    count=1,
                    examples=[error_message] if error_message else [],
                    last_error=error_message,
                )
                self.clusters[pattern_key] = cluster

            # Check if threshold reached
            if cluster.count >= DEFAULT_CLUSTER_THRESHOLD:
                return cluster

        return None

    def _make_pattern_key(self, failure_type: str, error_lower: str) -> str:
        """Create a stable hash key for the failure pattern."""
        # Use failure_type + first 50 chars of error for dedup
        base = f"{failure_type}:{error_lower[:50]}"
        return base.replace("\n", " ").replace("\r", " ")[:100]

    def get_clusters_above_threshold(self) -> list[FailureCluster]:
        """Return all clusters that have exceeded the threshold."""
        with self._lock:
            return [
                c for c in self.clusters.values()
                if c.count >= DEFAULT_CLUSTER_THRESHOLD
            ]

    def clear_cluster(self, pattern_key: str) -> None:
        """Clear a specific cluster after emitting a self_improvement event."""
        with self._lock:
            self.clusters.pop(pattern_key, None)

    def clear_all(self) -> None:
        """Clear all clusters (called after emitting self_improvement events)."""
        with self._lock:
            self.clusters.clear()

    def to_dict(self) -> dict[str, Any]:
        """Serialize registry state for persistence."""
        with self._lock:
            return {
                "clusters": {
                    k: {
                        "failure_type": v.failure_type,
                        "pattern_hash": v.pattern_hash,
                        "first_seen": v.first_seen,
                        "last_seen": v.last_seen,
                        "count": v.count,
                        "examples": v.examples,
                        "last_error": v.last_error,
                    }
                    for k, v in self.clusters.items()
                }
            }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FailureClusterRegistry":
        """Deserialize registry state."""
        registry = cls()
        clusters_data = data.get("clusters", {})
        for pattern_key, cluster_data in clusters_data.items():
            registry.clusters[pattern_key] = FailureCluster(
                failure_type=cluster_data.get("failure_type", "unknown"),
                pattern_hash=cluster_data.get("pattern_hash", pattern_key),
                first_seen=cluster_data.get("first_seen", time.time()),
                last_seen=cluster_data.get("last_seen", time.time()),
                count=cluster_data.get("count", 0),
                examples=cluster_data.get("examples", []),
                last_error=cluster_data.get("last_error"),
            )
        return registry


def _classify_failure_pattern(error_lower: str) -> str:
    """Classify an error message into a failure type based on keywords."""
    for fail_type, config in HIGH_SIGNAL_FAILURE_TYPES.items():
        for keyword in config["keywords"]:
            if keyword.lower() in error_lower:
                return fail_type
    return "unknown"


def get_registry_path() -> Path:
    """Return the path to the failure cluster registry file."""
    return get_hermes_home() / "state" / "mind" / "failure_clusters.json"


def load_registry() -> FailureClusterRegistry:
    """Load the failure cluster registry from disk."""
    path = get_registry_path()
    if not path.exists():
        return FailureClusterRegistry()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return FailureClusterRegistry.from_dict(data)
    except Exception:
        return FailureClusterRegistry()


def save_registry(registry: FailureClusterRegistry) -> None:
    """Save the failure cluster registry to disk."""
    path = get_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry.to_dict(), indent=2), encoding="utf-8")


def emit_policy_candidate_event(
    cluster: FailureCluster,
    source: str = "failure_cluster_registry",
) -> dict[str, Any]:
    """Emit a policy_candidate self_improvement event for a cluster."""

    from hermes_cli import mind_events

    config = HIGH_SIGNAL_FAILURE_TYPES.get(cluster.failure_type, {})

    why_it_matters = config.get(
        "why_it_matters",
        f"Repeated {cluster.failure_type} failures suggest systematic issues with Hermes autonomy.",
    )
    actionable_followup = config.get(
        "actionable_followup",
        f"Audit {cluster.failure_type} recovery patterns and consider updating Hermes routing logic.",
    )

    return mind_events.append_event(
        source=source,
        kind="self_improvement_signal",
        event_type="policy_candidate",
        category="self_improvement",
        summary=f"Failure cluster detected: {cluster.failure_type} ({cluster.count}x)",
        rationale=f"Cluster pattern hash: {cluster.pattern_hash}. Examples: {cluster.examples[:2]}",
        why_it_matters=why_it_matters,
        confidence_label="high",
        urgency="needs_review",
        autonomy_quality="failed_recovery_needed",
        next_best_action="create_task",
        metadata={
            "failure_type": cluster.failure_type,
            "count": cluster.count,
            "pattern_hash": cluster.pattern_hash,
            "first_seen": cluster.first_seen,
            "examples": cluster.examples[:3],
            "last_error": cluster.last_error,
            "actionable_followup": actionable_followup,
        },
    )
