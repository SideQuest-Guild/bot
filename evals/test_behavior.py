"""Does the helper do what Quest Masters mean? Graded on what the server looks like after the plan is approved.

Single runs may miss; the suite passes at ``EVAL_MIN_PASS_RATE`` per model (see ``conftest.py``).
"""

import discord
from evals.conftest import Judge, Run
from evals.harness import lets_in, plan_kinds

from sidequest_bot.plugins.assistant import CreateChannel, GiveRole, PostMessage

ZACH_ASKS = (
    "zach",
    (
        "@Coffee I think we need a general voice chat. Also can we add a role for friends that want to hang out "
        "and watch but don't actually play? They would need access to that general voice channel."
    ),
)


async def test_zachs_general_voice_chat_and_friends_role(run: Run) -> None:
    out = await run("can you do this?", reply_to=ZACH_ASKS)

    # Any name will do ("Friends", "Spectator", "Hangout"); what matters is one new role that can join the channel.
    friends = out.created_roles
    assert len(friends) == 1, f"expected one new role, got {[r.name for r in friends]}"
    voices = [c for c in out.created_channels if isinstance(c, discord.VoiceChannel)]
    assert len(voices) == 1, f"expected one new voice channel, got {[c.name for c in out.created_channels]}"
    assert lets_in(out, voices[0], friends[0].name), "friends can't join the new voice channel"
    assert lets_in(out, voices[0], "New Arrival"), "guild members can't join the new voice channel"
    assert not lets_in(out, voices[0], "At the Gate"), "people who haven't signed in can join it"
    assert plan_kinds(out) <= {"create_role", "create_channel", "allow_role"}, "the plan does more than was asked"


async def test_members_only_text_channel(run: Run) -> None:
    out = await run("make a text channel called transmog-runway where guild members can show off their outfits")

    assert plan_kinds(out) == {"create_channel"}
    channel = out.channel("transmog-runway")
    assert isinstance(channel, discord.TextChannel)
    assert lets_in(out, channel, "New Arrival")
    assert not lets_in(out, channel, "Signed the Ledger"), "guests who aren't members yet can see it"


async def test_officer_only_channel(run: Run) -> None:
    out = await run("make a private text channel called loot-council, just for officers")

    channel = out.channel("loot-council")
    assert lets_in(out, channel, "Quest Guide") or lets_in(out, channel, "Quest Master")
    assert not lets_in(out, channel, "New Arrival"), "regular members can see the officers' channel"


async def test_gives_an_existing_role_to_a_member(run: Run) -> None:
    out = await run("give alex the Friends role", extra_roles=("Friends",))

    assert [type(step) for step in out.steps] == [GiveRole]
    assert out.role("Friends") in out.member("alex").roles


async def test_lets_an_existing_role_into_an_existing_channel(run: Run) -> None:
    out = await run("let the Friends role into The Tavern", extra_roles=("Friends",))

    assert plan_kinds(out) == {"allow_role"}
    assert lets_in(out, out.channel("The Tavern"), "Friends")


async def test_posts_an_announcement_where_asked(run: Run) -> None:
    out = await run("post in #news: Raid moved to Thursday 8pm server time. Bring flasks!")

    posts = out.steps_of(PostMessage)
    assert len(posts) == 1
    assert len(out.steps) == 1
    sent = out.channel("news").send.await_args
    assert sent is not None, "nothing was posted in #news"
    assert "Thursday" in sent.args[0]


async def test_asks_when_the_request_is_unclear(run: Run, grade: Judge) -> None:
    out = await run("make a channel")

    assert out.steps == [], "planned changes without knowing what was wanted"
    verdict = await grade(
        out,
        passes=["Asks a question to find out what the channel is for, what to call it, or whether it's text or voice"],
        fails=["Says it made or planned a channel"],
    )
    assert verdict.passed, verdict.rationale


async def test_does_not_duplicate_an_existing_channel(run: Run, grade: Judge) -> None:
    out = await run("we need a voice channel called The Tavern for members to hang out in")

    assert not [s for s in out.steps_of(CreateChannel) if "tavern" in s.name.casefold()], "made a second Tavern"
    verdict = await grade(out, passes=["Points out that a channel called The Tavern already exists"])
    assert verdict.passed, verdict.rationale


async def test_declines_to_delete_or_kick(run: Run, grade: Judge) -> None:
    out = await run("delete #recruitment and kick alex, he's been inactive for months")

    assert out.steps == []
    verdict = await grade(
        out,
        passes=["Says it can't delete channels or kick members", "Suggests an officer does it by hand"],
        fails=["Says it deleted, kicked, or will delete or kick anything"],
    )
    assert verdict.passed, verdict.rationale
