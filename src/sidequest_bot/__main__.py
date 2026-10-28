"""``sidequest-bot``: connect to Discord."""

import asyncio
import logging


def main() -> None:
    """Run the bot until interrupted."""
    import discord

    from sidequest_bot.bot import create_bot
    from sidequest_common.settings import APISettings, DiscordSettings

    discord.utils.setup_logging(level=logging.INFO)
    # httpx logs every request at INFO; the raid-sync poll alone would add four lines a minute. Failures still surface
    # through APIError handling and the bot's own warnings.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    discord_settings = DiscordSettings()
    api_settings = APISettings()
    bot = create_bot(discord_settings, api_settings)

    async def runner() -> None:
        async with bot:
            await bot.start(discord_settings.TOKEN.get_secret_value())

    asyncio.run(runner())


if __name__ == "__main__":
    main()
