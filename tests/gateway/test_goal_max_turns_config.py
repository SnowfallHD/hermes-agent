import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals


class _FakeSessionEntry:
    session_id = "sid-gateway-goal-config"


class _FakeSessionStore:
    def __init__(self):
        self.entry = _FakeSessionEntry()

    def get_or_create_session(self, source):
        return self.entry

    def _generate_session_key(self, source):
        return "agent:main:discord:channel:goal-config"


@pytest.mark.asyncio
async def test_gateway_goal_uses_goals_max_turns_from_full_config(tmp_path, monkeypatch):
    """Gateway /goal should honor top-level goals.max_turns from config.yaml."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("goals:\n  max_turns: 7\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}

    event = MessageEvent(
        text="/goal ship the benchmark",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-config",
            chat_type="channel",
            user_id="user-goal-config",
        ),
        message_id="msg-goal-config",
    )

    response = await GatewayRunner._handle_goal_command(runner, event)

    try:
        assert "⊙ Goal set (7-turn budget): ship the benchmark" in response
        state = goals.GoalManager("sid-gateway-goal-config").state
        assert state is not None
        assert state.max_turns == 7
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_slack_goal_notice_uses_bang_controls_and_end_alias(tmp_path, monkeypatch):
    """Slack threads cannot use native / commands, so /goal notices should surface ! controls."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("goals:\n  max_turns: 7\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.SLACK: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}

    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="chat-goal-config",
        chat_type="channel",
        user_id="user-goal-config",
    )
    event = MessageEvent(
        text="/goal ship the benchmark",
        message_type=MessageType.COMMAND,
        source=source,
        message_id="msg-goal-config",
    )

    response = await GatewayRunner._handle_goal_command(runner, event)

    try:
        assert "⊙ Goal set (7-turn budget): ship the benchmark" in response
        assert "Controls: `!goal status` · `!goal pause` · `!goal resume` · `!goal end`" in response

        status_event = MessageEvent(
            text="/goal status",
            message_type=MessageType.COMMAND,
            source=source,
            message_id="msg-goal-status",
        )
        status = await GatewayRunner._handle_goal_command(runner, status_event)
        assert "Controls: `!goal status` · `!goal pause` · `!goal resume` · `!goal end`" in status

        end_event = MessageEvent(
            text="/goal end",
            message_type=MessageType.COMMAND,
            source=source,
            message_id="msg-goal-end",
        )
        ended = await GatewayRunner._handle_goal_command(runner, end_event)
        assert ended == "✓ Goal cleared."
    finally:
        goals._DB_CACHE.clear()
