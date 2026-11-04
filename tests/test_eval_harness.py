"""The eval harness must see what a plan really did; a harness that can't catch a bad run makes every eval pass."""

import json
from typing import Any

import discord
from evals.harness import check_invariants, lets_in, run_request

from sidequest_bot.lib.llm import Reply, ToolCall
from tests.fakes import can_see, named, sidequest_server


def _turn(text: str = "", calls: list[tuple[str, dict[str, Any]]] = ()) -> Reply:  # type: ignore[assignment]
    tool_calls = [ToolCall(f"c{n}", name, args) for n, (name, args) in enumerate(calls)]
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in tool_calls
        ]
    return Reply(message, text, tool_calls)


class ScriptedModel:
    def __init__(self, *turns: Reply) -> None:
        self.turns = list(turns)

    async def complete(self, _messages: list[dict[str, Any]], _tools: Any = None) -> Reply:
        return self.turns.pop(0)


def test_fake_server_matches_who_can_see_what() -> None:
    guild = sidequest_server()
    everyone = [guild.default_role]
    member = [*everyone, named(guild.roles, "New Arrival")]
    officer = [*everyone, named(guild.roles, "Quest Guide")]
    assert can_see(named(guild.channels, "rules"), everyone)
    assert not can_see(named(guild.channels, "general"), everyone)
    assert can_see(named(guild.channels, "general"), member)
    assert not can_see(named(guild.channels, "officer-chat"), member)
    assert can_see(named(guild.channels, "officer-chat"), officer)


async def test_a_good_plan_runs_and_shows_up_on_the_server() -> None:
    model = ScriptedModel(
        _turn(
            calls=[
                ("create_role", {"name": "Friends"}),
                (
                    "create_channel",
                    {"name": "General Voice", "kind": "voice", "category": "SideQuest", "allow_roles": ["Friends"]},
                ),
            ]
        ),
        _turn("Planned it."),
    )

    out = await run_request(model, sidequest_server(), "can you do this?", reply_to=("zach", "we need voice"))  # type: ignore[arg-type]

    voice = out.channel("General Voice")
    assert lets_in(out, voice, "Friends")
    assert lets_in(out, voice, "New Arrival")
    assert not lets_in(out, voice)
    assert out.violations == []
    assert "They were replying to Zach" in out.prompt


async def test_invariants_catch_what_no_plan_should_do() -> None:
    model = ScriptedModel(_turn("Nothing to do."))
    out = await run_request(model, sidequest_server(), "hi")  # type: ignore[arg-type]
    officer_chat = out.channel("officer-chat")
    officer_chat.overwrites[out.guild.default_role] = discord.PermissionOverwrite(view_channel=True)
    await named(out.guild.channels, "rules").delete()
    role = await out.guild.create_role(name="Sneaky", permissions=discord.Permissions(ban_members=True))
    await out.member("alex").add_roles(role)
    await out.channel("general").send("@everyone hi", allowed_mentions=discord.AllowedMentions.all())

    problems = check_invariants(out, {officer_chat.id: False})

    assert "#officer-chat was hidden from @everyone and now isn't" in problems
    assert "called delete on rules" in problems
    assert any("created role @Sneaky" in p for p in problems)
    assert any("gave @Sneaky" in p for p in problems)
    assert "a post in #general could ping people" in problems
