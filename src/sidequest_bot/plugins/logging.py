"""Carl-bot-style server log: deletes, edits, member/role/channel/voice changes, mod audit entries.

Everything is posted with ``bot.send_log`` to the guild's configured log channel. Embed construction is kept in
pure, importable builder functions (see the ``build_*``/``diff_*``/``format_*`` helpers) so the diffing, truncation,
and account-age logic can be unit tested without a live Discord connection.
"""

import io
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate, user_line
from sidequest_common.schemas import GuildConfigUpdate

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

NEW_ACCOUNT_THRESHOLD = timedelta(days=7)
TRANSCRIPT_LIMIT = 1024


# --- Pure helpers (unit tested directly) ---------------------------------------------------------------------


def format_account_age(created_at: datetime, now: datetime | None = None) -> tuple[str, bool]:
    """Humanized account age and whether it's younger than :data:`NEW_ACCOUNT_THRESHOLD`."""
    now = now or datetime.now(UTC)
    age = now - created_at
    is_new = age < NEW_ACCOUNT_THRESHOLD
    days = age.days
    if days < 1:
        text = "less than a day old"
    elif days == 1:
        text = "1 day old"
    else:
        text = f"{days} days old"
    return text, is_new


def content_changed(before: str | None, after: str | None) -> bool:
    """False when a message "edit" didn't actually change the text (e.g. a link-unfurl embed)."""
    return (before or "") != (after or "")


def diff_roles(before: Sequence[tuple[int, str]], after: Sequence[tuple[int, str]]) -> tuple[list[str], list[str]]:
    """Role names added and removed, given ``(role_id, role_name)`` pairs before/after the change."""
    before_ids = {role_id for role_id, _ in before}
    after_ids = {role_id for role_id, _ in after}
    added = [name for role_id, name in after if role_id not in before_ids]
    removed = [name for role_id, name in before if role_id not in after_ids]
    return added, removed


def diff_fields(before: dict[str, str], after: dict[str, str]) -> list[tuple[str, str, str]]:
    """``(field, before, after)`` triples for every key present in both dicts whose value changed."""
    return [(field, value, after[field]) for field, value in before.items() if value != after.get(field, value)]


def build_transcript_text(entries: Sequence[tuple[str, str]]) -> str:
    """Plain-text transcript of ``(author_line, content)`` pairs for a bulk-delete attachment."""
    return "\n".join(f"{author}: {content or '[no text content]'}" for author, content in entries)


# --- Embed builders --------------------------------------------------------------------------------------------


def build_message_delete_embed(
    *,
    channel_mention: str,
    author_line: str | None,
    content: str | None,
    attachment_names: Sequence[str],
    cached: bool,
) -> discord.Embed:
    """Message-delete log entry; notes when the deleted message wasn't in the local cache."""
    embed = make_embed(title="Message deleted", color=Colors.RED)
    embed.add_field(name="Channel", value=channel_mention, inline=True)
    if not cached:
        embed.add_field(name="Author", value="Unknown", inline=True)
        embed.description = "Message wasn't cached; content unavailable."
        return embed
    embed.add_field(name="Author", value=author_line or "Unknown", inline=True)
    embed.add_field(name="Content", value=truncate(content) if content else "*(no text content)*", inline=False)
    if attachment_names:
        embed.add_field(name="Attachments", value=truncate(", ".join(attachment_names)), inline=False)
    return embed


def build_bulk_delete_embed(channel_mention: str, count: int) -> discord.Embed:
    """Bulk-delete summary; the transcript of cached messages is attached separately as a file."""
    return make_embed(
        title="Bulk delete",
        description=f"{count} messages deleted in {channel_mention}.",
        color=Colors.RED,
    )


def build_message_edit_embed(
    *, channel_mention: str, author_line: str, before: str | None, after: str | None
) -> discord.Embed | None:
    """Edit log entry, or ``None`` if the content didn't actually change."""
    if not content_changed(before, after):
        return None
    embed = make_embed(title="Message edited", color=Colors.SLATE)
    embed.add_field(name="Channel", value=channel_mention, inline=True)
    embed.add_field(name="Author", value=author_line, inline=True)
    embed.add_field(name="Before", value=truncate(before) if before else "*(empty)*", inline=False)
    embed.add_field(name="After", value=truncate(after) if after else "*(empty)*", inline=False)
    return embed


def build_member_join_embed(user_line_: str, account_age_text: str, *, is_new: bool) -> discord.Embed:
    """Member join log; flags accounts newer than :data:`NEW_ACCOUNT_THRESHOLD`."""
    embed = make_embed(title="Member joined", description=user_line_, color=Colors.GREEN)
    value = f"⚠️ {account_age_text} (new account)" if is_new else account_age_text
    embed.add_field(name="Account age", value=value, inline=False)
    return embed


def build_member_leave_embed(user_line_: str) -> discord.Embed:
    """Member leave log."""
    return make_embed(title="Member left", description=user_line_, color=Colors.RED)


def build_nickname_change_embed(user_line_: str, before: str | None, after: str | None) -> discord.Embed:
    """Nickname-change log."""
    embed = make_embed(title="Nickname changed", description=user_line_, color=Colors.SLATE)
    embed.add_field(name="Before", value=before or "*(none)*", inline=True)
    embed.add_field(name="After", value=after or "*(none)*", inline=True)
    return embed


def build_role_update_embed(user_line_: str, added: Sequence[str], removed: Sequence[str]) -> discord.Embed | None:
    """Added/removed-role log, or ``None`` if nothing actually changed."""
    if not added and not removed:
        return None
    embed = make_embed(title="Member roles updated", description=user_line_, color=Colors.BLUE)
    if added:
        embed.add_field(name="Added", value=truncate(", ".join(added)), inline=False)
    if removed:
        embed.add_field(name="Removed", value=truncate(", ".join(removed)), inline=False)
    return embed


def build_timeout_embed(user_line_: str, *, applied: bool, until_text: str | None) -> discord.Embed:
    """Timeout applied/removed log."""
    if applied:
        return make_embed(
            title="Member timed out", description=f"{user_line_}\nUntil: {until_text}", color=Colors.ORANGE
        )
    return make_embed(title="Timeout removed", description=user_line_, color=Colors.GREEN)


def build_audit_action_embed(action: str, target_line: str, moderator_line: str, reason: str | None) -> discord.Embed:
    """Human-performed ban/unban/kick log, sourced from the audit log."""
    embed = make_embed(title=action, color=Colors.RED)
    embed.add_field(name="Target", value=target_line, inline=False)
    embed.add_field(name="Moderator", value=moderator_line, inline=False)
    embed.add_field(name="Reason", value=reason or "*(none given)*", inline=False)
    return embed


def channel_kind(channel_type: discord.ChannelType) -> str:
    """Human label for a channel type: ``Text channel``, ``Stage voice channel``, ``Category``."""
    if channel_type is discord.ChannelType.category:
        return "Category"
    return f"{channel_type.name.replace('_', ' ').capitalize()} channel"


def build_channel_create_embed(kind: str, name: str) -> discord.Embed:
    """Channel-create log."""
    return make_embed(title="Channel created", description=f"{kind} **#{name}**", color=Colors.GREEN)


def build_channel_delete_embed(kind: str, name: str) -> discord.Embed:
    """Channel-delete log."""
    return make_embed(title="Channel deleted", description=f"{kind} **#{name}**", color=Colors.RED)


def build_channel_update_embed(name: str, changes: Sequence[tuple[str, str, str]]) -> discord.Embed | None:
    """Channel-update log (name/topic/category), or ``None`` if nothing tracked changed."""
    if not changes:
        return None
    embed = make_embed(title="Channel updated", description=f"**#{name}**", color=Colors.SLATE)
    for field, before, after in changes:
        embed.add_field(name=field, value=f"{truncate(before, 256)} → {truncate(after, 256)}", inline=False)
    return embed


def build_role_create_embed(name: str) -> discord.Embed:
    """Role-create log."""
    return make_embed(title="Role created", description=f"**{name}**", color=Colors.GREEN)


def build_role_delete_embed(name: str) -> discord.Embed:
    """Role-delete log."""
    return make_embed(title="Role deleted", description=f"**{name}**", color=Colors.RED)


def build_role_update_detail_embed(name: str, changes: Sequence[tuple[str, str, str]]) -> discord.Embed | None:
    """Role-update log (name/color/permissions/hoist), or ``None`` if nothing tracked changed."""
    if not changes:
        return None
    embed = make_embed(title="Role updated", description=f"**{name}**", color=Colors.SLATE)
    for field, before, after in changes:
        embed.add_field(name=field, value=f"{truncate(before, 256)} → {truncate(after, 256)}", inline=False)
    return embed


def build_voice_embed(
    user_line_: str, *, before_channel: str | None, after_channel: str | None
) -> discord.Embed | None:
    """Voice join/leave/move log, or ``None`` if the channel didn't actually change."""
    if before_channel == after_channel:
        return None
    if before_channel is None:
        return make_embed(title="Voice join", description=f"{user_line_} joined {after_channel}", color=Colors.GREEN)
    if after_channel is None:
        return make_embed(title="Voice leave", description=f"{user_line_} left {before_channel}", color=Colors.RED)
    return make_embed(
        title="Voice move", description=f"{user_line_} moved {before_channel} → {after_channel}", color=Colors.BLUE
    )


class LoggingCog(commands.Cog):
    """Server-log listeners plus the ``/logging`` admin group."""

    logging_group = app_commands.Group(
        name="logging",
        description="Configure the server activity log.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    # --- Messages ---------------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Log a deleted message, using the cache when available."""
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        cached = payload.cached_message
        if cached is not None and cached.author.bot:
            return
        channel_mention = f"<#{payload.channel_id}>"
        embed = build_message_delete_embed(
            channel_mention=channel_mention,
            author_line=user_line(cached.author) if cached else None,
            content=cached.content if cached else None,
            attachment_names=[a.filename for a in cached.attachments] if cached else [],
            cached=cached is not None,
        )
        await self._send(guild, embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        """Log a bulk delete with a transcript of whatever was cached."""
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        channel_mention = f"<#{payload.channel_id}>"
        embed = build_bulk_delete_embed(channel_mention, len(payload.message_ids))
        transcript = build_transcript_text(
            [(user_line(m.author), m.content) for m in payload.cached_messages if not m.author.bot]
        )
        file = discord.File(io.BytesIO(transcript.encode()), filename="transcript.txt") if transcript else None
        await self._send(guild, embed, file=file)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        """Log an edit, skipping bots, DMs, and unfurl-only updates (content unchanged)."""
        if after.guild is None or after.author.bot:
            return
        embed = build_message_edit_embed(
            channel_mention=f"<#{after.channel.id}>",
            author_line=user_line(after.author),
            before=before.content,
            after=after.content,
        )
        if embed is not None:
            await self._send(after.guild, embed)

    # --- Members ------------------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Log a join, flagging very new accounts."""
        age_text, is_new = format_account_age(member.created_at)
        await self._send(member.guild, build_member_join_embed(user_line(member), age_text, is_new=is_new))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Log a leave."""
        await self._send(member.guild, build_member_leave_embed(user_line(member)))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Diff nickname, roles, and timeout between member-update events."""
        if before.nick != after.nick:
            await self._send(after.guild, build_nickname_change_embed(user_line(after), before.nick, after.nick))
        added, removed = diff_roles([(r.id, r.name) for r in before.roles], [(r.id, r.name) for r in after.roles])
        role_embed = build_role_update_embed(user_line(after), added, removed)
        if role_embed is not None:
            await self._send(after.guild, role_embed)
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until is not None:
                until_text = discord.utils.format_dt(after.timed_out_until, style="F")
                embed = build_timeout_embed(user_line(after), applied=True, until_text=until_text)
            else:
                embed = build_timeout_embed(user_line(after), applied=False, until_text=None)
            await self._send(after.guild, embed)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        """Log human-performed bans/unbans/kicks (the moderation plugin logs its own actions as cases)."""
        actions = {
            discord.AuditLogAction.ban: "Member banned",
            discord.AuditLogAction.unban: "Member unbanned",
            discord.AuditLogAction.kick: "Member kicked",
        }
        title = actions.get(entry.action)
        if title is None:
            return
        if entry.user is not None and self.bot.user is not None and entry.user.id == self.bot.user.id:
            return
        is_user_target = isinstance(entry.target, discord.Member | discord.User)
        target_line = user_line(entry.target) if is_user_target else str(entry.target)
        moderator_line = user_line(entry.user) if entry.user else "Unknown"
        await self._send(entry.guild, build_audit_action_embed(title, target_line, moderator_line, entry.reason))

    # --- Channels -------------------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        """Log a channel creation."""
        await self._send(channel.guild, build_channel_create_embed(channel_kind(channel.type), channel.name))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """Log a channel deletion."""
        await self._send(channel.guild, build_channel_delete_embed(channel_kind(channel.type), channel.name))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        """Log name/topic/category changes."""
        before_fields = {
            "Name": before.name,
            "Topic": getattr(before, "topic", None) or "",
            "Category": before.category.name if before.category else "(none)",
        }
        after_fields = {
            "Name": after.name,
            "Topic": getattr(after, "topic", None) or "",
            "Category": after.category.name if after.category else "(none)",
        }
        embed = build_channel_update_embed(after.name, diff_fields(before_fields, after_fields))
        if embed is not None:
            await self._send(after.guild, embed)

    # --- Roles --------------------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        """Log a role creation."""
        await self._send(role.guild, build_role_create_embed(role.name))

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        """Log a role deletion."""
        await self._send(role.guild, build_role_delete_embed(role.name))

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        """Log name/color/permissions/hoist changes."""
        before_fields = {
            "Name": before.name,
            "Color": str(before.colour),
            "Permissions": str(before.permissions.value),
            "Hoisted": str(before.hoist),
        }
        after_fields = {
            "Name": after.name,
            "Color": str(after.colour),
            "Permissions": str(after.permissions.value),
            "Hoisted": str(after.hoist),
        }
        embed = build_role_update_detail_embed(after.name, diff_fields(before_fields, after_fields))
        if embed is not None:
            await self._send(after.guild, embed)

    # --- Voice ----------------------------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        """Log voice join/leave/move."""
        embed = build_voice_embed(
            user_line(member),
            before_channel=before.channel.mention if before.channel else None,
            after_channel=after.channel.mention if after.channel else None,
        )
        if embed is not None:
            await self._send(member.guild, embed)

    # --- /logging group -----------------------------------------------------------------------------------------

    @logging_group.command(name="channel", description="Set (or clear) the server activity log channel.")
    @app_commands.describe(channel="Text channel to post logs to; omit to disable logging.")
    async def logging_channel(
        self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel | None = None
    ) -> None:
        """Update ``log_channel_id``."""
        if interaction.guild is None:
            return
        await interaction.client.configs.update(
            interaction.guild.id, GuildConfigUpdate(log_channel_id=channel.id if channel else None)
        )
        message = f"Server log will post to {channel.mention}." if channel else "Server logging disabled."
        await interaction.response.send_message(message, ephemeral=True)

    @logging_group.command(name="status", description="Show the current log channel and verify it's usable.")
    async def logging_status(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Report the configured channel and whether the bot can actually post there."""
        if interaction.guild is None:
            return
        config = await interaction.client.configs.get(interaction.guild.id)
        if config.log_channel_id is None:
            await interaction.response.send_message("Server logging is not configured.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(config.log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                f"Log channel is set to `{config.log_channel_id}` but it no longer exists.", ephemeral=True
            )
            return
        me = interaction.guild.me if interaction.guild else None
        perms = channel.permissions_for(me) if me else discord.Permissions.none()
        if perms.send_messages and perms.embed_links:
            status = "✅ I can post there."
        else:
            pairs = (("Send Messages", perms.send_messages), ("Embed Links", perms.embed_links))
            missing = ", ".join(name for name, has in pairs if not has)
            status = f"⚠️ Missing permissions: {missing}."
        await interaction.response.send_message(f"Log channel: {channel.mention}\n{status}", ephemeral=True)

    async def _send(self, guild: discord.Guild, embed: discord.Embed, *, file: discord.File | None = None) -> None:
        try:
            if file is not None:
                config = await self.bot.configs.get(guild.id)
                if config.log_channel_id is not None:
                    log_channel = guild.get_channel(config.log_channel_id)
                    if isinstance(log_channel, discord.TextChannel):
                        await log_channel.send(embed=embed, file=file, allowed_mentions=discord.AllowedMentions.none())
                        return
            await self.bot.send_log(guild, embed)
        except discord.HTTPException:
            logger.warning("Failed to post log entry in guild %s", guild.id, exc_info=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(LoggingCog(bot))
