"""Metacognitive observer plugin.

This bundled plugin registers only an observational post_tool_call hook. The
actual recording path remains gated inside :mod:`agent.metacognitive_router` by
``metacognitive_router.enabled``, ``dry_run``, and
``record_paths.tool_results`` so autoloading the plugin does not change runtime
behavior unless explicitly configured.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _as_success(status: Any, error_type: Any, error_message: Any) -> bool:
    if isinstance(status, str):
        return status.lower() not in {"error", "failed", "blocked"}
    return not (error_type or error_message)


def _on_post_tool_call(**payload: Any) -> None:
    try:
        from agent.metacognitive_router import maybe_record_tool_result

        maybe_record_tool_result(
            tool_name=str(payload.get("tool_name") or ""),
            args=payload.get("args"),
            result=payload.get("result"),
            action_success=_as_success(
                payload.get("status"),
                payload.get("error_type"),
                payload.get("error_message"),
            ),
            task_id=payload.get("task_id") or None,
            session_id=payload.get("session_id") or None,
            tool_call_id=payload.get("tool_call_id") or None,
            duration_ms=payload.get("duration_ms"),
            status=payload.get("status"),
        )
    except Exception as exc:  # pragma: no cover - observer must never break tools
        logger.debug("metacog observer post_tool_call failed: %s", exc)


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
