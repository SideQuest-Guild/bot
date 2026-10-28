"""Discord rejects the whole command sync (and the bot exits on startup) if any slash command breaks its limits."""

import importlib
import inspect
import pkgutil

import pytest
from discord import app_commands
from discord.ext import commands

import sidequest_bot.plugins

DESCRIPTION_LIMIT = 100
NAME_LIMIT = 32


def _plugin_app_commands() -> list[app_commands.Command | app_commands.Group]:
    found: list[app_commands.Command | app_commands.Group] = []
    for module_info in pkgutil.iter_modules(sidequest_bot.plugins.__path__):
        module = importlib.import_module(f"sidequest_bot.plugins.{module_info.name}")
        for _, cog in inspect.getmembers(module, inspect.isclass):
            if issubclass(cog, commands.Cog) and cog.__module__ == module.__name__:
                found.extend(cog.__cog_app_commands__)
    return found


def _walk(command: app_commands.Command | app_commands.Group) -> list[app_commands.Command | app_commands.Group]:
    if isinstance(command, app_commands.Group):
        return [command, *command.walk_commands()]
    return [command]


ALL_COMMANDS = [walked for top in _plugin_app_commands() for walked in _walk(top)]


@pytest.mark.parametrize("command", ALL_COMMANDS, ids=lambda command: command.qualified_name)
def test_command_fits_discord_limits(command: app_commands.Command | app_commands.Group) -> None:
    assert len(command.name) <= NAME_LIMIT
    assert 1 <= len(command.description) <= DESCRIPTION_LIMIT, command.description
    if isinstance(command, app_commands.Command):
        for parameter in command.parameters:
            assert len(parameter.name) <= NAME_LIMIT
            assert 1 <= len(parameter.description) <= DESCRIPTION_LIMIT, parameter.description
