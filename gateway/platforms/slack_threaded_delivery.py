"""Slack threaded-delivery formatting overlay.

This module keeps Coop/Kryden's Viktor-style Slack delivery behavior out of
SlackAdapter core mechanics.  The adapter owns Slack API/session semantics; this
module owns the optional formatting policy:

- long/structured top-level replies become a compact channel parent + detailed
  thread reply;
- existing thread replies remain single replies, avoiding nested threads;
- parent summaries can render with Slack Block Kit header blocks, Slack's native
  large-text surface.

The behavior is controlled by platform config extra key
``gateway.slack.auto_parent_thread`` (default: enabled).  Slack does not support
arbitrary dynamic font sizes in mrkdwn; header blocks are the supported large
text path.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

TRUE_VALUES = {"1", "true", "yes", "on"}

STRUCTURED_MARKERS = (
    "Root cause:",
    "Evidence:",
    "Files changed:",
    "Commands/tests run:",
    "Result:",
    "Remaining risk:",
    "Next action:",
    "Detailed breakdown:",
    "Details:",
    "Thread detail",
)


def auto_parent_thread_enabled(extra: Optional[Mapping[str, Any]]) -> bool:
    """Return whether automatic parent/thread splitting is enabled.

    Missing config defaults to enabled because Coop wants Viktor-style channel
    briefs as the standard Slack delivery policy. Operators can disable it per
    Slack platform config with ``auto_parent_thread: false``.
    """
    raw = (extra or {}).get("auto_parent_thread")
    if raw is None:
        return True
    return str(raw).strip().lower() in TRUE_VALUES


def split_auto_parent_thread_content(
    content: str,
    *,
    thread_ts: Optional[str],
    enabled: bool = True,
    min_chars: int = 700,
    min_nonempty_lines: int = 9,
    max_parent_chars: int = 650,
    max_parent_lines: int = 6,
) -> Optional[Tuple[str, str]]:
    """Split a top-level Slack reply into ``(parent, detail)`` when useful.

    Returns ``None`` when the message is short/unstructured, empty, already
    inside a Slack thread, or disabled by config.  The split is intentionally
    heuristic: keep the channel-visible parent compact and move the detailed
    audit/log/runbook body into the newly-created thread.
    """
    if thread_ts or not enabled:
        return None
    text = (content or "").strip()
    if not text:
        return None

    lines = [line.rstrip() for line in text.splitlines()]
    nonempty_count = sum(1 for line in lines if line.strip())
    if (
        len(text) < min_chars
        and nonempty_count < min_nonempty_lines
        and not any(marker in text for marker in STRUCTURED_MARKERS)
    ):
        return None

    parent_lines: List[str] = []
    detail_start = 0
    parent_chars = 0
    seen_content = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped and seen_content:
            if seen_content >= 2:
                detail_start = idx + 1
                break
            continue
        if not stripped:
            continue
        line_len = len(line)
        if seen_content >= max_parent_lines or parent_chars + line_len > max_parent_chars:
            detail_start = idx
            break
        parent_lines.append(line)
        parent_chars += line_len + 1
        seen_content += 1
    else:
        detail_start = len(lines)

    parent = "\n".join(parent_lines).strip()
    detail = "\n".join(lines[detail_start:]).strip()
    if not parent or not detail:
        return None
    return parent, detail


def plain_slack_header(text: str) -> str:
    """Strip markdown-ish syntax for Slack Block Kit plain_text headers."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"[*_~]", "", text)
    text = text.replace("<", "").replace(">", "")
    return re.sub(r"\s+", " ", text).strip()


def build_parent_summary_blocks(
    parent_raw: str,
    *,
    format_message: Callable[[str], str],
    fallback_header: str = "Hermes update",
) -> Optional[List[Dict[str, Any]]]:
    """Build Slack Block Kit blocks for the compact parent summary.

    Slack Block Kit ``header`` is the supported big-font surface. The remaining
    parent body stays mrkdwn in a section block so bullets/numbering survive.
    """
    lines = [line.strip() for line in (parent_raw or "").splitlines() if line.strip()]
    if not lines:
        return None

    header = plain_slack_header(lines[0])[:150] or fallback_header
    body_raw = "\n".join(lines[1:]).strip()
    blocks: List[Dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header, "emoji": True},
        }
    ]
    if body_raw:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": format_message(body_raw)[:3000]},
            }
        )
    return blocks
