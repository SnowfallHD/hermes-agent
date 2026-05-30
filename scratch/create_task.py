#!/usr/bin/env python3
"""Create a metacognition Kanban task using the internal kanban API."""

from pathlib import Path
import sys

# Add hermes_cli to path if running in the repo root
repo_root = Path(__file__).parent
sys.path.insert(0, str(repo_root))

from hermes_cli import kanban_db as kb


def main():
    board_slug = "kryden-50k-mrr"
    idempotency_key = "thoughts-metacog:assistant_completed_claims_need_outcome_verification"
    tenant = ""  #HERMES_TENANT is empty in this environment

    print(f"Creating task on board: {board_slug}")

    with kb.connect_closing(board=board_slug) as conn:
        task_id = kb.create_task(
            conn=conn,
            title="Verify completed-work claims against actual outcomes",
            body=(
                "Recent Thoughts make claims like 'Done. Changed to every 2 hours, 7 days a week, 24 hours a day.' "
                "or 'Done. The cron is now a real agent cron (not script-only).' but there is no verification evidence "
                "(edit, test, lint, compile, smoke, verify). "
                "Audit all completed-work claims against observable traces: edits, tool calls, "
                "tests/lint/compile/smoke/verify tool usage, or an explicit blocker. "
                "Classify draft/planned-only claims separately; they must not create correction tasks. "
                "Draft: an audit that validates thought inferences against observable traces: "
                "evidence_refs, route/outcome links, category/event_type alignment, later verification, "
                "and next_best_action follow-through."
            ),
            assignee="planner",
            priority=90,
            idempotency_key=idempotency_key,
            tenant=tenant,
        )
    print(f"Created/returned task: {task_id}")


if __name__ == "__main__":
    main()
