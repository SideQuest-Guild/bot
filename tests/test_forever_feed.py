from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import respx

from sidequest_bot.plugins.forever_feed import (
    FIRST_RUN_ITEMS,
    FORUMS_URL,
    MAX_AGE,
    TRACKER_URL,
    FeedItem,
    ForeverFeedCog,
    TrackerPost,
    discourse_to_discord,
    first_image_url,
    is_forever_post,
    parse_wowhead_rss,
    select_unposted,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
BOT_ID = 1


def _post(**overrides: object) -> TrackerPost:
    base: dict[str, object] = {
        "id": 1,
        "created_at": NOW,
        "topic_title": "Some Topic",
        "category_id": 171,
        "name": "Kaivax",
        "username": "Kaivax",
    }
    base.update(overrides)
    return TrackerPost.model_validate(base)


def _item(url: str, *, age: timedelta = timedelta(hours=1)) -> FeedItem:
    return FeedItem(
        source="wowhead", url=url, title=url, published_at=NOW - age, body="", byline="Wowhead", image_url=None
    )


class TestIsForeverPost:
    def test_post_in_forever_category(self) -> None:
        assert is_forever_post(_post(category_id=347), {347}) is True

    def test_forever_title_outside_forever_categories(self) -> None:
        assert is_forever_post(_post(topic_title="World of Warcraft: Forever Goes Live Globally!"), set()) is True
        assert is_forever_post(_post(topic_title="WoW Forever Beta Development Notes"), set()) is True

    def test_other_wow_news_is_not_forever(self) -> None:
        assert is_forever_post(_post(topic_title="World of Warcraft: Midnight Hotfixes - October 27"), {347}) is False

    def test_bare_word_forever_is_not_enough(self) -> None:
        assert is_forever_post(_post(topic_title="Transmog Gone Forever?"), {347}) is False


class TestDiscourseToDiscord:
    def test_drops_images_and_converts_links(self) -> None:
        raw = (
            "![image|833x468](upload://abc.jpeg)\n"
            '<a href="https://cdn.example/x.png"><img src="https://cdn.example/x.png"/></a>Intro text. '
            '<a href="https://worldofwarcraft.com/news/1">View Full Article</a>'
        )
        assert discourse_to_discord(raw) == "Intro text. [View Full Article](https://worldofwarcraft.com/news/1)"

    def test_headings_become_bold_and_bbcode_goes(self) -> None:
        raw = "## **TALES TAKE A DARK TURN**\n\n[quote]Quoted line[/quote]\n* Bullet"
        assert discourse_to_discord(raw) == "**TALES TAKE A DARK TURN**\n\nQuoted line\n* Bullet"

    def test_keeps_guild_names_in_angle_brackets(self) -> None:
        assert discourse_to_discord("Congrats to <SideQuest> on the world first!") == (
            "Congrats to <SideQuest> on the world first!"
        )

    def test_long_post_is_cut_at_a_line_break_within_the_limit(self) -> None:
        raw = "\n".join(f"* Change number {n} to an ability." for n in range(200))
        text = discourse_to_discord(raw, limit=500)
        assert len(text) <= 500
        assert text.endswith("\n…")
        assert text.splitlines()[-2].endswith("to an ability.")


class TestFirstImageUrl:
    def test_skips_emoji_and_avatars(self) -> None:
        cooked = (
            '<img src="https://forums.example/emoji/smile.png" class="emoji">'
            '<img class="avatar" src="https://forums.example/avatar.png">'
            '<img src="https://cdn.example/hero.jpeg?a=1&amp;b=2" alt="image">'
        )
        assert first_image_url(cooked) == "https://cdn.example/hero.jpeg?a=1&b=2"

    def test_none_without_images(self) -> None:
        assert first_image_url("<p>No pictures here.</p>") is None


WOWHEAD_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
  <item>
    <title>Blizzard Working on Top Layering Issues in WoW: Forever</title>
    <link>https://www.wowhead.com/forever/news=383650/blizzard-working-on-top-layering-issues</link>
    <description><![CDATA[Blizzard addressed layering &amp; sharding.<br><br>
<em><b><a href="https://www.wowhead.com/forever/news=383650">Continue reading »</a></b></em>]]></description>
    <category>Forever</category>
    <pubDate>Thu, 29 Oct 2026 13:12:11 -0500</pubDate>
    <media:content url="https://wow.zamimg.com/uploads/blog/images/55952.jpg" medium="image"/>
  </item>
  <item>
    <title>Two New Housing Customizations on 12.1.7 PTR</title>
    <link>https://www.wowhead.com/forever/news=383660/two-new-housing</link>
    <description>Retail news.</description>
    <category>PTR</category>
    <pubDate>Thu, 29 Oct 2026 18:07:55 -0500</pubDate>
  </item>
  <item>
    <title>Forever item with a broken date</title>
    <link>https://www.wowhead.com/forever/news=1/broken</link>
    <category>Forever</category>
    <pubDate>not a date</pubDate>
  </item>
</channel>
</rss>
"""


class TestParseWowheadRss:
    def test_keeps_only_forever_items_with_teaser_and_image(self) -> None:
        items = parse_wowhead_rss(WOWHEAD_RSS)
        assert [item.title for item in items] == ["Blizzard Working on Top Layering Issues in WoW: Forever"]
        item = items[0]
        assert item.body == "Blizzard addressed layering & sharding."
        assert item.image_url == "https://wow.zamimg.com/uploads/blog/images/55952.jpg"
        assert item.published_at == datetime(2026, 10, 29, 18, 12, 11, tzinfo=UTC)


class TestSelectUnposted:
    def test_skips_posted_and_stale_items_oldest_first(self) -> None:
        items = [
            _item("https://a/new", age=timedelta(minutes=5)),
            _item("https://a/posted"),
            _item("https://a/older", age=timedelta(hours=20)),
            _item("https://a/stale", age=MAX_AGE + timedelta(minutes=1)),
        ]
        selected = select_unposted(items, {"https://a/posted"}, has_history=True, now=NOW)
        assert [item.url for item in selected] == ["https://a/older", "https://a/new"]

    def test_fresh_channel_gets_only_the_newest_few(self) -> None:
        items = [_item(f"https://a/{n}", age=timedelta(hours=n)) for n in range(FIRST_RUN_ITEMS + 4)]
        selected = select_unposted(items, set(), has_history=False, now=NOW)
        assert [item.url for item in selected] == [f"https://a/{n}" for n in reversed(range(FIRST_RUN_ITEMS))]


def _message(author_id: int, urls: list[str]) -> SimpleNamespace:
    return SimpleNamespace(author=SimpleNamespace(id=author_id), embeds=[discord.Embed(url=url) for url in urls])


def _channel(history: list[SimpleNamespace]) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)

    async def _history(*, limit: int):
        for message in history:
            yield message

    channel.history = _history
    channel.send = AsyncMock()
    return channel


def _cog(http: httpx.AsyncClient | None = None) -> ForeverFeedCog:
    cog = ForeverFeedCog.__new__(ForeverFeedCog)
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=BOT_ID))
    cog._http = http or httpx.AsyncClient()
    cog._category_is_forever = {}
    cog._blue_items = {}
    return cog


class TestPostNew:
    async def test_posts_only_links_the_bot_has_not_posted(self) -> None:
        recent = datetime.now(UTC) - timedelta(hours=1)
        posted = FeedItem("wowhead", "https://a/posted", "Old", recent, "", "Wowhead")
        fresh = FeedItem("wowhead", "https://a/fresh", "New", recent, "", "Wowhead")
        channel = _channel([_message(BOT_ID, ["https://a/posted"]), _message(999, ["https://a/fresh"])])

        await _cog()._post_new(channel, [posted, fresh])

        channel.send.assert_awaited_once()
        assert channel.send.await_args.kwargs["embed"].url == "https://a/fresh"

    async def test_other_peoples_messages_dont_end_the_first_run_cap(self) -> None:
        recent = datetime.now(UTC) - timedelta(hours=1)
        items = [FeedItem("wowhead", f"https://a/{n}", "T", recent, "", "Wowhead") for n in range(FIRST_RUN_ITEMS + 2)]
        channel = _channel([_message(999, [])])

        await _cog()._post_new(channel, items)

        assert channel.send.await_count == FIRST_RUN_ITEMS


def _tracker_post(post_id: int, *, category_id: int, title: str) -> dict[str, object]:
    return {
        "id": post_id,
        "created_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "topic_title": title,
        "category_id": category_id,
        "name": "Kaivax",
        "username": "Kaivax",
    }


class TestFetchBlueItems:
    @respx.mock
    async def test_keeps_forever_posts_and_fetches_each_category_and_body_once(self) -> None:
        respx.get(TRACKER_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "posts": [
                        _tracker_post(10, category_id=347, title="Name Reservation Issue"),
                        _tracker_post(11, category_id=171, title="Midnight Hotfixes - October 27"),
                    ]
                },
            )
        )
        forever_category = respx.get(f"{FORUMS_URL}/c/347/show.json").mock(
            return_value=httpx.Response(200, json={"category": {"slug": "world-of-warcraft-forever-general"}})
        )
        respx.get(f"{FORUMS_URL}/c/171/show.json").mock(
            return_value=httpx.Response(200, json={"category": {"slug": "general-discussion"}})
        )
        detail = respx.get(f"{FORUMS_URL}/posts/10.json").mock(
            return_value=httpx.Response(200, json={"raw": "We fixed it.", "cooked": "<p>We fixed it.</p>"})
        )
        cog = _cog()

        items = await cog._fetch_blue_items()
        await cog._fetch_blue_items()

        assert [(item.url, item.body, item.byline) for item in items] == [
            (f"{FORUMS_URL}/p/10", "We fixed it.", "Blue post by Kaivax")
        ]
        assert forever_category.call_count == 1
        assert detail.call_count == 1

    @respx.mock
    async def test_tracker_outage_yields_nothing_without_raising(self) -> None:
        respx.get(TRACKER_URL).mock(return_value=httpx.Response(503))
        assert await _cog()._fetch_blue_items() == []
