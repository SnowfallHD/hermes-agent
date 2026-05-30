#!/usr/bin/env python3
"""Verify the created task."""

from pathlib import Path
import sys

repo_root = Path(__file__).parent
sys.path.insert(0, str(repo_root))

from hermes_cli import kanban_db as kb


def main():
    board_slug = "kryden-50k-mrr"
    task_id = "t_ae921948"

    print(f"Verifying task on board: {board_slug}")

    with kb.connect_closing(board=board_slug) as conn:
        task = kb.get_task(conn, task_id)
        print(f"Task ID: {task.id}")
        print(f"Title: {task.title}")
        print(f"Status: {task.status}")
        print(f"Assignee: {task.assignee}")
        print(f"Priority: {task.priority}")
        print(f"Tenant: {task.tenant}")
        print(f"Body preview: {task.body[:200] if task.body else 'None'}...")


if __name__ == "__main__":
    main()
