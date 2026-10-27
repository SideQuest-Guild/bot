"""Unit tests for the server-log pure helpers and embed builders."""

from datetime import UTC, datetime, timedelta

import discord

from sidequest_bot.plugins.logging import (
    build_bulk_delete_embed,
    build_channel_update_embed,
    build_member_join_embed,
    build_message_delete_embed,
    build_message_edit_embed,
    build_role_update_detail_embed,
    build_role_update_embed,
    build_transcript_text,
    build_voice_embed,
    channel_kind,
    content_changed,
    diff_fields,
    diff_roles,
    format_account_age,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class TestFormatAccountAge:
    def test_new_account_flagged(self) -> None:
        text, is_new = format_account_age(NOW - timedelta(days=2), now=NOW)
        assert is_new is True
        assert "2 days" in text

    def test_old_account_not_flagged(self) -> None:
        text, is_new = format_account_age(NOW - timedelta(days=30), now=NOW)
        assert is_new is False
        assert "30 days" in text

    def test_boundary_exactly_seven_days_is_not_new(self) -> None:
        # The threshold is exclusive: exactly 7 days old is no longer "new".
        _, is_new = format_account_age(NOW - timedelta(days=7), now=NOW)
        assert is_new is False

    def test_just_under_threshold_is_new(self) -> None:
        _, is_new = format_account_age(NOW - timedelta(days=6, hours=23), now=NOW)
        assert is_new is True

    def test_less_than_a_day_old(self) -> None:
        text, is_new = format_account_age(NOW - timedelta(hours=3), now=NOW)
        assert is_new is True
        assert text == "less than a day old"

    def test_singular_day_wording(self) -> None:
        text, _ = format_account_age(NOW - timedelta(days=1), now=NOW)
        assert text == "1 day old"


class TestContentChanged:
    def test_identical_content_unchanged(self) -> None:
        assert content_changed("hello", "hello") is False

    def test_different_content_changed(self) -> None:
        assert content_changed("hello", "goodbye") is True

    def test_embed_unfurl_with_none_before_after_equal(self) -> None:
        # Discord fires message_update with the same content when a link unfurls into an embed.
        assert content_changed(None, None) is False

    def test_empty_to_text_is_changed(self) -> None:
        assert content_changed(None, "new text") is True

    def test_text_to_empty_is_changed(self) -> None:
        assert content_changed("old text", None) is True


class TestDiffRoles:
    def test_added_and_removed(self) -> None:
        before = [(1, "Adventurer"), (2, "New Arrival")]
        after = [(1, "Adventurer"), (3, "Seasoned Adventurer")]
        added, removed = diff_roles(before, after)
        assert added == ["Seasoned Adventurer"]
        assert removed == ["New Arrival"]

    def test_no_change(self) -> None:
        roles = [(1, "Adventurer")]
        added, removed = diff_roles(roles, roles)
        assert added == []
        assert removed == []

    def test_all_removed(self) -> None:
        before = [(1, "Adventurer"), (2, "New Arrival")]
        added, removed = diff_roles(before, [])
        assert added == []
        assert set(removed) == {"Adventurer", "New Arrival"}

    def test_all_added(self) -> None:
        after = [(1, "Adventurer")]
        added, removed = diff_roles([], after)
        assert added == ["Adventurer"]
        assert removed == []


class TestDiffFields:
    def test_only_changed_fields_included(self) -> None:
        before = {"Name": "general", "Topic": "chat"}
        after = {"Name": "general", "Topic": "off-topic chat"}
        assert diff_fields(before, after) == [("Topic", "chat", "off-topic chat")]

    def test_no_changes_returns_empty(self) -> None:
        fields = {"Name": "general"}
        assert diff_fields(fields, fields) == []

    def test_field_missing_from_after_treated_unchanged(self) -> None:
        # Missing-from-after falls back to the before value (no diff emitted) rather than crashing.
        assert diff_fields({"Name": "general"}, {}) == []


class TestBuildTranscriptText:
    def test_formats_each_entry(self) -> None:
        text = build_transcript_text([("Alice", "hi"), ("Bob", "hello")])
        assert text == "Alice: hi\nBob: hello"

    def test_empty_content_placeholder(self) -> None:
        text = build_transcript_text([("Alice", "")])
        assert text == "Alice: [no text content]"

    def test_empty_entries(self) -> None:
        assert build_transcript_text([]) == ""


class TestBuildMessageDeleteEmbed:
    def test_cached_message_shows_content(self) -> None:
        embed = build_message_delete_embed(
            channel_mention="#general",
            author_line="Alice",
            content="hello world",
            attachment_names=["cat.png"],
            cached=True,
        )
        field_values = {f.name: f.value for f in embed.fields}
        assert field_values["Author"] == "Alice"
        assert field_values["Content"] == "hello world"
        assert field_values["Attachments"] == "cat.png"

    def test_uncached_message_notes_unavailable(self) -> None:
        embed = build_message_delete_embed(
            channel_mention="#general", author_line=None, content=None, attachment_names=[], cached=False
        )
        assert embed.description == "Message wasn't cached; content unavailable."
        field_values = {f.name: f.value for f in embed.fields}
        assert field_values["Author"] == "Unknown"
        assert "Content" not in field_values

    def test_truncates_long_content_to_embed_field_limit(self) -> None:
        long_content = "x" * 2000
        embed = build_message_delete_embed(
            channel_mention="#general", author_line="Alice", content=long_content, attachment_names=[], cached=True
        )
        field_values = {f.name: f.value for f in embed.fields}
        assert len(field_values["Content"]) <= 1024
        assert field_values["Content"].endswith("…")


class TestBuildBulkDeleteEmbed:
    def test_includes_count_and_channel(self) -> None:
        embed = build_bulk_delete_embed("#general", 42)
        assert "42" in (embed.description or "")
        assert "#general" in (embed.description or "")


class TestBuildMessageEditEmbed:
    def test_returns_none_when_unchanged(self) -> None:
        embed = build_message_edit_embed(channel_mention="#general", author_line="Alice", before="same", after="same")
        assert embed is None

    def test_returns_none_for_unfurl_with_no_text_change(self) -> None:
        # Simulates an embed-only update: content before/after is identical.
        embed = build_message_edit_embed(
            channel_mention="#general", author_line="Alice", before="https://example.com", after="https://example.com"
        )
        assert embed is None

    def test_builds_embed_when_changed(self) -> None:
        embed = build_message_edit_embed(
            channel_mention="#general", author_line="Alice", before="before text", after="after text"
        )
        assert embed is not None
        field_values = {f.name: f.value for f in embed.fields}
        assert field_values["Before"] == "before text"
        assert field_values["After"] == "after text"

    def test_truncates_before_and_after_to_embed_limit(self) -> None:
        embed = build_message_edit_embed(
            channel_mention="#general", author_line="Alice", before="a" * 2000, after="b" * 2000
        )
        assert embed is not None
        field_values = {f.name: f.value for f in embed.fields}
        assert len(field_values["Before"]) <= 1024
        assert len(field_values["After"]) <= 1024


class TestBuildMemberJoinEmbed:
    def test_flags_new_account_with_warning(self) -> None:
        embed = build_member_join_embed("Alice", "2 days old", is_new=True)
        field_values = {f.name: f.value for f in embed.fields}
        assert "⚠️" in field_values["Account age"]

    def test_does_not_flag_established_account(self) -> None:
        embed = build_member_join_embed("Alice", "400 days old", is_new=False)
        field_values = {f.name: f.value for f in embed.fields}
        assert "⚠️" not in field_values["Account age"]
        assert field_values["Account age"] == "400 days old"


class TestBuildRoleUpdateEmbed:
    def test_returns_none_when_no_changes(self) -> None:
        assert build_role_update_embed("Alice", [], []) is None

    def test_includes_added_and_removed_fields(self) -> None:
        embed = build_role_update_embed("Alice", ["Adventurer"], ["New Arrival"])
        assert embed is not None
        field_values = {f.name: f.value for f in embed.fields}
        assert field_values["Added"] == "Adventurer"
        assert field_values["Removed"] == "New Arrival"

    def test_only_added_omits_removed_field(self) -> None:
        embed = build_role_update_embed("Alice", ["Adventurer"], [])
        assert embed is not None
        names = [f.name for f in embed.fields]
        assert "Added" in names
        assert "Removed" not in names


class TestBuildChannelUpdateEmbed:
    def test_returns_none_when_no_changes(self) -> None:
        assert build_channel_update_embed("general", []) is None

    def test_includes_before_after_arrow(self) -> None:
        embed = build_channel_update_embed("general", [("Topic", "old topic", "new topic")])
        assert embed is not None
        field_values = {f.name: f.value for f in embed.fields}
        assert field_values["Topic"] == "old topic → new topic"


class TestBuildRoleUpdateDetailEmbed:
    def test_returns_none_when_no_changes(self) -> None:
        assert build_role_update_detail_embed("Adventurer", []) is None

    def test_includes_changed_fields(self) -> None:
        embed = build_role_update_detail_embed("Adventurer", [("Color", "#000000", "#ffffff")])
        assert embed is not None
        field_values = {f.name: f.value for f in embed.fields}
        assert field_values["Color"] == "#000000 → #ffffff"


class TestBuildVoiceEmbed:
    def test_returns_none_when_channel_unchanged(self) -> None:
        assert build_voice_embed("Alice", before_channel="#voice", after_channel="#voice") is None

    def test_join_when_before_is_none(self) -> None:
        embed = build_voice_embed("Alice", before_channel=None, after_channel="#voice")
        assert embed is not None
        assert embed.title == "Voice join"

    def test_leave_when_after_is_none(self) -> None:
        embed = build_voice_embed("Alice", before_channel="#voice", after_channel=None)
        assert embed is not None
        assert embed.title == "Voice leave"

    def test_move_when_both_set_and_different(self) -> None:
        embed = build_voice_embed("Alice", before_channel="#voice-a", after_channel="#voice-b")
        assert embed is not None
        assert embed.title == "Voice move"


class TestChannelKind:
    def test_text_channel(self) -> None:
        assert channel_kind(discord.ChannelType.text) == "Text channel"

    def test_voice_channel(self) -> None:
        assert channel_kind(discord.ChannelType.voice) == "Voice channel"

    def test_category_has_no_channel_suffix(self) -> None:
        assert channel_kind(discord.ChannelType.category) == "Category"

    def test_stage_voice_channel_underscore_becomes_space(self) -> None:
        assert channel_kind(discord.ChannelType.stage_voice) == "Stage voice channel"
