#!/usr/bin/env bash
set -euo pipefail

# Reusable targeted suite for Coop's Hermes upstream-integration branch.
# Run after every upstream fetch/merge before merging integration back to fork main.
# Add new tests here whenever Coop-specific behavior moves behind plugins/config/extensions
# or an upstream merge touches a new custom surface.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in \
    "$ROOT/venv/bin/python" \
    "$HOME/.hermes/hermes-agent/venv/bin/python" \
    "$ROOT/.venv/bin/python" \
    python3 \
    python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "No Python interpreter found for upstream integration tests" >&2
  exit 127
fi

run_pytest() {
  local allow_no_tests=0
  if [[ "${1:-}" == "--allow-no-tests" ]]; then
    allow_no_tests=1
    shift
  fi
  set +e
  "$PYTHON_BIN" -m pytest "$@" -o 'addopts=' -q
  local status=$?
  set -e
  if [[ $status -eq 5 && $allow_no_tests -eq 1 ]]; then
    echo "pytest selected no runnable tests for optional dependency slice; continuing."
    return 0
  fi
  return "$status"
}

# Slack delivery/formatting: narrowly filtered because test_slack.py is large.
run_pytest \
  tests/gateway/test_slack_threaded_delivery.py \
  tests/gateway/test_slack.py \
  -k 'AutoParentThread or SlackFormatting or dm_toplevel_preserves_message_id_for_progress_threading or threaded_delivery'

# Slack approval/buttons and plaintext command affordances are custom-risk
# gateway surfaces: approval prompts must render buttons even for long commands,
# and DM-only plaintext approval/restart aliases must not leak into groups.
run_pytest \
  tests/gateway/test_slack_approval_buttons.py \
  tests/e2e/test_platform_commands.py

# Standalone send_message Slack thread targeting. This file currently skips when
# optional telegram deps are absent, so allow pytest's "no tests collected" code.
run_pytest --allow-no-tests \
  tests/tools/test_send_message_tool.py \
  -k 'slack_thread_id_is_forwarded or slack_messages_are_formatted or resolved_slack_thread_name'

# Coop custom surfaces + upstream conflict surfaces.
run_pytest \
  tests/agent/test_prompt_builder.py \
  tests/agent/test_preflight_compression_diagnostics.py \
  tests/plugins/test_thoughts_dashboard_plugin.py \
  tests/cron/test_cron_mind_event_classification.py \
  tests/hermes_cli/test_custom_fork_guardrails.py \
  tests/hermes_cli/test_tools_config.py \
  tests/hermes_cli/test_web_server.py
