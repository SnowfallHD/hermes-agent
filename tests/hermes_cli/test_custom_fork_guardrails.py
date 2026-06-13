"""Guardrails for Coop-specific Hermes fork seams."""

from __future__ import annotations

from pathlib import Path

from hermes_cli.config import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parents[2]
SURFACE_MAP = ROOT / "docs" / "fork-custom-surface-map.md"
INTEGRATION_SCRIPT = ROOT / "scripts" / "run_upstream_integration_tests.sh"


def test_metacog_and_persistent_mind_defaults_are_inert():
    """Speculative autonomy surfaces must stay safe until explicitly enabled."""
    router = DEFAULT_CONFIG["metacognitive_router"]
    assert router["enabled"] is False
    assert router["dry_run"] is True
    assert router["record_paths"]["tool_results"] is False
    assert router["behavior_routing"]["enabled"] is False
    assert router["behavior_routing"]["allow_internal_kanban"] is False
    assert router["behavior_routing"]["allow_internal_execution_recommendations"] is False

    mind_loop = DEFAULT_CONFIG["persistent_mind_loop"]
    assert mind_loop["enabled"] is False
    assert mind_loop["dry_run"] is True
    assert mind_loop["write_state"] is False


def test_fork_custom_surface_map_tracks_required_risky_surfaces():
    text = SURFACE_MAP.read_text(encoding="utf-8")
    required_surfaces = [
        "Slack gateway routing + threaded delivery",
        "External memory provider canonicalization",
        "Cron/delegation memory inheritance",
        "Cron runtime-data scanner boundary",
        "Compression preflight diagnostics",
        "Metacognitive router + persistent mind loop",
        "Thoughts/Mind/Event dashboard",
        "Desktop/canonical conversation UX",
        "Upstream integration test harness",
    ]
    for surface in required_surfaces:
        assert surface in text

    required_selectors = [
        "tests/gateway/test_slack.py",
        "tests/agent/test_preflight_compression_diagnostics.py",
        "tests/hermes_cli/test_custom_fork_guardrails.py",
        "scripts/run_upstream_integration_tests.sh",
    ]
    for selector in required_selectors:
        assert selector in text


def test_upstream_integration_script_prefers_active_venv_before_stale_dotvenv():
    script = INTEGRATION_SCRIPT.read_text(encoding="utf-8")
    assert '"$ROOT/venv/bin/python"' in script
    assert '"$ROOT/.venv/bin/python"' in script
    assert script.index('"$ROOT/venv/bin/python"') < script.index('"$ROOT/.venv/bin/python"')
