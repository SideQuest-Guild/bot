"""Gate layout messages in ``#sign-the-ledger`` and ``#guest-chat``, and manual review for members who won't sign in.

Every 10 minutes (and once on ready) the bot keeps the hand-written Components V2 layout message in each channel of
the site guild (``WEB_GUILD_ID``) in sync with the code below: it finds the bot's own layout message (the one
carrying Discord's ``IS_COMPONENTS_V2`` flag whose first component is a media gallery; in ``#guest-chat`` it's
pinned), edits it in place only when its rendered text or buttons differ from what's wanted, or posts (and, for
``#guest-chat``, pins) one if none exists yet. Editing never re-uploads the banner/crest images: Discord keeps a
message's existing attachments unless told otherwise, and the new view's ``attachment://`` references point at the
same filenames, so omitting ``attachments``/``files`` from the edit call is enough.

``#sign-the-ledger``'s layout carries a persistent **I'd rather not sign in** button for members who will never sign
in on the site: it opens a modal for their character name and an optional note, then (once past a per-member
10-minute cooldown, and unless they're already confirmed) posts a request to ``#officer-chat`` with a **Confirm in
guild** button that calls :meth:`~sidequest_bot.plugins.link_roles.LinkRolesCog.confirm_discord_member`, the same
path the **Confirm in guild** context-menu command uses.
"""

import logging
import re
import time
from functools import lru_cache
from importlib.resources import files
from io import BytesIO
from typing import TYPE_CHECKING, Any, Final, Self, cast

import discord
from discord.ext import commands, tasks

from sidequest_bot.api_client import APIError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.channel_sync import find_text_channel
from sidequest_bot.lib.embeds import Colors, make_embed, truncate, user_line
from sidequest_bot.plugins.characters import CHARACTER_PAGE_URL, resolve_slug
from sidequest_bot.plugins.link_roles import LinkRolesCog
from sidequest_bot.plugins.signup_notices import clicker_is_gm
from sidequest_common.settings import WebSettings

if TYPE_CHECKING:
    from sidequest_common.schemas import GuildConfigSchema, MemberLink

logger = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 600
HISTORY_LIMIT = 50
"""How many of a channel's own recent messages are read back to find the layout message."""

LEDGER_CHANNEL_NAME = "sign-the-ledger"
GUEST_CHANNEL_NAME = "guest-chat"
OFFICER_CHANNEL_NAME = "officer-chat"
RULES_CHANNEL_NAME = "rules"

LEDGER_IMAGE = "sidequest-ledger.webp"
GUEST_IMAGE = "sidequest-guest-hall.webp"
CREST_IMAGE = "sidequest-crest.png"
_ASSETS = files("sidequest_bot") / "assets" / "gate"

SIGNIN_URL = "https://sidequest-guild.com/auth/discord/login?next=/account/"
ACCOUNT_URL = "https://sidequest-guild.com/account/"
RULES_URL = "https://sidequest-guild.com/rules/"
PRIVACY_URL = "https://sidequest-guild.com/privacy/"
TERMS_URL = "https://sidequest-guild.com/terms/"
EVENTS_URL = "https://sidequest-guild.com/events/"

REVIEW_CUSTOM_ID: Final = "sq:gate:review"
REVIEW_CUSTOM_ID_RE: Final = re.compile(r"^sq:gate:review$")
CONFIRM_CUSTOM_ID_RE: Final = re.compile(r"^sq:gate:confirm:(?P<discord_id>\d+)$")

REVIEW_COOLDOWN_SECONDS = 600

STATUS_FIELD_NAME = "Status"
PENDING_STATUS_TEXT = "Awaiting a Quest Master to confirm in guild."
LEFT_SERVER_STATUS_TEXT = "They left the server before being confirmed."


@lru_cache(maxsize=1)
def _web_settings() -> WebSettings:
    """``WebSettings`` (``WEB_*`` env vars), shared with the API: ``GUILD_ID`` and ``GM_ROLE_IDS``."""
    return WebSettings()


def build_confirm_custom_id(discord_id: int) -> str:
    """The ``custom_id`` for one member's **Confirm in guild** button."""
    return f"sq:gate:confirm:{discord_id}"


def _gate_file(filename: str, *, description: str) -> discord.File:
    """A fresh upload of ``filename`` from ``assets/gate/``, used only the first time a layout message is posted."""
    return discord.File(BytesIO((_ASSETS / filename).read_bytes()), filename=filename, description=description)


def _role_mention(guild: discord.Guild, role_id: int | None, fallback: str) -> str:
    """``role_id``'s mention if it's configured and still exists, else ``**fallback**``."""
    if role_id is not None and (role := guild.get_role(role_id)) is not None:
        return role.mention
    return f"**{fallback}**"


def _role_mention_by_name(guild: discord.Guild, name: str) -> str:
    """A role mention looked up by its exact name, since these aren't tracked in the guild config."""
    role = discord.utils.get(guild.roles, name=name)
    return role.mention if role is not None else f"**{name}**"


def _rules_mention(guild: discord.Guild) -> str:
    channel = find_text_channel(guild, RULES_CHANNEL_NAME)
    return channel.mention if channel is not None else f"#{RULES_CHANNEL_NAME}"


def _in_layout(item: discord.ui.DynamicItem[Any]) -> discord.ui.Item[discord.ui.LayoutView]:
    """``item`` typed for a layout's ``Section`` or ``ActionRow``.

    discord.py declares every ``DynamicItem`` as ``Item[View | LayoutView]``, which the layout-only containers
    reject even though dispatch works the same in both kinds of view.
    """
    return cast("discord.ui.Item[discord.ui.LayoutView]", item)


# --- Layout rendering -----------------------------------------------------------------------------------------


def build_ledger_view(guild: discord.Guild, config: GuildConfigSchema) -> discord.ui.LayoutView:
    """The ``#sign-the-ledger`` layout: banner, welcome, how to get in, manual review, 18+ footer and links."""
    battlenet_mention = _role_mention(guild, config.battlenet_role_id, "Battle-Tagged")
    container = discord.ui.Container(accent_colour=Colors.SITE_GOLD)
    container.add_item(
        discord.ui.Section(
            "## Welcome, traveller\nThe guild hall opens to everyone who has signed the ledger: a one-time "
            "sign-in on **sidequest-guild.com** with your Discord account. It takes about ten seconds.",
            accessory=discord.ui.Thumbnail(f"attachment://{CREST_IMAGE}", description="The SideQuest crest"),
        )
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    container.add_item(
        discord.ui.TextDisplay(
            "### How to get in\n"
            "**1.** Press **Sign the Ledger** below and approve the Discord sign-in.\n"
            "**2.** Within a minute the **Guest Hall** opens, and this channel disappears.\n"
            "**3.** Once you're in the in-game guild, link Battle.net on your account page and you're confirmed "
            "automatically within about 30 minutes, or an officer confirms you.\n"
            f"-# Optional: link Battle.net on your account page to earn {battlenet_mention}."
        )
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
    container.add_item(
        discord.ui.TextDisplay(
            "### What signing in shares\nYour Discord name, avatar and server roles. No messages, no email, no "
            "password. You can erase it all from your [account page](https://sidequest-guild.com/account/) at "
            "any time."
        )
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
    container.add_item(
        discord.ui.TextDisplay(
            "### Rather not sign in?\nPress **I'd rather not sign in**, tell us your character, and an officer "
            "will let you in by hand."
        )
    )
    container.add_item(discord.ui.ActionRow(_in_layout(GateReviewButton())))
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    container.add_item(
        discord.ui.TextDisplay(
            "-# SideQuest is for players 18 and older. Signing in means you accept the guild rules and Terms. "
            f"Read the rules in {_rules_mention(guild)} while you wait."
        )
    )
    container.add_item(
        discord.ui.ActionRow(
            discord.ui.Button(
                style=discord.ButtonStyle.link, label="Sign the Ledger", emoji="\N{FEATHER}", url=SIGNIN_URL
            ),
            discord.ui.Button(style=discord.ButtonStyle.link, label="Guild rules", url=RULES_URL),
            discord.ui.Button(style=discord.ButtonStyle.link, label="Privacy", url=PRIVACY_URL),
            discord.ui.Button(style=discord.ButtonStyle.link, label="Terms", url=TERMS_URL),
        )
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(
                f"attachment://{LEDGER_IMAGE}",
                description="Sign the Ledger: an open guild ledger on a desk by candlelight, beside an inkwell "
                "and sealed scrolls.",
            )
        )
    )
    view.add_item(container)
    return view


def build_guest_view(guild: discord.Guild, config: GuildConfigSchema) -> discord.ui.LayoutView:
    """The ``#guest-chat`` layout: banner, welcome, joining the guild, what to do meanwhile, and links."""
    roster_mention = _role_mention(guild, config.roster_role_id, "New Arrival")
    quest_master_mention = _role_mention_by_name(guild, "Quest Master")
    quest_guide_mention = _role_mention_by_name(guild, "Quest Guide")
    container = discord.ui.Container(accent_colour=Colors.SITE_GOLD)
    container.add_item(
        discord.ui.Section(
            "## You've signed the ledger\nWelcome in. The Guest Hall is for travellers who have signed in but "
            "haven't been matched to the in-game guild yet. Pull up a chair by the fire.",
            accessory=discord.ui.Thumbnail(f"attachment://{CREST_IMAGE}", description="The SideQuest crest"),
        )
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    container.add_item(
        discord.ui.TextDisplay(
            "### Joining the guild\n"
            f"**1.** In World of Warcraft: Forever, ask a {quest_master_mention} or {quest_guide_mention} for a "
            "guild invite.\n"
            "**2.** Once you're on the roster, linking Battle.net confirms you automatically within about 30 "
            f"minutes — or an officer confirms you. You get {roster_mention} and the rest of the guild opens "
            "within a minute.\n"
            "**3.** Link Battle.net on your account page."
        )
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
    container.add_item(
        discord.ui.TextDisplay(
            "### Until then\nChat here, hop into **Lobby** voice, and browse upcoming guild events on the site."
        )
    )
    container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
    container.add_item(
        discord.ui.ActionRow(
            discord.ui.Button(style=discord.ButtonStyle.link, label="Your account", url=ACCOUNT_URL),
            discord.ui.Button(style=discord.ButtonStyle.link, label="Events", url=EVENTS_URL),
            discord.ui.Button(style=discord.ButtonStyle.link, label="Guild rules", url=RULES_URL),
        )
    )
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(
        discord.ui.MediaGallery(
            discord.MediaGalleryItem(
                f"attachment://{GUEST_IMAGE}",
                description="The Guest Hall: travellers rest by a campfire under a lion banner while gryphons "
                "fly toward a glowing castle.",
            )
        )
    )
    view.add_item(container)
    return view


# --- Change detection: text/buttons only, never media URLs ---------------------------------------------------

_MEDIA_COMPONENT_TYPES = frozenset(
    {discord.ComponentType.media_gallery.value, discord.ComponentType.thumbnail.value, discord.ComponentType.file.value}
)


def _reduce(data: Any) -> Any:
    """``data`` (one component's ``to_dict()``) with its id and any media URLs dropped, recursively.

    Media (the banner, the crest) always carries a fresh, signed CDN URL on every fetch even when nothing
    changed, so comparing it would make the sync think it needs to edit every single tick; its mere presence is
    all that matters here, not where it points.
    """
    if data.get("type") in _MEDIA_COMPONENT_TYPES:
        return data["type"]
    reduced = {key: value for key, value in data.items() if key != "id"}
    if "accessory" in reduced:
        reduced["accessory"] = _reduce(reduced["accessory"])
    if "components" in reduced:
        reduced["components"] = [_reduce(child) for child in reduced["components"]]
    return reduced


def _view_signature(view: discord.ui.LayoutView) -> list[Any]:
    """The reduced signature of a view we built, for comparison against a fetched message's."""
    return [_reduce(data) for data in view.to_components()]


def _message_signature(message: discord.Message) -> list[Any]:
    """The reduced signature of a fetched message's current components."""
    return [_reduce(component.to_dict()) for component in message.components]


# --- Review request embed ---------------------------------------------------------------------------------------


def _embed_field(embed: discord.Embed, name: str) -> str | None:
    for field in embed.fields:
        if field.name == name:
            return cast("str | None", field.value)
    return None


def build_review_embed(member: discord.Member, character_name: str, note: str | None) -> discord.Embed:
    """The officer-chat embed for one manual review request: who, when, their character, and any note."""
    embed = make_embed(
        "Manual review request", f"{user_line(member)} chose not to sign in on the site.", color=Colors.GOLD
    )
    embed.add_field(name="Discord username", value=f"@{member.name}", inline=True)
    embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, style="R"), inline=True)
    if member.joined_at is not None:
        embed.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, style="R"), inline=True)
    embed.add_field(name="Character", value=truncate(character_name, 256), inline=False)
    if note:
        embed.add_field(name="Note", value=truncate(note), inline=False)
    embed.add_field(name=STATUS_FIELD_NAME, value=PENDING_STATUS_TEXT, inline=False)
    return embed


def apply_review_status(embed: discord.Embed, status_text: str) -> discord.Embed:
    """Replace ``embed``'s Status field with ``status_text`` in place (appends one if somehow missing)."""
    for index, field in enumerate(embed.fields):
        if field.name == STATUS_FIELD_NAME:
            embed.set_field_at(index, name=STATUS_FIELD_NAME, value=status_text, inline=False)
            return embed
    embed.add_field(name=STATUS_FIELD_NAME, value=status_text, inline=False)
    return embed


def build_review_request_view(discord_id: int, character_name: str, *, disabled: bool = False) -> discord.ui.View:
    """**Confirm in guild** (disabled once resolved) plus a link to search the character on the site, if named well."""
    view = discord.ui.View(timeout=None)
    view.add_item(GateConfirmButton(discord_id, disabled=disabled))
    slug = resolve_slug(character_name)
    if slug is not None:
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.link, label="Search character", url=CHARACTER_PAGE_URL.format(slug=slug)
            )
        )
    return view


# --- Persistent components --------------------------------------------------------------------------------------


class GateReviewModal(discord.ui.Modal, title="I'd rather not sign in"):
    """Character name (required) and an optional note, posted to officer-chat on submit."""

    def __init__(self, cog: GateMessagesCog) -> None:
        super().__init__()
        self.cog = cog
        self.character_input = discord.ui.TextInput(max_length=32, placeholder="Nox Zul")
        self.note_input = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=300, required=False)
        self.add_item(discord.ui.Label(text="Character name", component=self.character_input))
        self.add_item(discord.ui.Label(text="Note for officers (optional)", component=self.note_input))

    async def on_submit(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Defer, then hand off to the cog, which does the cooldown/already-confirmed checks and posts the request."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.handle_review_submit(interaction, self.character_input.value, self.note_input.value or None)


class GateReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=REVIEW_CUSTOM_ID_RE):
    """Persistent **I'd rather not sign in** button; carries no state, so it survives restarts untouched."""

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary, label="I'd rather not sign in", custom_id=REVIEW_CUSTOM_ID
            )
        )

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[Any], _item: discord.ui.Item[Any], _match: re.Match[str], /
    ) -> Self:
        """Rebuild after a restart; there's nothing to recover, the button is stateless."""
        return cls()

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Open the review modal for the clicker."""
        cog = interaction.client.get_cog("GateMessagesCog")
        if not isinstance(cog, GateMessagesCog):
            return
        await interaction.response.send_modal(GateReviewModal(cog))


class GateConfirmButton(discord.ui.DynamicItem[discord.ui.Button], template=CONFIRM_CUSTOM_ID_RE):
    """Persistent **Confirm in guild** button on one member's review request; re-checks the clicker is a GM."""

    def __init__(self, discord_id: int, *, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.success,
                label="Confirm in guild",
                custom_id=build_confirm_custom_id(discord_id),
                disabled=disabled,
            )
        )
        self.discord_id = discord_id

    @classmethod
    async def from_custom_id(
        cls, _interaction: discord.Interaction[Any], item: discord.ui.Item[Any], match: re.Match[str], /
    ) -> Self:
        """Rebuild from a dispatched component after a restart."""
        return cls(int(match["discord_id"]), disabled=getattr(item, "disabled", False))

    async def callback(self, interaction: discord.Interaction[SideQuestBot]) -> None:
        """Re-check the clicker is a GM, confirm the target on the roster, then edit the request message in place."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        clicker = interaction.user
        if guild is None or not isinstance(clicker, discord.Member):
            return
        if not clicker_is_gm({role.id for role in clicker.roles}, _web_settings().GM_ROLE_IDS):
            await interaction.followup.send("Only Quest Masters can confirm members.", ephemeral=True)
            return
        message = interaction.message
        status = _embed_field(message.embeds[0], STATUS_FIELD_NAME) if message is not None and message.embeds else None
        if status is not None and status.startswith("Confirmed by"):
            await interaction.followup.send("Already confirmed.", ephemeral=True)
            return
        target = guild.get_member(self.discord_id)
        if target is None:
            await self._mark_resolved(message, LEFT_SERVER_STATUS_TEXT)
            await interaction.followup.send("That member left the server.", ephemeral=True)
            return
        cog = interaction.client.get_cog("LinkRolesCog")
        if not isinstance(cog, LinkRolesCog):
            await interaction.followup.send("Confirmation isn't available right now.", ephemeral=True)
            return
        link = await cog.confirm_discord_member(interaction, target, clicker)
        if link is None:
            return  # confirm_discord_member already replied with the reason
        await self._mark_resolved(message, f"Confirmed by {clicker.mention}", disabled=True)

    async def _mark_resolved(
        self, message: discord.Message | None, status_text: str, *, disabled: bool = False
    ) -> None:
        if message is None or not message.embeds:
            return
        updated_embed = apply_review_status(message.embeds[0], status_text)
        character_name = _embed_field(updated_embed, "Character") or ""
        view = build_review_request_view(self.discord_id, character_name, disabled=disabled)
        try:
            await message.edit(embed=updated_embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.warning("Failed to edit review request message for %s", self.discord_id, exc_info=True)


# --- Cog ---------------------------------------------------------------------------------------------------------


class GateMessagesCog(commands.Cog):
    """Keeps the gate layout messages in sync, and handles the manual-review request flow."""

    def __init__(self, bot: SideQuestBot) -> None:
        self.bot = bot
        self._review_cooldowns: dict[int, float] = {}
        self._sync_loop.start()

    async def cog_unload(self) -> None:
        """Stop the sync loop when the cog is unloaded."""
        self._sync_loop.cancel()

    # --- Layout sync ------------------------------------------------------------------------------------------

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def _sync_loop(self) -> None:
        guild = self.bot.get_guild(_web_settings().GUILD_ID)
        if guild is None:
            return
        try:
            config = await self.bot.configs.get(guild.id)
        except APIError:
            # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
            logger.warning("Failed to fetch guild config for gate messages in guild %s", guild.id, exc_info=True)
            return
        await self._sync_channel(
            guild,
            LEDGER_CHANNEL_NAME,
            build_ledger_view(guild, config),
            files=[
                _gate_file(
                    LEDGER_IMAGE,
                    description="Sign the Ledger: an open guild ledger on a desk by candlelight, beside an "
                    "inkwell and sealed scrolls.",
                ),
                _gate_file(CREST_IMAGE, description="The SideQuest crest"),
            ],
            pin=False,
        )
        await self._sync_channel(
            guild,
            GUEST_CHANNEL_NAME,
            build_guest_view(guild, config),
            files=[
                _gate_file(
                    GUEST_IMAGE,
                    description="The Guest Hall: travellers rest by a campfire under a lion banner while "
                    "gryphons fly toward a glowing castle.",
                ),
                _gate_file(CREST_IMAGE, description="The SideQuest crest"),
            ],
            pin=True,
        )

    @_sync_loop.before_loop
    async def _before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _find_layout_message(self, channel: discord.TextChannel, bot_id: int) -> discord.Message | None:
        """The bot's own layout message: ``IS_COMPONENTS_V2`` set and a media gallery as its first component."""
        async for message in channel.history(limit=HISTORY_LIMIT):
            if message.author.id != bot_id:
                continue
            if not message.flags.components_v2:
                continue
            if message.components and message.components[0].type == discord.ComponentType.media_gallery:
                return message
        return None

    async def _sync_channel(
        self,
        guild: discord.Guild,
        channel_name: str,
        view: discord.ui.LayoutView,
        *,
        files: list[discord.File],
        pin: bool,
    ) -> None:
        channel = find_text_channel(guild, channel_name)
        if channel is None:
            logger.warning("No #%s channel in guild %s; skipping gate message sync", channel_name, guild.id)
            return
        me = self.bot.user
        if me is None:
            return
        existing = await self._find_layout_message(channel, me.id)
        if existing is not None:
            if _message_signature(existing) == _view_signature(view):
                return
            try:
                # Re-upload the images with the edit: Components V2 messages report no attachments over the API, so
                # attachment:// references can't be trusted to resolve against the old upload. Edits only happen
                # when the text changes, so this is rare.
                await existing.edit(view=view, attachments=files, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logger.warning("Failed to edit gate message in #%s in guild %s", channel_name, guild.id, exc_info=True)
            return
        try:
            message = await channel.send(view=view, files=files, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.warning("Failed to post gate message in #%s in guild %s", channel_name, guild.id, exc_info=True)
            return
        if pin:
            try:
                await message.pin(reason="Gate layout message")
            except discord.HTTPException:
                logger.warning("Failed to pin gate message in #%s in guild %s", channel_name, guild.id, exc_info=True)

    # --- Manual review request ---------------------------------------------------------------------------------

    async def handle_review_submit(
        self, interaction: discord.Interaction[SideQuestBot], character_name: str, note: str | None
    ) -> None:
        """Check the clicker isn't already in, isn't on cooldown, then post the request to officer-chat."""
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return
        config = await self.bot.configs.get(guild.id)
        if config.roster_role_id is not None and any(role.id == config.roster_role_id for role in member.roles):
            await interaction.followup.send("You're already confirmed in the guild.", ephemeral=True)
            return
        link = await self._member_link(guild.id, member.id)
        if link is not None and (link.roster_confirmed or link.signed_in):
            await interaction.followup.send("You're already signed in or confirmed.", ephemeral=True)
            return
        now = time.monotonic()
        last = self._review_cooldowns.get(member.id)
        if last is not None and now - last < REVIEW_COOLDOWN_SECONDS:
            await interaction.followup.send(
                "You already asked recently; an officer will be along shortly.", ephemeral=True
            )
            return
        channel = find_text_channel(guild, OFFICER_CHANNEL_NAME)
        if channel is None:
            logger.warning("No #%s channel in guild %s; can't post a review request", OFFICER_CHANNEL_NAME, guild.id)
            await interaction.followup.send("Couldn't reach the officers' channel; try again later.", ephemeral=True)
            return
        embed = build_review_embed(member, character_name, note)
        view = build_review_request_view(member.id, character_name)
        try:
            await channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.warning("Failed to post review request for %s in guild %s", member.id, guild.id, exc_info=True)
            await interaction.followup.send("Couldn't reach the officers' channel; try again shortly.", ephemeral=True)
            return
        self._review_cooldowns[member.id] = now
        await interaction.followup.send(
            "Sent to the officers; you'll get the guild channels as soon as one confirms you.", ephemeral=True
        )

    async def _member_link(self, guild_id: int, discord_id: int) -> MemberLink | None:
        try:
            links = await self.bot.api.list_member_links(guild_id)
        except APIError:
            logger.warning("Failed to check member link state for %s in guild %s", discord_id, guild_id, exc_info=True)
            return None
        return next((link for link in links if link.discord_id == discord_id), None)


async def setup(bot: SideQuestBot) -> None:
    """Register the persistent button/modal handlers and the cog."""
    bot.add_dynamic_items(GateReviewButton, GateConfirmButton)
    await bot.add_cog(GateMessagesCog(bot))
