"""Slack channel → profile routing for the single-gateway profile multiplexer."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.fixture
def adapter():
    config = PlatformConfig(enabled=True, token="«redacted:xox…")
    config.extra["allowed_channels"] = ["C_BUILDER", "C_DEFAULT"]
    config.extra["profile_channels"] = {"C_BUILDER": "builder"}
    a = SlackAdapter(config)
    a._app = MagicMock()
    a._app.client = AsyncMock()
    a._bot_user_id = "U_BOT"
    a._running = True
    a.handle_message = AsyncMock()
    return a


def _channel_event(
    channel: str,
    text: str = "<@U_BOT> work",
    ts: str = "1700000000.000001",
    thread_ts: str | None = None,
) -> dict:
    event = {
        "channel": channel,
        "channel_type": "channel",
        "user": "U_USER",
        "text": text,
        "ts": ts,
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event


async def _capture_event(adapter: SlackAdapter, event: dict):
    captured = []
    adapter.handle_message = AsyncMock(side_effect=lambda e: captured.append(e))
    with (
        patch.object(
            adapter, "_resolve_user_name", new=AsyncMock(return_value="testuser")
        ),
        patch.object(adapter, "_fetch_thread_context", new=AsyncMock(return_value="")),
        patch.object(
            adapter, "_fetch_thread_parent_text", new=AsyncMock(return_value=None)
        ),
    ):
        await adapter._handle_slack_message(event)
    assert len(captured) == 1
    return captured[0]


def test_config_bridges_slack_profile_channels(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "slack:\n"
        "  profile_channels:\n"
        '    "C_BUILDER": " builder "\n'
        '    "C_RESEARCH": research\n'
        '    "": ignored\n'
        '    "C_EMPTY": "  "\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    config = load_gateway_config()

    assert config.platforms[Platform.SLACK].extra["profile_channels"] == {
        "C_BUILDER": "builder",
        "C_RESEARCH": "research",
    }


class TestSlackProfileChannels:
    @pytest.mark.asyncio
    async def test_mapped_channel_stamps_source_profile(self, adapter):
        event = _channel_event("C_BUILDER")

        msg = await _capture_event(adapter, event)

        assert msg.source.profile == "builder"

    @pytest.mark.asyncio
    async def test_unmapped_allowed_channel_stays_on_gateway_profile(self, adapter):
        event = _channel_event("C_DEFAULT")

        msg = await _capture_event(adapter, event)

        assert msg.source.profile is None

    @pytest.mark.asyncio
    async def test_thread_reply_in_mapped_channel_keeps_profile_route(self, adapter):
        # Put the channel in free-response mode so a non-mentioned thread reply
        # reaches the handler exactly like Coop replying inside a profile channel.
        adapter.config.extra["free_response_channels"] = ["C_BUILDER"]
        event = _channel_event(
            "C_BUILDER",
            text="follow-up in the builder thread",
            ts="1700000000.000002",
            thread_ts="1700000000.000001",
        )

        msg = await _capture_event(adapter, event)

        assert msg.source.profile == "builder"
        assert msg.source.thread_id == "1700000000.000001"
