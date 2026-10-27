"""Moderation commands: warn, timeout, kick, ban (and their reversals), plus case lookup/editing.

Every punitive action follows the same shape: hierarchy check (fast, before the 3s ack deadline), a defer, then a
best-effort DM to the target, the actual Discord action, a recorded case via the API, and a log embed, all replied
to via followup. Duration parsing and the hierarchy guard are kept as pure, importable functions so they can be
unit tested without a live Discord connection.
"""

import contextlib
import logging
import re
from datetime import timedelta
from typing import cast

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate, user_line
from sidequest_common.schemas import CaseAction, ModCaseCreate, ModCaseSchema, ModCaseUpdate

logger = logging.getLogger(__name__)

MAX_TIMEOUT_DURATION = timedelta(days=28)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_FORMAT_PATTERN = re.compile(r"(\d+[smhd])+")
_DURATION_TOKEN_PATTERN = re.compile(r"(\d+)([smhd])")

_ACTION_COLORS = {
    CaseAction.WARN: Colors.ORANGE,
    CaseAction.TIMEOUT: Colors.ORANGE,
    CaseAction.UNTIMEOUT: Colors.GREEN,
    CaseAction.KICK: Colors.RED,
    CaseAction.BAN: Colors.RED,
    CaseAction.UNBAN: Colors.GREEN,
    CaseAction.AUTOMOD: Colors.PURPLE,
}
_ACTION_PAST_TENSE = {
    CaseAction.WARN: "warned",
    CaseAction.TIMEOUT: "timed out",
    CaseAction.UNTIMEOUT: "had your timeout removed",
    CaseAction.KICK: "kicked",
    CaseAction.BAN: "banned",
    CaseAction.UNBAN: "unbanned",
}


# --- Pure helpers (unit tested directly) -----------------------------------------------------------------------


def parse_duration(text: str) -> timedelta:
    """Parse strings like ``10m``, ``1h30m``, ``2d`` into a :class:`timedelta`.

    Raises:
        ValueError: the text doesn't match ``<number><unit>`` tokens, is zero, or exceeds 28 days.
    """
    cleaned = text.strip().lower().replace(" ", "")
    if not cleaned or not _DURATION_FORMAT_PATTERN.fullmatch(cleaned):
        msg = f"Invalid duration {text!r}. Use a combination like 10m, 1h30m, or 2d."
        raise ValueError(msg)
    total_seconds = sum(int(value) * _UNIT_SECONDS[unit] for value, unit in _DURATION_TOKEN_PATTERN.findall(cleaned))
    if total_seconds <= 0:
        msg = "Duration must be greater than zero."
        raise ValueError(msg)
    duration = timedelta(seconds=total_seconds)
    if duration > MAX_TIMEOUT_DURATION:
        msg = f"Timeout duration can't exceed {MAX_TIMEOUT_DURATION.days} days."
        raise ValueError(msg)
    return duration


def format_duration(total_seconds: int) -> str:
    """Compact duration string, e.g. ``5400`` -> ``1h30m``. ``0`` -> ``0s``."""
    remaining = total_seconds
    parts: list[str] = []
    for unit, unit_seconds in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        value, remaining = divmod(remaining, unit_seconds)
        if value:
            parts.append(f"{value}{unit}")
    return "".join(parts) or "0s"


def hierarchy_violation(
    *,
    actor_id: int,
    actor_top_role_position: int,
    actor_is_owner: bool,
    target_id: int,
    target_top_role_position: int,
    target_is_owner: bool,
    bot_id: int,
    bot_top_role_position: int,
) -> str | None:
    """The reason the action is refused, or ``None`` if it's allowed.

    Refuses acting on self, the bot, the guild owner, or a member whose top role is at or above the invoker's
    (unless the invoker is the owner) or at or above the bot's.
    """
    if target_id == actor_id:
        return "You can't target yourself."
    if target_id == bot_id:
        return "I can't target myself."
    if target_is_owner:
        return "You can't target the server owner."
    if not actor_is_owner and target_top_role_position >= actor_top_role_position:
        return "You can't target someone with a role equal to or higher than yours."
    if target_top_role_position >= bot_top_role_position:
        return "I can't target someone with a role equal to or higher than mine."
    return None


def resolve_violation(
    *, actor: discord.Member, target_id: int, target_member: discord.Member | None, bot_member: discord.Member
) -> str | None:
    """Hierarchy check for a target; falls back to self/bot-only checks when the target isn't a guild member."""
    guild = actor.guild
    return hierarchy_violation(
        actor_id=actor.id,
        actor_top_role_position=actor.top_role.position,
        actor_is_owner=actor.id == guild.owner_id,
        target_id=target_id,
        target_top_role_position=target_member.top_role.position if target_member else -1,
        target_is_owner=target_member.id == guild.owner_id if target_member else False,
        bot_id=bot_member.id,
        bot_top_role_position=bot_member.top_role.position,
    )


async def resolve_target_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    """The guild member for ``user_id``, or ``None`` if confirmed not a member.

    Checks the cache first; falls back to a fetch so a cache miss doesn't get mistaken for "not a member" (which
    would skip the hierarchy check entirely).
    """
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


# --- Embed builders ----------------------------------------------------------------------------------------------


def build_case_embed(case: ModCaseSchema, *, target_line: str, moderator_line: str) -> discord.Embed:
    """``Case #N | Action`` embed with member, moderator, optional duration, and reason."""
    embed = make_embed(
        title=f"Case #{case.case_number} | {case.action.value.capitalize()}",
        color=_ACTION_COLORS.get(case.action, Colors.PURPLE),
    )
    embed.add_field(name="Member", value=target_line, inline=False)
    embed.add_field(name="Moderator", value=moderator_line, inline=False)
    if case.duration_seconds:
        embed.add_field(name="Duration", value=format_duration(case.duration_seconds), inline=False)
    embed.add_field(name="Reason", value=truncate(case.reason or "No reason provided."), inline=False)
    return embed


def build_case_list_embed(cases: list[ModCaseSchema], *, member_line: str | None) -> discord.Embed:
    """Compact list of recent cases, newest first."""
    title = f"Cases for {member_line}" if member_line else "Recent cases"
    lines = [
        f"**#{case.case_number}** {case.action.value} <@{case.user_id}> — {truncate(case.reason or 'No reason', 200)}"
        for case in cases
    ]
    return make_embed(title=title, description="\n".join(lines), color=Colors.PURPLE)


# --- Cog ----------------------------------------------------------------------------------------------------------


class ModerationCog(commands.Cog):
    """Punitive slash commands plus case lookup/editing."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @app_commands.command(name="warn", description="Warn a member.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.describe(member="Member to warn.", reason="Why they're being warned.")
    async def warn(self, interaction: discord.Interaction[SideQuestBot], member: discord.Member, reason: str) -> None:
        """Record a warning with no other side effect."""
        guild = interaction.guild
        assert guild is not None  # noqa: S101 — guaranteed by guild_only()
        actor = cast("discord.Member", interaction.user)
        violation = resolve_violation(actor=actor, target_id=member.id, target_member=member, bot_member=guild.me)
        if violation:
            await interaction.response.send_message(violation, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._dm_target(member, guild_name=guild.name, action=CaseAction.WARN, reason=reason)
        case = await self._record_case(
            guild,
            action=CaseAction.WARN,
            target_id=member.id,
            target_line=user_line(member),
            moderator=actor,
            reason=reason,
        )
        await interaction.followup.send(f"Warned {member.mention} (case #{case.case_number}).", ephemeral=True)

    @app_commands.command(name="timeout", description="Time out a member.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.describe(
        member="Member to time out.", duration="e.g. 10m, 1h30m, 2d (max 28d).", reason="Why they're being timed out."
    )
    async def timeout(
        self, interaction: discord.Interaction[SideQuestBot], member: discord.Member, duration: str, reason: str
    ) -> None:
        """Apply a Discord timeout for ``duration``."""
        guild = interaction.guild
        assert guild is not None  # noqa: S101
        actor = cast("discord.Member", interaction.user)
        violation = resolve_violation(actor=actor, target_id=member.id, target_member=member, bot_member=guild.me)
        if violation:
            await interaction.response.send_message(violation, ephemeral=True)
            return
        try:
            parsed = parse_duration(duration)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._dm_target(member, guild_name=guild.name, action=CaseAction.TIMEOUT, reason=reason, duration=parsed)
        await member.timeout(parsed, reason=reason)
        case = await self._record_case(
            guild,
            action=CaseAction.TIMEOUT,
            target_id=member.id,
            target_line=user_line(member),
            moderator=actor,
            reason=reason,
            duration_seconds=int(parsed.total_seconds()),
        )
        duration_text = format_duration(int(parsed.total_seconds()))
        await interaction.followup.send(
            f"Timed out {member.mention} for {duration_text} (case #{case.case_number}).", ephemeral=True
        )

    @app_commands.command(name="untimeout", description="Remove a member's timeout.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.describe(member="Member to un-timeout.", reason="Why.")
    async def untimeout(
        self, interaction: discord.Interaction[SideQuestBot], member: discord.Member, reason: str | None = None
    ) -> None:
        """Clear a Discord timeout early."""
        guild = interaction.guild
        assert guild is not None  # noqa: S101
        actor = cast("discord.Member", interaction.user)
        violation = resolve_violation(actor=actor, target_id=member.id, target_member=member, bot_member=guild.me)
        if violation:
            await interaction.response.send_message(violation, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._dm_target(member, guild_name=guild.name, action=CaseAction.UNTIMEOUT, reason=reason)
        await member.timeout(None, reason=reason)
        case = await self._record_case(
            guild,
            action=CaseAction.UNTIMEOUT,
            target_id=member.id,
            target_line=user_line(member),
            moderator=actor,
            reason=reason,
        )
        await interaction.followup.send(
            f"Removed {member.mention}'s timeout (case #{case.case_number}).", ephemeral=True
        )

    @app_commands.command(name="kick", description="Kick a member.")
    @app_commands.default_permissions(kick_members=True)
    @app_commands.guild_only()
    @app_commands.describe(member="Member to kick.", reason="Why.")
    async def kick(
        self, interaction: discord.Interaction[SideQuestBot], member: discord.Member, reason: str | None = None
    ) -> None:
        """Kick a member from the guild."""
        guild = interaction.guild
        assert guild is not None  # noqa: S101
        actor = cast("discord.Member", interaction.user)
        violation = resolve_violation(actor=actor, target_id=member.id, target_member=member, bot_member=guild.me)
        if violation:
            await interaction.response.send_message(violation, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._dm_target(member, guild_name=guild.name, action=CaseAction.KICK, reason=reason)
        await member.kick(reason=reason)
        case = await self._record_case(
            guild,
            action=CaseAction.KICK,
            target_id=member.id,
            target_line=user_line(member),
            moderator=actor,
            reason=reason,
        )
        await interaction.followup.send(f"Kicked {member.mention} (case #{case.case_number}).", ephemeral=True)

    @app_commands.command(name="ban", description="Ban a user (works even if they aren't currently a member).")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    @app_commands.describe(
        user="User to ban (by mention or ID).", reason="Why.", delete_message_days="Days of their messages to delete."
    )
    async def ban(
        self,
        interaction: discord.Interaction[SideQuestBot],
        user: discord.Member | discord.User,
        reason: str | None = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        """Ban ``user``, who may or may not currently be a member of the guild."""
        guild = interaction.guild
        assert guild is not None  # noqa: S101
        actor = cast("discord.Member", interaction.user)
        # Discord resolves ``user`` to a Member already when it's a current one; only fall back to a fetch on a
        # genuine cache miss, never silently treat that miss as "not a member" and skip the hierarchy check.
        target_member = user if isinstance(user, discord.Member) else await resolve_target_member(guild, user.id)
        violation = resolve_violation(actor=actor, target_id=user.id, target_member=target_member, bot_member=guild.me)
        if violation:
            await interaction.response.send_message(violation, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self._dm_target(user, guild_name=guild.name, action=CaseAction.BAN, reason=reason)
        await guild.ban(
            cast("discord.abc.Snowflake", user), reason=reason, delete_message_seconds=delete_message_days * 86400
        )
        case = await self._record_case(
            guild,
            action=CaseAction.BAN,
            target_id=user.id,
            target_line=user_line(user),
            moderator=actor,
            reason=reason,
        )
        await interaction.followup.send(f"Banned {user.mention} (case #{case.case_number}).", ephemeral=True)

    @app_commands.command(name="unban", description="Remove a ban by user ID.")
    @app_commands.default_permissions(ban_members=True)
    @app_commands.guild_only()
    @app_commands.describe(user_id="Snowflake ID of the banned user.", reason="Why.")
    async def unban(
        self, interaction: discord.Interaction[SideQuestBot], user_id: str, reason: str | None = None
    ) -> None:
        """Unban a user by raw ID (string to avoid float precision loss on large snowflakes)."""
        guild = interaction.guild
        assert guild is not None  # noqa: S101
        actor = cast("discord.Member", interaction.user)
        try:
            target_id = int(user_id)
        except ValueError:
            await interaction.response.send_message(f"`{user_id}` isn't a valid user ID.", ephemeral=True)
            return
        if target_id in (actor.id, guild.me.id):
            await interaction.response.send_message("That ID isn't a valid target.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await guild.unban(discord.Object(id=target_id), reason=reason)
        case = await self._record_case(
            guild,
            action=CaseAction.UNBAN,
            target_id=target_id,
            target_line=f"<@{target_id}>",
            moderator=actor,
            reason=reason,
        )
        await interaction.followup.send(f"Unbanned `{target_id}` (case #{case.case_number}).", ephemeral=True)

    @app_commands.command(name="cases", description="List recent moderation cases.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.describe(member="Only show cases for this member.")
    async def cases(self, interaction: discord.Interaction[SideQuestBot], member: discord.Member | None = None) -> None:
        """Up to 10 most recent cases, optionally filtered to one member."""
        guild = interaction.guild
        assert guild is not None  # noqa: S101
        recent = await self.bot.api.list_cases(guild.id, user_id=member.id if member else None, limit=10)
        if not recent:
            await interaction.response.send_message("No cases found.", ephemeral=True)
            return
        embed = build_case_list_embed(recent, member_line=user_line(member) if member else None)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="case", description="Show a single moderation case.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.describe(number="Case number.")
    async def case(self, interaction: discord.Interaction[SideQuestBot], number: int) -> None:
        """Full detail for one case."""
        guild = interaction.guild
        assert guild is not None  # noqa: S101
        found = await self.bot.api.get_case(guild.id, number)
        embed = build_case_embed(found, target_line=f"<@{found.user_id}>", moderator_line=f"<@{found.moderator_id}>")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="reason", description="Edit the reason on an existing case.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.guild_only()
    @app_commands.describe(number="Case number.", text="New reason text.")
    async def reason(self, interaction: discord.Interaction[SideQuestBot], number: int, text: str) -> None:
        """Change a case's recorded reason."""
        guild = interaction.guild
        assert guild is not None  # noqa: S101
        updated = await self.bot.api.update_case(guild.id, number, ModCaseUpdate(reason=text))
        await interaction.response.send_message(f"Updated reason on case #{updated.case_number}.", ephemeral=True)

    @staticmethod
    async def _dm_target(
        target: discord.Member | discord.User,
        *,
        guild_name: str,
        action: CaseAction,
        reason: str | None,
        duration: timedelta | None = None,
    ) -> None:
        """Best-effort DM; a target with DMs closed just doesn't get one."""
        lines = [f"You have been {_ACTION_PAST_TENSE[action]} in **{guild_name}**."]
        if duration is not None:
            lines.append(f"Duration: {format_duration(int(duration.total_seconds()))}")
        lines.append(f"Reason: {reason or 'No reason provided.'}")
        with contextlib.suppress(discord.Forbidden):
            await target.send("\n".join(lines))

    async def _record_case(
        self,
        guild: discord.Guild,
        *,
        action: CaseAction,
        target_id: int,
        target_line: str,
        moderator: discord.Member | discord.User,
        reason: str | None,
        duration_seconds: int | None = None,
    ) -> ModCaseSchema:
        """Create the case via the API and post the log embed."""
        case = await self.bot.api.create_case(
            guild.id,
            ModCaseCreate(
                action=action,
                user_id=target_id,
                moderator_id=moderator.id,
                reason=reason,
                duration_seconds=duration_seconds,
            ),
        )
        embed = build_case_embed(case, target_line=target_line, moderator_line=user_line(moderator))
        await self.bot.send_log(guild, embed)
        return case


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(ModerationCog(bot))
