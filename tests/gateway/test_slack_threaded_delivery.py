from gateway.platforms.slack_threaded_delivery import (
    auto_parent_thread_enabled,
    build_parent_summary_blocks,
    plain_slack_header,
    split_auto_parent_thread_content,
)


def test_auto_parent_thread_enabled_defaults_on_and_honors_false():
    assert auto_parent_thread_enabled({}) is True
    assert auto_parent_thread_enabled({"auto_parent_thread": "false"}) is False
    assert auto_parent_thread_enabled({"auto_parent_thread": "yes"}) is True


def test_short_unstructured_message_is_not_split():
    assert (
        split_auto_parent_thread_content(
            "Short update with no detailed body.",
            thread_ts=None,
            enabled=True,
        )
        is None
    )


def test_existing_thread_reply_is_not_split_again():
    content = "🚀 Launch brief\n\n" + "\n".join(f"Detail line {i}" for i in range(20))
    assert (
        split_auto_parent_thread_content(
            content,
            thread_ts="123.456",
            enabled=True,
        )
        is None
    )


def test_structured_long_top_level_message_splits_into_parent_and_detail():
    content = "\n".join(
        [
            "🚀 Launch brief",
            "**Summary:** shipped the thing",
            "1. First channel-visible point",
            "2. Second channel-visible point",
            "",
            "Detailed breakdown:",
            "- evidence A",
            "- evidence B",
            "- evidence C",
        ]
    )

    split = split_auto_parent_thread_content(content, thread_ts=None, enabled=True)

    assert split is not None
    parent, detail = split
    assert "🚀 Launch brief" in parent
    assert "Detailed breakdown:" in detail


def test_parent_blocks_use_header_and_keep_body_mrkdwn():
    blocks = build_parent_summary_blocks(
        "🚀 **Launch brief**\n1. First\n2. Second",
        format_message=lambda text: text.replace("**", "*"),
    )

    assert blocks is not None
    assert blocks[0]["type"] == "header"
    assert blocks[0]["text"] == {
        "type": "plain_text",
        "text": "🚀 Launch brief",
        "emoji": True,
    }
    assert blocks[1]["type"] == "section"
    assert "1. First" in blocks[1]["text"]["text"]


def test_plain_slack_header_strips_markdown_noise():
    assert plain_slack_header("🚀 **Launch** [`docs`](https://example.com) <tag>") == "🚀 Launch docs tag"
