"""Diagnostics for preflight compression pass behavior.

These tests document why users can see multiple back-to-back compaction status
messages in a single visible turn: the preflight guard intentionally allows up to
three compression passes while rough request estimates remain over threshold.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent import turn_context


class _ToolGuardrails:
    def reset_for_turn(self):
        pass


class _TodoStore:
    def has_items(self):
        return False


class _RuntimeInterrupt:
    def _set_interrupt(self, value, thread_id):
        self.last = (value, thread_id)


class _FakeCompressor:
    protect_first_n = 0
    protect_last_n = 0
    threshold_tokens = 500
    context_length = 1000
    last_prompt_tokens = 0
    last_real_prompt_tokens = 0

    def __init__(self):
        self.seen_should_compress_tokens = []

    def should_defer_preflight_to_real_usage(self, tokens):
        return False

    def should_compress(self, tokens):
        self.seen_should_compress_tokens.append(tokens)
        return tokens >= self.threshold_tokens


def _make_agent():
    compressor = _FakeCompressor()
    agent = SimpleNamespace(
        session_id="session-preflight",
        provider="test-provider",
        model="test-model",
        base_url="",
        api_key="",
        api_mode="chat_completions",
        platform="slack",
        compression_enabled=True,
        context_compressor=compressor,
        tools=[],
        quiet_mode=True,
        _cached_system_prompt="system prompt",
        _memory_write_origin="assistant_tool",
        _tool_guardrails=_ToolGuardrails(),
        _compression_warning=None,
        max_iterations=3,
        _todo_store=_TodoStore(),
        _user_turn_count=0,
        _memory_nudge_interval=0,
        _turns_since_memory=0,
        _stream_context_scrubber=None,
        _stream_think_scrubber=None,
        _memory_manager=None,
        _memory_store=None,
        valid_tool_names=[],
        _interrupt_requested=False,
        _interrupt_thread_signal_pending=False,
        _interrupt_message=None,
    )
    agent.compress_calls = 0
    agent.statuses = []
    agent._ensure_db_session = lambda: None
    agent._restore_primary_runtime = lambda: None
    agent._cleanup_dead_connections = lambda: False
    agent._emit_status = agent.statuses.append
    agent._replay_compression_warning = lambda: None
    agent._safe_print = lambda *_args, **_kwargs: None
    agent._hydrate_todo_store = lambda _history: None
    agent._persist_session = lambda _messages, _history: None

    def _compress_context(messages, system_message, approx_tokens, task_id):
        agent.compress_calls += 1
        # Simulate productive-but-insufficient compression: each pass shortens
        # the message list, but the rough estimator stays over threshold until
        # the hard three-pass cap is reached.
        return messages[:-1], f"{system_message or 'system prompt'} / compressed {agent.compress_calls}"

    agent._compress_context = _compress_context
    return agent


def test_preflight_compression_can_run_three_passes_when_rough_estimate_stays_high(monkeypatch):
    agent = _make_agent()
    history = [{"role": "user", "content": f"old turn {i}"} for i in range(6)]
    estimates = iter([1000, 900, 800, 700])

    monkeypatch.setattr(
        turn_context,
        "estimate_request_tokens_rough",
        lambda *_args, **_kwargs: next(estimates),
    )

    ctx = turn_context.build_turn_context(
        agent,
        user_message="new turn",
        system_message="system prompt",
        conversation_history=history,
        task_id="task-preflight",
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *_args, **_kwargs: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda value: value,
        summarize_user_message_for_log=lambda value: value,
        set_session_context=lambda _session_id: None,
        set_current_write_origin=lambda _origin: None,
        ra=lambda: _RuntimeInterrupt(),
    )

    assert agent.compress_calls == 3
    assert len(agent.statuses) == 1
    assert "Preflight compression" in agent.statuses[0]
    assert ctx.conversation_history is None
    assert agent.context_compressor.seen_should_compress_tokens[:4] == [1000, 900, 800, 700]
