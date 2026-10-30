"""A private welcome for members who just signed in on the site.

``link_roles`` fires :func:`send_guest_welcome_dm` the moment it grants a member the site link role — the gate ->
guest transition. That's derived straight from the role grant itself rather than a separate "already welcomed" flag:
once the role is on the member, ``plan_role_changes`` never lists it in ``to_add`` again, so the DM can't repeat on
later sync ticks, and a bot restart changes nothing because the role (not bot memory) is the source of truth. A
closed DM (``discord.Forbidden``) or any other HTTP error is only logged; it never interrupts the role sync.

Discord won't let the bot send an unprompted ephemeral message, so the same welcome is also reachable on demand: a
persistent "Start here" button, posted and pinned in a guest channel via ``/guestwelcome post``, replies ephemerally
with the identical embed built by :func:`build_guest_welcome_embed`. Both also show a "We're looking for" field
when officers have set recruiting needs (``GET /api/recruiting``); a failed fetch just omits the field.
"""

import logging
import re
from typing import Any, Final, Self

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError, SideQuestAPI
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate
from sidequest_common.schemas import Recruiting, RecruitingPriority

logger = logging.getLogger(__name__)

CUSTOM_ID: Final = "sq:welcome"
CUSTOM_ID_RE: Final = re.compile(r"^sq:welcome$")

CHOOSE_CLASS_CHANNEL_NAME = "choose-your-class"
EVENTS_URL = "https://sidequest-guild.com/events/"

PINNED_POST_CONTENT = "New here? Click below for a quick welcome and pointers to get started."


def _choose_class_channel_mention(guild: discord.Guild) -> str:
    """``#choose-your-class``'s mention if the channel exists in ``guild``, else the plain ``#name`` text."""
    channel = discord.utils.get(guild.text_channels, name=CHOOSE_CLASS_CHANNEL_NAME)
    return channel.mention if channel is not None else f"#{CHOOSE_CLASS_CHANNEL_NAME}"


def _recruiting_field_text(recruiting: Recruiting) -> str | None:
    """A comma-separated "We're looking for" line (roles, then classes), bound to Discord's 1,024-char field limit.

    ``None`` if nothing is wanted. Up to 3 roles and 9 classes, each with up to a 140-char note, can comfortably
    exceed the limit, so the joined text is truncated (with an ellipsis) rather than sent as an oversized field
    that would get the whole embed rejected.
    """

    def want_text(label: str, priority: RecruitingPriority, note: str | None) -> str:
        text = f"**{label}**" if priority == RecruitingPriority.HIGH else label
        return f"{text} ({note})" if note else text

    parts = [want_text(want.role.value.capitalize(), want.priority, want.note) for want in recruiting.roles]
    parts += [want_text(want.wow_class.value.capitalize(), want.priority, want.note) for want in recruiting.classes]
    return truncate(", ".join(parts)) if parts else None


async def _fetch_recruiting(api: SideQuestAPI) -> Recruiting | None:
    """The current recruiting needs, or ``None`` if the API call fails."""
    try:
        return await api.get_recruiting()
    except APIError:
        logger.warning("Failed to fetch recruiting needs for guest welcome", exc_info=True)
        return None


def build_guest_welcome_embed(member: discord.Member, recruiting: Recruiting | None = None) -> discord.Embed:
    """The welcome embed for ``member``, used for both the sign-in DM and the button's ephemeral reply.

    ``recruiting`` adds a "We're looking for" field when officers have wanted roles/classes set.
    """
    description = (
        f"Hey {member.display_name}, welcome! You're in as a guest, and a Quest Master will confirm you as a full "
        f"guild member soon.\n\n"
        f"Head to {_choose_class_channel_mention(member.guild)} to pick a class role, and keep an eye on "
        f"{EVENTS_URL} for upcoming raids and events."
    )
    embed = make_embed("Welcome to the guild!", description, color=Colors.GOLD)
    field_text = _recruiting_field_text(recruiting) if recruiting is not None else None
    if field_text:
        embed.add_field(name="We're looking for", value=field_text, inline=False)
    return embed


async def send_guest_welcome_dm(member: discord.Member, api: SideQuestAPI) -> None:
    """DM the welcome embed to ``member``. Closed DMs and other HTTP errors are logged, never raised."""
    embed = build_guest_welcome_embed(member, await _fetch_recruiting(api))
    try:
        await member.send(embed=embed)
    except discord.Forbidden:
        logger.info("Could not DM guest welcome to %s in guild %s: DMs are closed", member.id, member.guild.id)
    except discord.HTTPException:
        logger.warning("Failed to DM guest welcome to %s in guild %s", member.id, member.guild.id, exc_info=True)


def build_guest_welcome_view() -> discord.ui.View:
    """A persistent view holding the "Start here" button, for the pinned post."""
    view = discord.ui.View(timeout=None)
    view.add_item(GuestWelcomeButton())
    return view


class GuestWelcomeButton(discord.ui.DynamicItem[discord.ui.Button], template=CUSTOM_ID_RE):
    """Persistent "Start here" button; carries no state, so it survives restarts untouched."""

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(style=discord.ButtonStyle.primary, label="Start here", custom_id=CUSTOM_ID))

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[Any], _item: discord.ui.Item[Any], _match: re.Match[str], /
    ) -> Self:
        """Rebuild after a restart; there's nothing to recover, the button is stateless."""
        return cls()

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Defer ephemerally, then reply with the same welcome embed sent by DM on sign-in.

        Deferring before the recruiting fetch keeps this within Discord's 3-second ack window even if the API
        call is slow or the API is mid-deploy outage (it has a 10s timeout and retries).
        """
        if not isinstance(interaction.user, discord.Member):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = build_guest_welcome_embed(interaction.user, await _fetch_recruiting(interaction.client.api))
        await interaction.followup.send(embed=embed, ephemeral=True)


class GuestWelcomeCog(commands.Cog):
    """``/guestwelcome post`` admin command: posts and pins the "Start here" button."""

    guestwelcome_group = app_commands.Group(
        name="guestwelcome",
        description="Post the pinned guest welcome button.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    @guestwelcome_group.command(
        name="post", description="Post and pin the guest welcome button in a channel (e.g. #guest-chat)."
    )
    @app_commands.describe(channel="Channel to post the welcome button in.")
    async def post(self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel) -> None:
        """Post the pinned prompt with its button, then pin the message."""
        await interaction.response.defer(ephemeral=True)
        view = build_guest_welcome_view()
        try:
            message = await channel.send(PINNED_POST_CONTENT, view=view)
            await message.pin()
        except discord.HTTPException:
            logger.warning("Failed to post/pin guest welcome button in channel %s", channel.id, exc_info=True)
            await interaction.followup.send("Couldn't post or pin the message there.", ephemeral=True)
            return
        await interaction.followup.send(f"Posted and pinned the welcome button in {channel.mention}.", ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the persistent button handler and the cog."""
    bot.add_dynamic_items(GuestWelcomeButton)
    await bot.add_cog(GuestWelcomeCog(bot))
