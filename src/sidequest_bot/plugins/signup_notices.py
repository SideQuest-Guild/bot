"""New-signup officer notices: an embed with Approve / More info buttons posted for every first-time sign-in.

Every 60 s, for each guild with ``signup_notice_channel_id`` configured: pull members with no notice posted yet
(``GET /api/guilds/{guild_id}/signup-notices``), post one embed per member with a persistent **Approve** button
(``sq:sn:approve:<member_id>``) and a **More info** link button to the member's detail on the Officers page, then
ack each post so it isn't posted again. Also pulls posted notices that were confirmed since (e.g. on the site) and
edits their message to say so, so officers reading the channel never see a stale "Guest" notice for someone already
on the roster. Clicking Approve re-checks the clicker holds a ``WEB_GM_ROLE_IDS`` role, then calls the same
roster-confirmation the GM console uses (identified by the clicker's Discord id), and edits the message in place.
"""

import logging
import re
from functools import lru_cache
from typing import Any, Final, Self

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed
from sidequest_common.schemas import (
    ConfirmByDiscordRequest,
    GuildConfigUpdate,
    SignupNoticeAck,
    SignupNoticeCandidate,
    SignupNoticeResolution,
)
from sidequest_common.settings import WebSettings

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60

CUSTOM_ID_RE: Final = re.compile(r"^sq:sn:approve:(?P<member_id>\d+)$")

STATUS_FIELD_NAME = "Status"
GUEST_STATUS_TEXT = "Guest until a Quest Master approves."


@lru_cache(maxsize=1)
def _web_settings() -> WebSettings:
    """``WebSettings`` (``WEB_*`` env vars), shared with the API: ``BASE_URL`` and ``GM_ROLE_IDS``."""
    return WebSettings()


def build_custom_id(member_id: int) -> str:
    """The ``custom_id`` for one member's Approve button."""
    return f"sq:sn:approve:{member_id}"


def parse_custom_id(custom_id: str) -> int | None:
    """The member id encoded in ``custom_id``, or ``None`` if it doesn't match our template."""
    match = CUSTOM_ID_RE.match(custom_id)
    if match is None:
        return None
    return int(match["member_id"])


def avatar_url(discord_id: int, avatar_hash: str | None) -> str:
    """Discord CDN avatar URL; falls back to Discord's default avatar if the member has none set."""
    if avatar_hash:
        ext = "gif" if avatar_hash.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.{ext}"
    index = (discord_id >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{index}.png"


def more_info_url(base_url: str, member_id: int) -> str:
    """Deep link to this member's detail on the Officers page's Members tab."""
    return f"{base_url.rstrip('/')}/officers/#members?member={member_id}"


def clicker_is_gm(role_ids: set[int], gm_role_ids: list[int]) -> bool:
    """Whether a Discord member holding ``role_ids`` may approve a signup notice."""
    return bool(set(gm_role_ids) & role_ids)


def build_notice_embed(candidate: SignupNoticeCandidate) -> discord.Embed:
    """The notice embed for one new sign-in: name, avatar, BattleTag if linked, and a Guest status field."""
    embed = make_embed(
        "New signature in the Ledger",
        f"{candidate.display_name} (@{candidate.username})",
        color=Colors.SITE_GOLD,
    )
    embed.set_thumbnail(url=avatar_url(int(candidate.discord_id), candidate.avatar_hash))
    if candidate.battletag:
        embed.add_field(name="BattleTag", value=candidate.battletag, inline=True)
    embed.add_field(name=STATUS_FIELD_NAME, value=GUEST_STATUS_TEXT, inline=False)
    return embed


def apply_approval_status(embed: discord.Embed, status_text: str) -> discord.Embed:
    """Replace ``embed``'s Status field with ``status_text`` in place (appends one if somehow missing)."""
    for index, field in enumerate(embed.fields):
        if field.name == STATUS_FIELD_NAME:
            embed.set_field_at(index, name=STATUS_FIELD_NAME, value=status_text, inline=False)
            return embed
    embed.add_field(name=STATUS_FIELD_NAME, value=status_text, inline=False)
    return embed


def build_notice_view(member_id: int, more_info_link: str, *, approved: bool = False) -> discord.ui.View:
    """Approve (disabled once ``approved``) plus a link button to the member's detail on the Officers page."""
    view = discord.ui.View(timeout=None)
    view.add_item(SignupApproveButton(member_id, disabled=approved))
    view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label="More info", url=more_info_link))
    return view


class SignupApproveButton(discord.ui.DynamicItem[discord.ui.Button], template=CUSTOM_ID_RE):
    """Persistent Approve button; survives bot restarts like ``role_menus.RoleMenuButton``."""

    def __init__(self, member_id: int, *, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="Approve",
                custom_id=build_custom_id(member_id),
                disabled=disabled,
            )
        )
        self.member_id = member_id

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        """Rebuild from a dispatched component after a restart."""
        return cls(int(match["member_id"]), disabled=getattr(item, "disabled", False))

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Re-check the clicker is a GM, confirm the member on the roster, then edit the message in place."""
        await interaction.response.defer(ephemeral=True, thinking=False)
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return
        if not clicker_is_gm({role.id for role in interaction.user.roles}, _web_settings().GM_ROLE_IDS):
            await interaction.followup.send("Only Quest Masters can approve.", ephemeral=True)
            return
        bot = interaction.client
        request = ConfirmByDiscordRequest(
            actor_discord_id=str(interaction.user.id), actor_name=interaction.user.display_name
        )
        try:
            await bot.api.confirm_roster_by_discord(guild.id, interaction.user.id, request)
        except NotFoundError:
            await interaction.followup.send("That signup record no longer exists.", ephemeral=True)
            return
        except APIError:
            logger.warning("Failed to confirm roster for member %s via signup notice", self.member_id, exc_info=True)
            await interaction.followup.send("Couldn't reach the guild records; try again shortly.", ephemeral=True)
            return
        await self._mark_message_approved(bot, guild, interaction.message, f"Approved by {interaction.user.mention}")
        await interaction.followup.send("Approved.", ephemeral=True)

    async def _mark_message_approved(
        self, bot: SideQuestBot, guild: discord.Guild, message: discord.Message | None, status_text: str
    ) -> None:
        if message is None or not message.embeds:
            return
        updated_embed = apply_approval_status(message.embeds[0], status_text)
        view = build_notice_view(self.member_id, more_info_url(_web_settings().BASE_URL, self.member_id), approved=True)
        try:
            await message.edit(embed=updated_embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.warning("Failed to edit signup notice message for member %s", self.member_id, exc_info=True)
            return
        try:
            await bot.api.ack_signup_notice(
                guild.id, self.member_id, SignupNoticeAck(channel_id=message.channel.id, message_id=message.id)
            )
        except APIError:
            logger.warning("Failed to ack signup notice for member %s", self.member_id, exc_info=True)


class SignupNoticesCog(commands.Cog):
    """Periodic signup-notice poll plus the ``/signups channel`` admin command."""

    signups_group = app_commands.Group(
        name="signups",
        description="Configure where new-signup notices post for Quest Masters to approve.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._not_site_guild_ids: set[int] = set()
        self._poll_loop.start()

    async def cog_unload(self) -> None:
        """Stop the poll loop when the cog is unloaded."""
        self._poll_loop.cancel()

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def _poll_loop(self) -> None:
        for guild in list(self.bot.guilds):
            if guild.id in self._not_site_guild_ids:
                continue
            try:
                await self._poll_guild(guild)
            except NotFoundError:
                logger.info("Guild %s is not the site guild; no longer polling signup notices there", guild.id)
                self._not_site_guild_ids.add(guild.id)
            except APIError:
                # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
                logger.warning("Signup notice poll failed for guild %s", guild.id, exc_info=True)

    @_poll_loop.before_loop
    async def _before_poll_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _poll_guild(self, guild: discord.Guild) -> None:
        config = await self.bot.configs.get(guild.id)
        if config.signup_notice_channel_id is None:
            return
        channel = guild.get_channel(config.signup_notice_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        page = await self.bot.api.list_signup_notices(guild.id)
        for candidate in page.pending:
            await self._post_notice(guild, channel, candidate)
        for resolution in page.resolved:
            await self._resolve_notice(guild, channel, resolution)

    async def _post_notice(
        self, guild: discord.Guild, channel: discord.TextChannel, candidate: SignupNoticeCandidate
    ) -> None:
        embed = build_notice_embed(candidate)
        view = build_notice_view(candidate.member_id, more_info_url(_web_settings().BASE_URL, candidate.member_id))
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            logger.warning(
                "Failed to post signup notice for member %s in guild %s", candidate.member_id, guild.id, exc_info=True
            )
            return
        try:
            await self.bot.api.ack_signup_notice(
                guild.id, candidate.member_id, SignupNoticeAck(channel_id=message.channel.id, message_id=message.id)
            )
        except APIError:
            logger.warning("Failed to ack signup notice for member %s", candidate.member_id, exc_info=True)

    async def _resolve_notice(
        self, guild: discord.Guild, channel: discord.TextChannel, resolution: SignupNoticeResolution
    ) -> None:
        try:
            message = await channel.fetch_message(int(resolution.message_id))
        except discord.HTTPException:
            logger.warning("Failed to fetch signup notice message for member %s", resolution.member_id, exc_info=True)
            return
        if not message.embeds:
            return
        who = resolution.confirmed_by or "a Quest Master"
        updated_embed = apply_approval_status(message.embeds[0], f"Approved on the site by {who}")
        view = build_notice_view(
            resolution.member_id, more_info_url(_web_settings().BASE_URL, resolution.member_id), approved=True
        )
        try:
            await message.edit(embed=updated_embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.warning("Failed to edit signup notice message for member %s", resolution.member_id, exc_info=True)
            return
        try:
            await self.bot.api.ack_signup_notice(
                guild.id,
                resolution.member_id,
                SignupNoticeAck(channel_id=int(resolution.channel_id), message_id=int(resolution.message_id)),
            )
        except APIError:
            logger.warning("Failed to ack resolved signup notice for member %s", resolution.member_id, exc_info=True)

    @signups_group.command(name="channel", description="Set (or clear) the channel for new-signup approval notices.")
    @app_commands.describe(channel="Channel to post signup notices in; omit to disable.")
    async def signups_channel(
        self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel | None = None
    ) -> None:
        """Update ``signup_notice_channel_id``."""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.client.configs.update(
            guild.id, GuildConfigUpdate(signup_notice_channel_id=channel.id if channel else None)
        )
        message = (
            f"New signups will post in {channel.mention} for Quest Masters to approve."
            if channel
            else "Signup notices disabled."
        )
        await interaction.response.send_message(message, ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the persistent button handler and the cog."""
    bot.add_dynamic_items(SignupApproveButton)
    await bot.add_cog(SignupNoticesCog(bot))
