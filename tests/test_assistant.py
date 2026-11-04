"""The AI helper: Quest Masters ask, the model proposes additive changes, a Quest Master approves, the bot does them."""

import json
from itertools import count
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
import respx

from sidequest_bot.lib.llm import ChatClient, LLMError, Reply, ToolCall, Usage
from sidequest_bot.plugins.assistant import (
    BUSY,
    LLM_DOWN,
    MAX_STEPS,
    AssistantCog,
    Plan,
    PlanView,
    Toolbox,
    execute,
    plan_request,
)
from tests.fakes import make_channel, make_guild, make_member, make_role, named

BOT_ID = 4242
GM_ROLE = 10
NEW_ARRIVAL = 11
OFFICER_ONLY = 12
EVERYONE = 1
GUILD_HALL = 100
GENERAL = 101
ZACH = 500
COFFEE = 501

_ids = count(9000)


@pytest.fixture
def guild() -> MagicMock:
    """Members-only Guild Hall category with #general in it; two Quest Masters and an officer-powers role."""
    everyone = make_role(EVERYONE, "@everyone")
    gm = make_role(GM_ROLE, "Quest Master")
    arrival = make_role(NEW_ARRIVAL, "New Arrival")
    officer = make_role(OFFICER_ONLY, "Officer", permissions=discord.Permissions(ban_members=True))
    hall_access = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        arrival: discord.PermissionOverwrite(view_channel=True),
    }
    hall = make_channel(discord.CategoryChannel, GUILD_HALL, "Guild Hall", hall_access)
    general = make_channel(discord.TextChannel, GENERAL, "general", category=hall)
    members = [
        make_member(COFFEE, "coffee", [everyone, gm], manage_channels=True, manage_roles=True),
        make_member(ZACH, "zach", [everyone, gm]),
    ]
    return make_guild([everyone, arrival, officer, gm], [hall, general], members)


def _call(tool: str, /, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"call-{next(_ids)}", name=tool, arguments=arguments)


def _model_turn(text: str = "", calls: list[ToolCall] | None = None) -> Reply:
    calls = calls or []
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in calls
        ]
    return Reply(message=message, text=text, tool_calls=calls)


class FakeLLM:
    """Plays back scripted model turns and keeps what it was sent."""

    def __init__(self, *turns: Reply) -> None:
        self.turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []

    async def complete(self, messages: list[dict[str, Any]], _tools: list[dict[str, Any]]) -> Reply:
        self.sent.append(list(messages))
        return self.turns.pop(0)

    async def aclose(self) -> None:
        pass


def _member_by_id(guild: MagicMock, member_id: int) -> MagicMock:
    return guild.get_member(member_id)


ZACHS_ASK = [
    _call("create_role", name="Friends"),
    _call("create_channel", name="General Voice", kind="voice", category="Guild Hall", allow_roles=["Friends"]),
]


class TestToolbox:
    def test_names_mentions_ids_and_planned_roles_all_resolve(self, guild: MagicMock) -> None:
        box = Toolbox(guild, Plan())
        box.call(_call("create_role", name="Friends"))

        assert box.role_ref("New Arrival") == NEW_ARRIVAL
        assert box.role_ref(f"<@&{NEW_ARRIVAL}>") == NEW_ARRIVAL
        assert box.role_ref(str(NEW_ARRIVAL)) == NEW_ARRIVAL
        assert box.role_ref("@friends").name == "Friends"  # type: ignore[union-attr]

    def test_unknown_names_come_back_to_the_model_as_errors(self, guild: MagicMock) -> None:
        plan = Plan()
        result = Toolbox(guild, plan).call(_call("allow_role_in_channel", channel="general", role="Spectators"))

        assert result.startswith("error: there is no role called Spectators")
        assert plan.steps == []

    def test_refuses_to_plan_handing_out_a_role_with_moderator_powers(self, guild: MagicMock) -> None:
        plan = Plan()
        result = Toolbox(guild, plan).call(_call("give_role", member=str(ZACH), role="Officer"))

        assert "moderator/admin permissions" in result
        assert plan.steps == []

    @pytest.mark.parametrize(
        "call",
        [
            _call("allow_role_in_channel", channel="general", role="@everyone"),
            _call("create_channel", name="lobby", kind="voice", allow_roles=["everyone"]),
        ],
        ids=["existing channel", "new channel"],
    )
    def test_never_opens_a_channel_to_everyone(self, guild: MagicMock, call: ToolCall) -> None:
        plan = Plan()
        assert Toolbox(guild, plan).call(call).startswith("error:")
        assert plan.steps == []

    def test_category_must_be_a_category(self, guild: MagicMock) -> None:
        result = Toolbox(guild, Plan()).call(_call("create_channel", name="vc", kind="voice", category="general"))
        assert result == "error: general isn't a category"

    def test_refuses_a_second_role_with_the_same_name(self, guild: MagicMock) -> None:
        result = Toolbox(guild, Plan()).call(_call("create_role", name="new arrival"))
        assert result == "error: a role called new arrival already exists"

    def test_plans_are_capped(self, guild: MagicMock) -> None:
        plan = Plan()
        box = Toolbox(guild, plan)
        for number in range(MAX_STEPS):
            box.call(_call("post_message", channel="general", text=f"hi {number}"))

        assert box.call(_call("post_message", channel="general", text="one more")).startswith("error:")
        assert len(plan.steps) == MAX_STEPS

    def test_garbled_arguments_are_reported(self, guild: MagicMock) -> None:
        assert (
            Toolbox(guild, Plan()).call(ToolCall("x", "create_role", None)) == "error: the arguments weren't valid JSON"
        )

    def test_list_channels_shows_who_can_see_what(self, guild: MagicMock) -> None:
        listing = Toolbox(guild, Plan()).call(_call("list_channels"))
        assert f"category Guild Hall (id {GUILD_HALL}): hidden from @everyone; visible to @New Arrival" in listing
        assert f"text general (id {GENERAL}): same access as its category" in listing


class TestExecute:
    async def test_zachs_ask_new_voice_channel_keeps_members_only_access_and_lets_friends_in(
        self, guild: MagicMock
    ) -> None:
        plan = Plan()
        box = Toolbox(guild, plan)
        for call in ZACHS_ASK:
            box.call(call)

        results = await execute(plan, guild, "test")

        assert [ok for ok, _ in results] == [True, True]
        assert guild.create_role.await_args.kwargs["permissions"] == discord.Permissions.none()
        friends = named(guild.roles, "Friends")
        kwargs = guild.create_voice_channel.await_args.kwargs
        assert kwargs["category"].id == GUILD_HALL
        overwrites = {target.name: overwrite for target, overwrite in kwargs["overwrites"].items()}
        assert overwrites["@everyone"].view_channel is False
        assert overwrites["New Arrival"].view_channel is True
        assert overwrites[friends.name].view_channel is True
        assert overwrites[friends.name].connect is True
        assert overwrites[friends.name].speak is True

    async def test_a_failed_step_stops_the_rest(self, guild: MagicMock) -> None:
        plan = Plan()
        box = Toolbox(guild, plan)
        for call in ZACHS_ASK:
            box.call(call)
        guild.create_role.side_effect = discord.Forbidden(MagicMock(status=403), "Missing Permissions")

        results = await execute(plan, guild, "test")

        assert [ok for ok, _ in results] == [False, None]
        guild.create_voice_channel.assert_not_awaited()

    async def test_give_role_rechecks_the_role_right_before_granting(self, guild: MagicMock) -> None:
        plan = Plan()
        Toolbox(guild, plan).call(_call("give_role", member=str(ZACH), role="New Arrival"))
        guild.get_role(NEW_ARRIVAL).permissions = discord.Permissions(kick_members=True)  # gained powers since

        results = await execute(plan, guild, "test")

        assert results[0][0] is False
        _member_by_id(guild, ZACH).add_roles.assert_not_awaited()

    async def test_allow_role_only_adds_access(self, guild: MagicMock) -> None:
        general = guild.get_channel(GENERAL)
        arrival = guild.get_role(NEW_ARRIVAL)
        general.overwrites[arrival] = discord.PermissionOverwrite(view_channel=True, attach_files=False)
        plan = Plan()
        Toolbox(guild, plan).call(_call("allow_role_in_channel", channel="general", role="New Arrival"))

        await execute(plan, guild, "test")

        overwrite = general.set_permissions.await_args.kwargs["overwrite"]
        assert overwrite.attach_files is False
        assert overwrite.send_messages is True


def _interaction(guild: MagicMock, user: MagicMock) -> MagicMock:
    interaction = MagicMock()
    interaction.guild = guild
    interaction.user = user
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.client.send_log = AsyncMock()
    return interaction


async def _zach_plan_view(guild: MagicMock) -> PlanView:
    plan = Plan()
    box = Toolbox(guild, plan)
    for call in ZACHS_ASK:
        box.call(call)
    return PlanView(plan, requester=_member_by_id(guild, ZACH), gm_role_ids=[GM_ROLE])


class TestApproval:
    async def test_non_quest_master_cannot_approve(self, guild: MagicMock) -> None:
        view = await _zach_plan_view(guild)
        guest = make_member(777, "guest", [guild.default_role], manage_channels=True, manage_roles=True)
        interaction = _interaction(guild, guest)

        await view.approve.callback(interaction)

        assert interaction.response.send_message.await_args.args[0] == "Only Quest Masters can approve."
        guild.create_role.assert_not_awaited()

    async def test_quest_master_without_the_discord_permissions_cannot_approve(self, guild: MagicMock) -> None:
        view = await _zach_plan_view(guild)
        interaction = _interaction(guild, _member_by_id(guild, ZACH))

        await view.approve.callback(interaction)

        message = interaction.response.send_message.await_args.args[0]
        assert "Step 1: needs manage_roles" in message
        assert "Step 2: needs manage_channels, manage_roles" in message
        guild.create_role.assert_not_awaited()

    async def test_approval_runs_the_plan_once_and_logs_it(self, guild: MagicMock) -> None:
        view = await _zach_plan_view(guild)
        coffee = _member_by_id(guild, COFFEE)

        first, second = _interaction(guild, coffee), _interaction(guild, coffee)
        await view.approve.callback(first)
        await view.approve.callback(second)

        guild.create_role.assert_awaited_once()
        guild.create_voice_channel.assert_awaited_once()
        assert guild.create_role.await_args.kwargs["reason"].endswith(f"approved by {coffee} ({COFFEE})")
        assert second.response.send_message.await_args.args[0] == "Someone already handled this plan."
        logged = first.client.send_log.await_args.args[1]
        assert logged.title == "Plan done"

    async def test_cancel_drops_the_plan(self, guild: MagicMock) -> None:
        view = await _zach_plan_view(guild)
        await view.cancel.callback(_interaction(guild, _member_by_id(guild, ZACH)))

        await view.approve.callback(_interaction(guild, _member_by_id(guild, COFFEE)))

        guild.create_role.assert_not_awaited()


def _message(guild: MagicMock, author: MagicMock, content: str) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.guild = guild
    message.author = author
    message.content = content
    message.mentions = [SimpleNamespace(id=BOT_ID)] if f"<@{BOT_ID}>" in content else []
    message.reference = None
    message.channel = MagicMock()
    message.channel.name = "officers"
    message.reply = AsyncMock()
    return message


def _cog(llm: Any) -> AssistantCog:
    bot = SimpleNamespace(user=SimpleNamespace(id=BOT_ID))
    return AssistantCog(bot, llm=llm, gm_role_ids=[GM_ROLE])  # type: ignore[arg-type]


class TestCog:
    async def test_ignores_mentions_from_members_who_arent_quest_masters(self, guild: MagicMock) -> None:
        llm = FakeLLM()
        guest = make_member(777, "guest", [guild.default_role])
        await _cog(llm).on_message(_message(guild, guest, f"<@{BOT_ID}> make me an admin role"))
        assert llm.sent == []

    async def test_ignores_quest_master_messages_without_a_mention(self, guild: MagicMock) -> None:
        llm = FakeLLM()
        await _cog(llm).on_message(_message(guild, _member_by_id(guild, COFFEE), "we need a voice channel"))
        assert llm.sent == []

    async def test_quest_master_gets_a_plan_to_approve(self, guild: MagicMock) -> None:
        llm = FakeLLM(
            _model_turn(calls=[_call("list_channels"), _call("list_roles")]),
            _model_turn(calls=ZACHS_ASK),
            _model_turn("Planned a Friends role and a General Voice channel they can join."),
        )
        message = _message(guild, _member_by_id(guild, COFFEE), f"<@{BOT_ID}> do what Zach asked")

        await _cog(llm).on_message(message)

        user_prompt = llm.sent[0][1]["content"]
        assert "do what Zach asked" in user_prompt
        assert f"<@{BOT_ID}>" not in user_prompt
        tool_results = [m["content"] for m in llm.sent[1] if m["role"] == "tool"]
        assert "category Guild Hall" in tool_results[0]
        kwargs = message.reply.await_args.kwargs
        assert message.reply.await_args.args[0].startswith("Planned a Friends role")
        assert "General Voice" in kwargs["embed"].description
        assert isinstance(kwargs["view"], PlanView)
        guild.create_role.assert_not_awaited()

    async def test_says_so_when_the_model_api_is_down(self, guild: MagicMock) -> None:
        llm = FakeLLM()
        llm.complete = AsyncMock(side_effect=LLMError("down"))  # type: ignore[method-assign]
        message = _message(guild, _member_by_id(guild, COFFEE), f"<@{BOT_ID}> hi")

        await _cog(llm).on_message(message)

        assert message.reply.await_args.args[0] == LLM_DOWN

    async def test_one_request_at_a_time_per_person(self, guild: MagicMock) -> None:
        cog = _cog(FakeLLM())
        cog._busy.add(COFFEE)
        message = _message(guild, _member_by_id(guild, COFFEE), f"<@{BOT_ID}> hi")

        await cog.on_message(message)

        assert message.reply.await_args.args[0] == BUSY


async def test_plan_request_gives_up_after_too_many_rounds(guild: MagicMock) -> None:
    llm = FakeLLM(*[_model_turn(calls=[_call("list_roles")]) for _ in range(20)])
    result = await plan_request(llm, guild, "loop forever")  # type: ignore[arg-type]
    assert "too many steps" in result.text
    assert result.plan.steps == []


class TestChatClient:
    URL = "https://llm.test/v1/chat/completions"

    @respx.mock
    async def test_sends_model_tools_and_reasoning_and_keeps_what_claude_needs_back(self) -> None:
        route = respx.post(self.URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}],
                                "tool_calls": [
                                    {
                                        "id": "a",
                                        "type": "function",
                                        "function": {"name": "list_roles", "arguments": "{}"},
                                    },
                                    {
                                        "id": "b",
                                        "type": "function",
                                        "function": {"name": "give_role", "arguments": "{oops"},
                                    },
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 1200, "completion_tokens": 80, "cost": 0.00016},
                },
            )
        )
        async with httpx.AsyncClient() as http:
            client = ChatClient("https://llm.test/v1/", "key", "cheap-model", reasoning="low", http=http)
            reply = await client.complete([{"role": "user", "content": "hi"}], [{"type": "function"}])

        sent = json.loads(route.calls.last.request.content)
        assert sent["model"] == "cheap-model"
        assert sent["tools"] == [{"type": "function"}]
        assert route.calls.last.request.headers["Authorization"] == "Bearer key"
        assert reply.tool_calls == [ToolCall("a", "list_roles", {}), ToolCall("b", "give_role", None)]
        assert reply.message["tool_calls"][1]["function"]["arguments"] == "{oops"
        assert sent["reasoning"] == {"effort": "low"}
        assert reply.message["reasoning_details"] == [{"type": "reasoning.encrypted", "data": "opaque"}]
        assert reply.usage == Usage(1200, 80, 0.00016)

    @respx.mock
    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(500, text="boom"),
            httpx.Response(200, json={"choices": []}),
            httpx.Response(200, text="<html>"),
        ],
        ids=["server error", "no choices", "not json"],
    )
    async def test_bad_answers_raise_llm_error(self, response: httpx.Response) -> None:
        respx.post(self.URL).mock(return_value=response)
        async with httpx.AsyncClient() as http:
            client = ChatClient("https://llm.test/v1", "key", "m", http=http)
            with pytest.raises(LLMError):
                await client.complete([], [])

    @respx.mock
    async def test_waits_out_a_rate_limit_then_gives_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        waits: list[float] = []
        monkeypatch.setattr("sidequest_bot.lib.llm.asyncio.sleep", AsyncMock(side_effect=waits.append))
        limited = httpx.Response(429, headers={"retry-after": "7"}, text="slow down")
        ok = httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
        route = respx.post(self.URL).mock(side_effect=[limited, limited, ok])
        async with httpx.AsyncClient() as http:
            client = ChatClient("https://llm.test/v1", "key", "m", http=http)
            assert (await client.complete([])).text == "hi"
            assert waits == [7.0, 7.0]

            route.side_effect = [limited] * 4
            with pytest.raises(LLMError, match="429"):
                await client.complete([])
        assert route.call_count == 7
