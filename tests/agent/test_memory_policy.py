"""Regression tests for external memory providers as canonical memory.

When an external provider (Honcho, Mem0, etc.) is active, the provider should be
Hermes's durable/canonical memory surface. Built-in MEMORY.md / USER.md remain
useful, but only as compact local/recent context.
"""

from types import SimpleNamespace

from agent.memory_policy import (
    canonical_provider_active,
    format_local_recent_block,
    should_run_local_memory_nudge,
)


class Provider:
    def __init__(self, name):
        self.name = name


class Manager:
    def __init__(self, providers):
        self.providers = providers


def _agent(*, mode="canonical", providers=None, nudge_interval=10):
    return SimpleNamespace(
        _memory_provider_mode=mode,
        _memory_manager=Manager(providers or []),
        _memory_nudge_interval=nudge_interval,
        valid_tool_names=["memory"],
        _memory_store=object(),
    )


def test_canonical_provider_active_requires_external_provider():
    assert canonical_provider_active(_agent(providers=[Provider("honcho")])) is True
    assert canonical_provider_active(_agent(providers=[])) is False
    assert canonical_provider_active(_agent(mode="additive", providers=[Provider("honcho")])) is False


def test_canonical_provider_suppresses_local_memory_nudge():
    assert should_run_local_memory_nudge(_agent(providers=[Provider("honcho")])) is False
    assert should_run_local_memory_nudge(_agent(mode="additive", providers=[Provider("honcho")])) is True
    assert should_run_local_memory_nudge(_agent(providers=[])) is True


def test_local_recent_block_relabeled_when_provider_is_canonical():
    block = format_local_recent_block(
        '# MEMORY (your personal notes) [20% — 10/50 chars]\nold local fact',
        target="memory",
        canonical_provider=True,
    )

    assert block.startswith("# LOCAL RECENT MEMORY")
    assert "additive compact cache" in block
    assert "old local fact" in block
    assert "# MEMORY (your personal notes)" not in block


def test_local_recent_user_block_relabeled_when_provider_is_canonical():
    block = format_local_recent_block(
        '# USER PROFILE (who the user is) [10% — 5/50 chars]\nold user fact',
        target="user",
        canonical_provider=True,
    )

    assert block.startswith("# LOCAL RECENT USER PROFILE")
    assert "Honcho/external provider is canonical" in block
    assert "old user fact" in block
    assert "# USER PROFILE" not in block
