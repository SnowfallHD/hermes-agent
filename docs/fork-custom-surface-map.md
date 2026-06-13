# SnowfallHD Hermes Fork Custom Surface Map

Purpose: keep Coop-specific Hermes behavior visible, testable, and easier to merge with upstream. This file maps custom surfaces by blast radius, source paths, invariants, and regression selectors. Update it whenever fork-only code creates or removes a custom runtime seam.

## Classification rules

- **Core runtime seam**: modifies routing, prompt assembly, session identity, model calls, memory authority, gateway dispatch, cron execution, or production process behavior. Needs focused tests before merge.
- **Adapter/policy seam**: platform-specific policy wrapped around a protocol adapter. Prefer small modules and config flags.
- **Plugin/extension seam**: default-off or separately mounted feature with bounded API/tool exposure. Prefer this shape for new custom behavior.
- **Documentation/tooling seam**: reports, scripts, runbooks, tests. Lowest runtime risk, but should stay accurate.

## Surface checklist

| Surface | Risk | Main paths | Required invariants | Focused selectors |
| --- | --- | --- | --- | --- |
| Slack gateway routing + threaded delivery | High core/adapter | `gateway/platforms/slack.py`, `gateway/platforms/slack_threaded_delivery.py`, `gateway/run.py`, `gateway/slash_commands.py`, `tests/gateway/test_slack.py` | Mention/thread routing remains hermetic from live Slack env; `!goal`/`!stop` work in threads; commands keep parseable `event.text`; parent/thread reply formatting does not fragment sessions unexpectedly. | `python -m pytest tests/gateway/test_slack.py tests/gateway/test_goal_max_turns_config.py tests/gateway/test_slack_threaded_delivery.py -q` |
| External memory provider canonicalization | High core prompt/memory | `agent/memory_policy.py`, `agent/system_prompt.py`, `agent/turn_context.py`, `plugins/memory/honcho/__init__.py`, `tests/agent/test_memory_policy.py`, `tests/agent/test_system_prompt.py` | External providers can be canonical; local `MEMORY.md`/`USER.md` are recent/additive when canonical; local memory nudge is suppressed in canonical mode; memory tool writes mirror durable targets to provider. | `python -m pytest tests/agent/test_memory_policy.py tests/agent/test_system_prompt.py tests/honcho_plugin/test_session.py -q` |
| Cron/delegation memory inheritance | High core worker/cron | `cron/scheduler.py`, `tools/delegate_tool.py`, `hermes_cli/config.py`, `tests/cron/test_cron_profile.py`, `tests/tools/test_delegate.py` | Cron/delegate memory inheritance remains config-gated; script-only `no_agent` jobs do not invoke agent models; profile/workdir isolation is preserved. | `python -m pytest tests/cron/test_cron_profile.py tests/tools/test_delegate.py -q` |
| Cron runtime-data scanner boundary | High security/correctness | `cron/scheduler.py`, `tools/cronjob_tools.py`, `tests/cron/test_cron_mind_event_classification.py` | Stored prompts/skills remain strictly scanned; runtime script/context output is treated as data, not granted instruction authority; suspicious runtime text does not block legitimate analysis or become trusted control. | `python -m pytest tests/cron/test_cron_mind_event_classification.py tests/cron/test_cron_profile.py -q` |
| Compression preflight diagnostics | Medium core ergonomics | `agent/turn_context.py`, `agent/conversation_compression.py`, `agent/context_compressor.py`, `tests/agent/test_preflight_compression_diagnostics.py`, `tests/run_agent/test_infinite_compaction_loop.py` | Back-to-back compression statuses are an explicit, capped preflight behavior; no-op/ineffective compression increments anti-thrash state; compression lineage remains distinct from Slack visual thread identity. | `python -m pytest tests/agent/test_preflight_compression_diagnostics.py tests/run_agent/test_infinite_compaction_loop.py tests/run_agent/test_compression_boundary_hook.py -q` |
| Metacognitive router + persistent mind loop | High if enabled, safe default-off | `agent/metacognitive_router.py`, `agent/persistent_mind_loop.py`, `hermes_cli/config.py`, `plugins/metacog_observer/__init__.py` | Router and persistent mind loop default disabled; dry-run defaults true; behavior routing and execution recommendations remain disabled unless explicitly configured; persistent writes default off. | `python -m pytest tests/hermes_cli/test_custom_fork_guardrails.py -q` |
| Thoughts/Mind/Event dashboard | Moderate extension/plugin | `hermes_cli/mind_events.py`, `tools/mind_events_tool.py`, `plugins/thoughts/dashboard/*`, `plugins/metacog_observer/*`, `hermes_cli/web_server.py` | Toolset exposure is opt-in/default-off; dashboard APIs return JSON not SPA fallback; reducers produce summaries from durable events, not hidden chain-of-thought. | `python -m pytest tests/plugins/test_thoughts_dashboard_plugin.py tests/cron/test_cron_mind_event_classification.py -q` |
| Desktop/canonical conversation UX | Moderate session UX | `hermes_state.py`, `tui_gateway/server.py`, `apps/desktop/src/*` | Compression-created physical session splits are grouped under a stable conversation identity; recency/resume uses the latest tip; synthetic compaction summaries are not treated as fresh user input. | `python -m pytest tests/test_hermes_state.py tests/test_tui_gateway_server.py -q` |
| Upstream integration test harness | Low tooling, high governance value | `scripts/run_upstream_integration_tests.sh`, `docs/fork-custom-surface-map.md` | Prefer active `venv/bin/python` over stale `.venv`; run focused custom-surface selectors; include new risky custom seams as they are added. | `bash scripts/run_upstream_integration_tests.sh` |

## Update protocol

1. Before editing a core runtime seam, add or identify a focused regression selector above.
2. Prefer moving custom policy into plugin/config/adapter modules instead of expanding upstream core files.
3. If a custom seam is default-off, add a test that proves the default remains inert.
4. If a live environment variable can change test behavior, make the test hermetic with `monkeypatch.delenv`.
5. Add the selector to `scripts/run_upstream_integration_tests.sh` when it should guard future upstream merges.
