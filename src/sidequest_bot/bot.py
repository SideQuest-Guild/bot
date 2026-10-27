"""Bot class: wiring for the API client, config cache, plugins, and slash-command errors."""

import logging
import pkgutil
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from sidequest_bot import plugins
from sidequest_bot.api_client import APIError, SideQuestAPI
from sidequest_bot.config_cache import GuildConfigCache

if TYPE_CHECKING:
    from sidequest_common.settings import APISettings, DiscordSettings

logger = logging.getLogger(__name__)


def build_intents() -> discord.Intents:
    """Default intents plus the privileged ones the plugins need.

    ``members``: welcome, auto-role, member logs. ``message_content``: automod filters and logging message text.
    Both must also be enabled for the application in the Discord Developer Portal.
    """
    intents = discord.Intents.default()
    intents.members = True
    intents.message_content = True
    return intents


class SideQuestBot(commands.Bot):
    """Slash-command bot. ``api`` and ``configs`` are shared by every plugin."""

    def __init__(self, *, api: SideQuestAPI, dev_guild_id: int | None = None) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,  # slash-only; prefix commands are unused
            intents=build_intents(),
            activity=discord.Game(name="the long quest"),
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
            tree_cls=SideQuestTree,
        )
        self.api = api
        self.configs = GuildConfigCache(api)
        self.dev_guild_id = dev_guild_id

    async def setup_hook(self) -> None:
        """Load every plugin module, then sync slash commands."""
        for module in pkgutil.iter_modules(plugins.__path__, prefix=f"{plugins.__name__}."):
            await self.load_extension(module.name)
            logger.info("Loaded plugin %s", module.name)
        if self.dev_guild_id is not None:
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        logger.info("Synced %d slash commands", len(synced))

    async def close(self) -> None:
        """Close the API client along with the gateway connection."""
        await super().close()
        await self.api.aclose()

    async def on_ready(self) -> None:
        """Log the identity we connected as."""
        logger.info("Connected as %s (%s) to %d guild(s)", self.user, getattr(self.user, "id", "?"), len(self.guilds))

    async def send_log(self, guild: discord.Guild, embed: discord.Embed) -> discord.Message | None:
        """Post ``embed`` to the guild's configured log channel. No-op if unset, missing, or not writable."""
        config = await self.configs.get(guild.id)
        if config.log_channel_id is None:
            return None
        channel = guild.get_channel(config.log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            return await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.warning("Could not post to log channel %s in guild %s", channel.id, guild.id, exc_info=True)
            return None


class SideQuestTree(app_commands.CommandTree[SideQuestBot]):
    """Command tree that tells users what went wrong (ephemerally) instead of failing silently."""

    async def on_error(  # ty: ignore[invalid-method-override]  # discord.py also binds on_error as an attribute
        self, interaction: discord.Interaction[SideQuestBot], error: app_commands.AppCommandError, /
    ) -> None:
        """Map known failures to a friendly message; log everything unexpected."""
        original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        command_name = interaction.command.name if interaction.command else "?"
        # CommandOnCooldown and MissingPermissions subclass CheckFailure, so they go first.
        if isinstance(original, app_commands.CommandOnCooldown):
            message = f"Slow down! Try again in {original.retry_after:.0f}s."
        elif isinstance(original, app_commands.MissingPermissions | app_commands.BotMissingPermissions):
            message = str(original)
        elif isinstance(original, app_commands.CheckFailure):
            message = "You can't use this command here."
        elif isinstance(original, discord.Forbidden):
            message = "I don't have permission to do that. Check my role's position and permissions."
        elif isinstance(original, APIError):
            logger.warning("API error in /%s: %s", command_name, original)
            message = f"The guild records rejected that: {original.detail}"
        else:
            logger.error("Unhandled error in /%s", command_name, exc_info=error)
            message = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def create_bot(discord_settings: DiscordSettings, api_settings: APISettings) -> SideQuestBot:
    """Bot wired to the API described by ``api_settings``."""
    api = SideQuestAPI(api_settings.URL, api_settings.KEY.get_secret_value())
    return SideQuestBot(api=api, dev_guild_id=discord_settings.GUILD_ID)
