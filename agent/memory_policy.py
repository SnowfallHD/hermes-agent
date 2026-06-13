"""Memory-provider policy helpers.

External memory providers (Honcho, Mem0, etc.) can be treated as the canonical
long-term memory surface while built-in MEMORY.md / USER.md remain as bounded,
local/recent context.  Keep the rules here so prompt assembly, turn nudges, and
status/reporting do not each rediscover slightly different semantics.
"""

from __future__ import annotations

from typing import Any, Iterable

_CANONICAL = "canonical"
_ADDITIVE = "additive"
_VALID_MODES = {_CANONICAL, _ADDITIVE}


def normalize_provider_mode(value: Any) -> str:
    """Normalize memory.provider_mode.

    ``canonical`` means an external provider is the durable source of truth and
    local md is a recent/additive cache.  ``additive`` preserves legacy behavior:
    local md remains first-class and external providers only add recall/tools.
    """
    mode = str(value or _CANONICAL).strip().lower()
    return mode if mode in _VALID_MODES else _CANONICAL


def _provider_names(memory_manager: Any) -> list[str]:
    providers: Iterable[Any] = getattr(memory_manager, "providers", []) or []
    names: list[str] = []
    for provider in providers:
        name = getattr(provider, "name", "") or ""
        if name:
            names.append(str(name))
    return names


def has_external_provider(memory_manager: Any) -> bool:
    """Return True when at least one non-built-in memory provider is active."""
    return any(name != "builtin" for name in _provider_names(memory_manager))


def canonical_provider_active(agent: Any) -> bool:
    """Return True when an external provider should be treated as canonical."""
    return (
        normalize_provider_mode(getattr(agent, "_memory_provider_mode", _CANONICAL)) == _CANONICAL
        and has_external_provider(getattr(agent, "_memory_manager", None))
    )


def should_run_local_memory_nudge(agent: Any) -> bool:
    """Return whether the local md-memory consolidation nudge should fire."""
    if canonical_provider_active(agent):
        return False
    return (
        getattr(agent, "_memory_nudge_interval", 0) > 0
        and "memory" in (getattr(agent, "valid_tool_names", []) or [])
        and bool(getattr(agent, "_memory_store", None))
    )


def format_local_recent_block(block: str, *, target: str, canonical_provider: bool) -> str:
    """Relabel built-in memory blocks when an external provider is canonical.

    The content is still useful, but the heading must not imply that MEMORY.md /
    USER.md are the canonical long-term store.  This keeps model behavior aligned
    with provider-canonical installs without removing the compact local context.
    """
    if not canonical_provider or not block:
        return block

    lines = block.splitlines()
    body = "\n".join(lines[1:]).strip() if lines else ""
    if target == "user":
        heading = "# LOCAL RECENT USER PROFILE"
    else:
        heading = "# LOCAL RECENT MEMORY"

    note = (
        "Honcho/external provider is canonical long-term memory; this local md "
        "block is an additive compact cache for recent or high-signal context. "
        "Do not treat local md as the sole source of truth."
    )
    if body:
        return f"{heading}\n{note}\n\n{body}"
    return f"{heading}\n{note}"
