"""Tests for the bundled metacognitive observer plugin."""

from __future__ import annotations


class _FakePluginContext:
    def __init__(self) -> None:
        self.hooks = {}

    def register_hook(self, name, callback) -> None:
        self.hooks.setdefault(name, []).append(callback)


def test_metacog_observer_registers_post_tool_call_hook() -> None:
    from plugins.metacog_observer import register

    ctx = _FakePluginContext()
    register(ctx)

    assert "post_tool_call" in ctx.hooks
    assert len(ctx.hooks["post_tool_call"]) == 1


def test_metacog_observer_forwards_redacted_tool_payload(monkeypatch) -> None:
    import plugins.metacog_observer as plugin

    calls = []

    def fake_record(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(
        "agent.metacognitive_router.maybe_record_tool_result",
        fake_record,
    )

    plugin._on_post_tool_call(
        tool_name="terminal",
        args={"command": "echo hi"},
        result="hi",
        status="ok",
        task_id="task-1",
        session_id="session-1",
        tool_call_id="call-1",
        duration_ms=12,
    )

    assert calls == [
        {
            "tool_name": "terminal",
            "args": {"command": "echo hi"},
            "result": "hi",
            "action_success": True,
            "task_id": "task-1",
            "session_id": "session-1",
            "tool_call_id": "call-1",
            "duration_ms": 12,
            "status": "ok",
        }
    ]


def test_metacog_observer_treats_tool_errors_as_unsuccessful(monkeypatch) -> None:
    import plugins.metacog_observer as plugin

    calls = []
    monkeypatch.setattr(
        "agent.metacognitive_router.maybe_record_tool_result",
        lambda **kwargs: calls.append(kwargs),
    )

    plugin._on_post_tool_call(
        tool_name="terminal",
        args={},
        result={"error": "boom"},
        status="error",
        error_type="tool_error",
        error_message="boom",
    )

    assert calls
    assert calls[0]["action_success"] is False


def test_metacog_observer_is_fail_open(monkeypatch) -> None:
    import plugins.metacog_observer as plugin

    def boom(**_kwargs):
        raise RuntimeError("observer should never break tool execution")

    monkeypatch.setattr("agent.metacognitive_router.maybe_record_tool_result", boom)

    # No exception should escape the hook.
    plugin._on_post_tool_call(tool_name="terminal", args={}, result="ok")
