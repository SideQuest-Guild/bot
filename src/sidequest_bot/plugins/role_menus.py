"""Self-assignable role menus: persistent button panels managed via ``/rolemenu``."""

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, Final, Self
from uuid import UUID

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot.api_client import APIError, NotFoundError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate
from sidequest_bot.lib.roles import configure_block_reason, grant_block_reason
from sidequest_common.schemas import RoleMenuCreate, RoleMenuOptionUpsert, RoleMenuSchema, RoleMenuUpdate

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sidequest_common.schemas import RoleMenuOptionSchema

logger = logging.getLogger(__name__)

CUSTOM_ID_RE: Final = re.compile(
    r"^sq:rm:(?P<menu_id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}):"
    r"(?P<role_id>[0-9]+)$"
)

DESCRIPTION_LIMIT: Final = 4096
"""Discord's hard cap on an embed's ``description``."""

_ROLE_TOGGLE_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
"""One lock per (guild_id, member_id) so concurrent button clicks can't both slip past the max_roles cap."""


def _lock_for(guild_id: int, member_id: int) -> asyncio.Lock:
    """The lock serializing role toggles for one member in one guild."""
    return _ROLE_TOGGLE_LOCKS.setdefault((guild_id, member_id), asyncio.Lock())


def build_custom_id(menu_id: UUID, role_id: int) -> str:
    """The ``custom_id`` for the button offering ``role_id`` on menu ``menu_id``."""
    return f"sq:rm:{menu_id}:{role_id}"


def parse_custom_id(custom_id: str) -> tuple[UUID, int] | None:
    """``(menu_id, role_id)`` if ``custom_id`` matches our template, else ``None``."""
    match = CUSTOM_ID_RE.match(custom_id)
    if match is None:
        return None
    return UUID(match["menu_id"]), int(match["role_id"])


def exceeds_max_roles(*, has_role: bool, max_roles: int, held_count: int) -> bool:
    """Would granting the role (when the member doesn't already have it) exceed ``max_roles``? 0 = unlimited."""
    if has_role or max_roles == 0:
        return False
    return held_count >= max_roles


def render_menu_embed(menu: RoleMenuSchema) -> discord.Embed:
    """The menu's message embed: title/description plus one line per option, in position order.

    The combined text is truncated to Discord's 4096-char description cap: a full menu description plus 25 options
    each with a 100-char description and a 64-char custom emoji easily exceeds it.
    """
    lines = [_render_option_line(option) for option in menu.options]
    description = menu.description or ""
    if lines:
        description = f"{description}\n\n" if description else ""
        description += "\n".join(lines)
    description = truncate(description, DESCRIPTION_LIMIT) if description else None
    return make_embed(menu.title, description, color=Colors.SITE_GOLD)


def _render_option_line(option: RoleMenuOptionSchema) -> str:
    prefix = f"{option.emoji} " if option.emoji else ""
    line = f"{prefix}<@&{option.role_id}>"
    if option.description:
        line += f" — {option.description}"
    return line


def build_menu_view(menu: RoleMenuSchema) -> discord.ui.View:
    """A persistent view with one secondary button per option, in position order."""
    view = discord.ui.View(timeout=None)
    for option in menu.options:
        view.add_item(RoleMenuButton.from_option(menu.id, option))
    return view


class RoleMenuButton(discord.ui.DynamicItem[discord.ui.Button], template=CUSTOM_ID_RE):
    """A role-toggle button whose state (menu + role) lives entirely in its ``custom_id``."""

    def __init__(self, menu_id: UUID, role_id: int, *, label: str, emoji: str | discord.PartialEmoji | None) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=label,
                emoji=emoji,
                custom_id=build_custom_id(menu_id, role_id),
            )
        )
        self.menu_id = menu_id
        self.role_id = role_id

    @classmethod
    def from_option(cls, menu_id: UUID, option: RoleMenuOptionSchema) -> Self:
        """Build the button for one menu option, for initial posting or re-rendering."""
        return cls(menu_id, option.role_id, label=option.label, emoji=option.emoji)

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[Any], item: discord.ui.Item[Any], _match: re.Match[str], /
    ) -> Self:
        """Rebuild the button from a dispatched component; label/emoji come from the live message."""
        custom_id = getattr(item, "custom_id", None) or ""
        parsed = parse_custom_id(custom_id)
        if parsed is None:  # pragma: no cover - the template already guarantees a match
            msg = f"Unroutable role menu custom_id: {custom_id!r}"
            raise ValueError(msg)
        menu_id, role_id = parsed
        label = getattr(item, "label", None) or "Role"
        emoji = getattr(item, "emoji", None)
        return cls(menu_id, role_id, label=label, emoji=emoji)

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Re-confirm the role is still offered, then toggle it on the clicking member."""
        await interaction.response.defer(ephemeral=True, thinking=False)
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return
        message = await self._resolve_and_toggle(interaction.client, guild, interaction.user.id)
        await interaction.followup.send(message, ephemeral=True)

    async def _resolve_and_toggle(self, bot: SideQuestBot, guild: discord.Guild, member_id: int) -> str:
        """Re-fetch the menu, validate the click, and toggle the role under a per-member lock."""
        menu = await self._fetch_offering_menu(bot, guild)
        if isinstance(menu, str):
            return menu
        role = self._resolve_grantable_role(guild)
        if isinstance(role, str):
            return role
        async with _lock_for(guild.id, member_id):
            member = await self._fetch_authoritative_member(guild, member_id)
            if isinstance(member, str):
                return member
            cap_message = self._cap_message(menu, guild, member, role)
            if cap_message is not None:
                return cap_message
            return await self._toggle_role(member, role)

    async def _fetch_authoritative_member(self, guild: discord.Guild, member_id: int) -> discord.Member | str:
        """Fresh (non-cached) role state for the clicking member, fetched inside the per-member lock."""
        try:
            return await guild.fetch_member(member_id)
        except discord.HTTPException:
            logger.warning("Could not fetch member %s in guild %s", member_id, guild.id, exc_info=True)
            return "Couldn't verify your roles; try again shortly."

    async def _fetch_offering_menu(self, bot: SideQuestBot, guild: discord.Guild) -> RoleMenuSchema | str:
        """The live menu, or an error message if it, or the role on it, no longer exists."""
        try:
            menu = await bot.api.get_role_menu(guild.id, self.menu_id)
        except NotFoundError:
            return "This role menu no longer exists."
        except APIError:
            logger.warning("Could not fetch role menu %s", self.menu_id, exc_info=True)
            return "Couldn't reach the guild records; try again shortly."
        if self.role_id not in {option.role_id for option in menu.options}:
            return "That role is no longer offered by this menu."
        return menu

    def _resolve_grantable_role(self, guild: discord.Guild) -> discord.Role | str:
        """The role to toggle, or an error message if it's missing or unsafe to self-assign."""
        role = guild.get_role(self.role_id)
        if role is None:
            return "That role no longer exists."
        reason = grant_block_reason(role)
        if reason is not None:
            return f"I can't grant that role: {reason}"
        return role

    @staticmethod
    def _cap_message(
        menu: RoleMenuSchema, guild: discord.Guild, member: discord.Member, role: discord.Role
    ) -> str | None:
        """An error message if granting ``role`` would exceed ``menu.max_roles``, else ``None``."""
        has_role = role in member.roles
        held_count = sum(1 for option in menu.options if (r := guild.get_role(option.role_id)) and r in member.roles)
        if exceeds_max_roles(has_role=has_role, max_roles=menu.max_roles, held_count=held_count):
            return f"You can only hold {menu.max_roles} role(s) from this menu at once."
        return None

    @staticmethod
    async def _toggle_role(member: discord.Member, role: discord.Role) -> str:
        """Add or remove ``role`` on ``member``, whichever applies. Returns the reply text."""
        has_role = role in member.roles
        try:
            if has_role:
                await member.remove_roles(role, reason="Self-assign role menu")
            else:
                await member.add_roles(role, reason="Self-assign role menu")
        except discord.HTTPException:
            guild_id = member.guild.id
            logger.warning("Could not toggle role %s for %s in guild %s", role.id, member.id, guild_id, exc_info=True)
            return "Couldn't update your roles; check my permissions."
        return f"Removed {role.mention}." if has_role else f"Added {role.mention}."


async def _sync_menu_message(bot: SideQuestBot, guild: discord.Guild, menu: RoleMenuSchema) -> RoleMenuSchema:
    """Post the menu's message if it has none yet, edit it in place, or re-post if it was deleted."""
    channel = guild.get_channel(menu.channel_id)
    if not isinstance(channel, discord.TextChannel):
        logger.warning("Role menu %s channel %s is missing or not a text channel", menu.id, menu.channel_id)
        return menu
    embed = render_menu_embed(menu)
    view = build_menu_view(menu)
    if menu.message_id is not None:
        try:
            message = await channel.fetch_message(menu.message_id)
        except discord.NotFound:
            pass
        else:
            await message.edit(embed=embed, view=view)
            return menu
    posted = await channel.send(embed=embed, view=view)
    return await bot.api.update_role_menu(guild.id, menu.id, RoleMenuUpdate(message_id=posted.id))


def _require_guild(interaction: discord.Interaction[SideQuestBot]) -> discord.Guild:
    """The interaction's guild; every ``/rolemenu`` command is ``guild_only`` so this is always set."""
    guild = interaction.guild
    if guild is None:  # pragma: no cover - guild_only=True guarantees this
        msg = "This command can only be used in a guild."
        raise RuntimeError(msg)
    return guild


def _require_member(interaction: discord.Interaction[SideQuestBot]) -> discord.Member:
    """The interaction's invoking member; every ``/rolemenu`` command is ``guild_only`` so this is always set."""
    member = interaction.user
    if not isinstance(member, discord.Member):  # pragma: no cover - guild_only=True guarantees this
        msg = "This command can only be used in a guild."
        raise TypeError(msg)
    return member


async def _autocomplete_menu(
    interaction: discord.Interaction[SideQuestBot], current: str
) -> list[app_commands.Choice[str]]:
    """Menu titles matching ``current``, value is the menu's UUID."""
    if interaction.guild_id is None:
        return []
    try:
        menus: Sequence[RoleMenuSchema] = await interaction.client.api.list_role_menus(interaction.guild_id)
    except APIError:
        return []
    needle = current.lower()
    matches = [menu for menu in menus if needle in menu.title.lower()]
    return [app_commands.Choice(name=menu.title[:100], value=str(menu.id)) for menu in matches[:25]]


class RoleMenusCog(commands.Cog):
    """``/rolemenu`` command group plus the persistent button handler."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot

    rolemenu = app_commands.Group(
        name="rolemenu",
        description="Manage self-assignable role menus.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    @rolemenu.command(name="create", description="Create a new role menu.")
    @app_commands.describe(
        channel="Channel to post the menu in",
        title="Menu title",
        description="Optional description",
        max_roles="Max roles a member may hold from this menu at once (0 = unlimited)",
    )
    async def create(
        self,
        interaction: discord.Interaction[SideQuestBot],
        channel: discord.TextChannel,
        title: str,
        description: str | None = None,
        max_roles: app_commands.Range[int, 0, 25] = 0,
    ) -> None:
        """Create an empty menu; add roles to it with ``/rolemenu add``."""
        guild = _require_guild(interaction)
        await interaction.response.defer(ephemeral=True)
        menu = await self.bot.api.create_role_menu(
            guild.id,
            RoleMenuCreate(channel_id=channel.id, title=title, description=description, max_roles=max_roles),
        )
        await interaction.followup.send(
            f"Created role menu **{menu.title}** (`{menu.id}`). Use `/rolemenu add` to add roles.", ephemeral=True
        )

    @rolemenu.command(name="add", description="Add a role to a menu.")
    @app_commands.describe(menu="The role menu", role="Role to offer", label="Button label", emoji="Button emoji")
    @app_commands.autocomplete(menu=_autocomplete_menu)
    async def add(  # noqa: PLR0917 - slash command options map 1:1 to parameters
        self,
        interaction: discord.Interaction[SideQuestBot],
        menu: str,
        role: discord.Role,
        label: str,
        emoji: str | None = None,
        description: str | None = None,
    ) -> None:
        """Add or edit the button for ``role`` and (re)post the menu's message.

        Requires both: the role must be safe for the bot to hand out (:func:`grant_block_reason`), and the inviting
        member must be allowed to configure it (:func:`configure_block_reason`) - otherwise a Manage Roles member
        could offer a role above their own and self-assign their way to more permissions.
        """
        guild = _require_guild(interaction)
        member = _require_member(interaction)
        reason = grant_block_reason(role) or configure_block_reason(member, role)
        if reason is not None:
            await interaction.response.send_message(f"Can't offer that role: {reason}", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        updated = await self.bot.api.upsert_role_menu_option(
            guild.id,
            UUID(menu),
            role.id,
            RoleMenuOptionUpsert(label=label, emoji=emoji, description=description),
        )
        await _sync_menu_message(self.bot, guild, updated)
        await interaction.followup.send(f"Added {role.mention} to **{updated.title}**.", ephemeral=True)

    @rolemenu.command(name="remove", description="Remove a role from a menu.")
    @app_commands.describe(menu="The role menu", role="Role to remove")
    @app_commands.autocomplete(menu=_autocomplete_menu)
    async def remove(self, interaction: discord.Interaction[SideQuestBot], menu: str, role: discord.Role) -> None:
        """Remove the button for ``role`` and refresh the menu's message."""
        guild = _require_guild(interaction)
        await interaction.response.defer(ephemeral=True)
        updated = await self.bot.api.delete_role_menu_option(guild.id, UUID(menu), role.id)
        await _sync_menu_message(self.bot, guild, updated)
        await interaction.followup.send(f"Removed {role.mention} from **{updated.title}**.", ephemeral=True)

    @rolemenu.command(name="edit", description="Edit a menu's title, description, or role cap.")
    @app_commands.describe(menu="The role menu", title="New title", description="New description", max_roles="New cap")
    @app_commands.autocomplete(menu=_autocomplete_menu)
    async def edit(
        self,
        interaction: discord.Interaction[SideQuestBot],
        menu: str,
        title: str | None = None,
        description: str | None = None,
        max_roles: app_commands.Range[int, 0, 25] | None = None,
    ) -> None:
        """Apply only the fields given, then refresh the menu's message."""
        guild = _require_guild(interaction)
        changes: dict[str, str | int] = {}
        if title is not None:
            changes["title"] = title
        if description is not None:
            changes["description"] = description
        if max_roles is not None:
            changes["max_roles"] = max_roles
        if not changes:
            await interaction.response.send_message("Nothing to change.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        menu_id = UUID(menu)
        updated = await self.bot.api.update_role_menu(guild.id, menu_id, RoleMenuUpdate.model_validate(changes))
        await _sync_menu_message(self.bot, guild, updated)
        await interaction.followup.send(f"Updated **{updated.title}**.", ephemeral=True)

    @rolemenu.command(name="delete", description="Delete a menu and its message.")
    @app_commands.describe(menu="The role menu")
    @app_commands.autocomplete(menu=_autocomplete_menu)
    async def delete(self, interaction: discord.Interaction[SideQuestBot], menu: str) -> None:
        """Delete the posted message (if any) and the menu record."""
        guild = _require_guild(interaction)
        await interaction.response.defer(ephemeral=True)
        menu_id = UUID(menu)
        existing = await self.bot.api.get_role_menu(guild.id, menu_id)
        if existing.message_id is not None:
            channel = guild.get_channel(existing.channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(existing.message_id)
                    await message.delete()
                except discord.HTTPException:
                    logger.warning("Could not delete message for role menu %s", menu_id, exc_info=True)
        await self.bot.api.delete_role_menu(guild.id, menu_id)
        await interaction.followup.send(f"Deleted **{existing.title}**.", ephemeral=True)

    @rolemenu.command(name="list", description="List this guild's role menus.")
    async def list_menus(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """All menus in this guild, with their option count and channel."""
        guild = _require_guild(interaction)
        menus = await self.bot.api.list_role_menus(guild.id)
        if not menus:
            await interaction.response.send_message("No role menus yet.", ephemeral=True)
            return
        lines = [
            f"`{menu.id}` — **{menu.title}** ({len(menu.options)} option(s)) in <#{menu.channel_id}>" for menu in menus
        ]
        embed = make_embed("Role Menus", "\n".join(lines), color=Colors.SLATE)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rolemenu.command(name="refresh", description="Re-post a menu's message if it was deleted.")
    @app_commands.describe(menu="The role menu")
    @app_commands.autocomplete(menu=_autocomplete_menu)
    async def refresh(self, interaction: discord.Interaction[SideQuestBot], menu: str) -> None:
        """Re-sync the menu's message: edits it in place, or re-posts it if it's gone."""
        guild = _require_guild(interaction)
        await interaction.response.defer(ephemeral=True)
        current = await self.bot.api.get_role_menu(guild.id, UUID(menu))
        await _sync_menu_message(self.bot, guild, current)
        await interaction.followup.send(f"Refreshed **{current.title}**.", ephemeral=True)


async def setup(bot: SideQuestBot) -> None:
    """Register the persistent button handler and the ``/rolemenu`` command group."""
    bot.add_dynamic_items(RoleMenuButton)
    await bot.add_cog(RoleMenusCog(bot))
