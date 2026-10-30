"""WoW: Forever news feed: Blizzard's blue posts and Wowhead's news, one embed each, in ``forever_feed_channel_id``.

Every ``CHECK_INTERVAL_SECONDS``, if any guild has a feed channel configured, the bot reads two public sources once:

- the Blizzard forums' blue-post tracker, kept to posts in a WoW: Forever category (any category whose slug says
  ``forever``, so new Forever subforums count without a code change) or titled "WoW: Forever"/"Warcraft: Forever",
  each rendered from the post's own Markdown;
- Wowhead's news RSS, kept to items in its ``Forever`` category.

Then, per guild, it posts every item from the last ``MAX_AGE`` whose link isn't already on one of its recent messages
in that channel, oldest first. The channel itself is the record of what was posted, so restarts and redeploys never
double-post and the API holds no feed state. A channel the bot has never posted in gets only the newest
``FIRST_RUN_ITEMS`` items rather than days of backlog.
"""

import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Final, Literal

import discord
import httpx
from defusedxml import DefusedXmlException, ElementTree
from discord import app_commands
from discord.ext import commands, tasks
from pydantic import BaseModel, ValidationError

from sidequest_bot.api_client import APIError
from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors
from sidequest_common.schemas import GuildConfigUpdate

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300
MAX_AGE: Final = timedelta(days=2)
"""Items older than this are never posted, so the channel-history check below only has to cover this window."""
HISTORY_LIMIT = 100
"""How many recent channel messages are read to find what was already posted (~15 items/day x 2 days fits)."""
FIRST_RUN_ITEMS = 3

FORUMS_URL: Final = "https://us.forums.blizzard.com/en/wow"
TRACKER_URL: Final = f"{FORUMS_URL}/groups/blizzard-tracker/posts.json"
WOWHEAD_RSS_URL: Final = "https://www.wowhead.com/forever/news/rss/all"
WOWHEAD_CATEGORY: Final = "Forever"
USER_AGENT: Final = "SideQuestGuildBot (+https://sidequest-guild.com)"

DESCRIPTION_LIMIT = 1500
TITLE_LIMIT = 256
AUTHOR_NAME: Final = "World of Warcraft: Forever"

FOREVER_TITLE_RE: Final = re.compile(r"\b(?:wow|warcraft):?\s+forever\b", re.IGNORECASE)
_MD_IMAGE_RE: Final = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_LINK_RE: Final = re.compile(r'<a\b[^>]*\bhref="(https?://[^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_BBCODE_RE: Final = re.compile(r"\[/?(?:quote|details|spoiler|center|left|right|size|color|wrap|grid)\b[^\]]*\]")
_HTML_TAG_RE: Final = re.compile(r"</?(?:br|small|big|sup|sub|div|span|p|u|s|del|ins|font|center|kbd)\b[^>]*>")
_HEADING_RE: Final = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_BLANK_RUN_RE: Final = re.compile(r"\n{3,}")
_IMG_TAG_RE: Final = re.compile(r"<img\b[^>]*>")
_IMG_SRC_RE: Final = re.compile(r'\bsrc="(https://[^"]+)"')
_IMG_CLASS_RE: Final = re.compile(r'\bclass="([^"]*)"')
_SKIPPED_IMG_CLASSES: Final = frozenset({"emoji", "avatar", "site-icon"})
_BR_RE: Final = re.compile(r"<br\s*/?>")
_ANY_TAG_RE: Final = re.compile(r"<[^>]+>")
_CONTINUE_READING_RE: Final = re.compile(r"\s*Continue reading\s*»?\s*$")
_MEDIA_CONTENT: Final = "{http://search.yahoo.com/mrss/}content"

Source = Literal["blizzard", "wowhead"]


@dataclass(frozen=True, slots=True)
class FeedItem:
    """One post ready to render. ``url`` is ASCII-only and doubles as the dedupe key read back from embeds."""

    source: Source
    url: str
    title: str
    published_at: datetime
    body: str
    byline: str
    image_url: str | None = None


class TrackerPost(BaseModel):
    """One entry of the forums' ``blizzard-tracker`` group posts feed (the fields we use)."""

    id: int
    created_at: datetime
    topic_title: str
    category_id: int
    name: str | None = None
    username: str


class TrackerPage(BaseModel):
    """``GET /groups/blizzard-tracker/posts.json``: the newest ~20 blue posts across every WoW forum."""

    posts: list[TrackerPost]


class PostDetail(BaseModel):
    """``GET /posts/{id}.json``: the post's Markdown source and its rendered HTML (for image URLs)."""

    raw: str
    cooked: str


class CategoryInfo(BaseModel):
    """The one field of a forum category we read."""

    slug: str


class CategoryShow(BaseModel):
    """``GET /c/{id}/show.json``, reduced to the slug."""

    category: CategoryInfo


def is_forever_post(post: TrackerPost, forever_category_ids: set[int]) -> bool:
    """Whether a blue post is about WoW: Forever: posted in a Forever category, or titled as Forever news."""
    return post.category_id in forever_category_ids or FOREVER_TITLE_RE.search(post.topic_title) is not None


def clip(text: str, limit: int) -> str:
    """``text`` if it fits in ``limit``; else cut at the last line break in the back half, ending in an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 2]
    newline = cut.rfind("\n")
    if newline > limit // 2:
        cut = cut[:newline]
    return cut.rstrip() + "\n…"


def _markdown_link(match: re.Match[str]) -> str:
    text = match[2].strip()
    return f"[{text}]({html.unescape(match[1])})" if text else ""


def discourse_to_discord(raw: str, limit: int = DESCRIPTION_LIMIT) -> str:
    """Discourse post Markdown reshaped for an embed description, cut at a line break to fit ``limit``.

    Inline images go (an embed shows only one, set separately), HTML links become Markdown links (dropped if
    they wrapped only an image), BBCode wrappers and stray HTML tags are dropped keeping their text, and headings
    become bold lines.
    """
    text = _MD_IMAGE_RE.sub("", raw)
    text = _IMG_TAG_RE.sub("", text)
    text = _HTML_LINK_RE.sub(_markdown_link, text)
    text = _BBCODE_RE.sub("", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _HEADING_RE.sub(lambda match: f"**{match[1].strip('* ')}**", text)
    text = _BLANK_RUN_RE.sub("\n\n", text).strip()
    return clip(text, limit)


def first_image_url(cooked: str) -> str | None:
    """The first real picture in a post's rendered HTML, skipping emoji, avatars and site icons."""
    for tag in _IMG_TAG_RE.findall(cooked):
        classes = _IMG_CLASS_RE.search(tag)
        if classes and _SKIPPED_IMG_CLASSES & set(classes[1].split()):
            continue
        src = _IMG_SRC_RE.search(tag)
        if src:
            return html.unescape(src[1])
    return None


def blue_post_item(post: TrackerPost, detail: PostDetail) -> FeedItem:
    """A tracker post plus its body as a ``FeedItem``, linked by the forum's ASCII ``/p/<id>`` short link."""
    return FeedItem(
        source="blizzard",
        url=f"{FORUMS_URL}/p/{post.id}",
        title=post.topic_title,
        published_at=post.created_at,
        body=discourse_to_discord(detail.raw),
        byline=f"Blue post by {post.name or post.username}",
        image_url=first_image_url(detail.cooked),
    )


def wowhead_teaser(description_html: str) -> str:
    """Wowhead's RSS description as plain text, without its trailing "Continue reading »" link."""
    text = html.unescape(_ANY_TAG_RE.sub("", _BR_RE.sub("\n", description_html)))
    text = _CONTINUE_READING_RE.sub("", text)
    return clip(_BLANK_RUN_RE.sub("\n\n", text).strip(), DESCRIPTION_LIMIT)


def parse_wowhead_rss(xml_text: str) -> list[FeedItem]:
    """Wowhead news RSS items in the ``Forever`` category; items missing a title, link or date are skipped."""
    root = ElementTree.fromstring(xml_text)
    items: list[FeedItem] = []
    for node in root.iterfind("channel/item"):
        if WOWHEAD_CATEGORY not in {(category.text or "").strip() for category in node.iterfind("category")}:
            continue
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        published = node.findtext("pubDate")
        if not title or not link.startswith("https://") or not published:
            continue
        try:
            published_at = parsedate_to_datetime(published)
        except TypeError, ValueError:
            continue
        media = node.find(_MEDIA_CONTENT)
        items.append(
            FeedItem(
                source="wowhead",
                url=link,
                title=title,
                published_at=published_at,
                body=wowhead_teaser(node.findtext("description") or ""),
                byline="Wowhead",
                image_url=media.get("url") if media is not None else None,
            )
        )
    return items


def select_unposted(
    items: Iterable[FeedItem], posted_urls: set[str], *, has_history: bool, now: datetime
) -> list[FeedItem]:
    """Items from the last ``MAX_AGE`` not yet posted, oldest first; only the newest few in a fresh channel."""
    fresh = sorted(
        (item for item in items if item.url not in posted_urls and now - item.published_at <= MAX_AGE),
        key=lambda item: item.published_at,
    )
    return fresh if has_history else fresh[-FIRST_RUN_ITEMS:]


def build_item_embed(item: FeedItem) -> discord.Embed:
    """Blizzard-blue embed for a blue post, Wowhead-red for a Wowhead article; the title links to the source."""
    embed = discord.Embed(
        title=clip(item.title, TITLE_LIMIT),
        url=item.url,
        description=item.body or None,
        colour=Colors.BLIZZARD_BLUE if item.source == "blizzard" else Colors.WOWHEAD_RED,
        timestamp=item.published_at,
    )
    embed.set_author(name=AUTHOR_NAME)
    embed.set_footer(text=item.byline)
    if item.image_url:
        embed.set_image(url=item.image_url)
    return embed


class ForeverFeedCog(commands.Cog):
    """Periodic WoW: Forever news poll, posting new items to each guild's ``forever_feed_channel_id``."""

    forever_feed_group = app_commands.Group(
        name="foreverfeed",
        description="Configure the WoW: Forever news feed.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: SideQuestBot, *, http: httpx.AsyncClient | None = None) -> None:
        self.bot = bot
        self._http = http or httpx.AsyncClient(timeout=15.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
        self._category_is_forever: dict[int, bool] = {}
        self._blue_items: dict[int, FeedItem] = {}
        """Rendered blue posts by post id, so each body is fetched once; pruned to what the tracker still lists."""
        self._check_loop.start()

    async def cog_unload(self) -> None:
        """Stop the poll and close the HTTP client."""
        self._check_loop.cancel()
        await self._http.aclose()

    @forever_feed_group.command(name="channel", description="Set (or clear) the channel WoW: Forever news posts in.")
    @app_commands.describe(channel="Channel to post WoW: Forever news in; omit to disable.")
    async def forever_feed_channel(
        self, interaction: discord.Interaction[SideQuestBot], channel: discord.TextChannel | None = None
    ) -> None:
        """Update ``forever_feed_channel_id``."""
        guild = interaction.guild
        if guild is None:
            return
        await interaction.client.configs.update(
            guild.id, GuildConfigUpdate(forever_feed_channel_id=channel.id if channel else None)
        )
        message = (
            f"WoW: Forever news will post in {channel.mention} within {CHECK_INTERVAL_SECONDS // 60} minutes."
            if channel
            else "WoW: Forever news feed disabled."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def _check_loop(self) -> None:
        channels = await self._feed_channels()
        if not channels:
            return
        items = [*await self._fetch_blue_items(), *await self._fetch_wowhead_items()]
        for channel in channels:
            try:
                await self._post_new(channel, items)
            except discord.HTTPException:
                # Must not escape: an unhandled exception stops a tasks.loop for good. Retried next tick.
                logger.warning("Forever feed post failed in channel %s", channel.id, exc_info=True)

    @_check_loop.before_loop
    async def _before_check_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _feed_channels(self) -> list[discord.TextChannel]:
        channels: list[discord.TextChannel] = []
        for guild in list(self.bot.guilds):
            try:
                config = await self.bot.configs.get(guild.id)
            except APIError:
                logger.warning("Couldn't read config for guild %s; skipping its Forever feed", guild.id, exc_info=True)
                continue
            if config.forever_feed_channel_id is None:
                continue
            channel = guild.get_channel(config.forever_feed_channel_id)
            if isinstance(channel, discord.TextChannel):
                channels.append(channel)
        return channels

    async def _get_json[T: BaseModel](self, url: str, model: type[T]) -> T | None:
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            return model.model_validate_json(response.content)
        except httpx.HTTPError, ValidationError:
            logger.warning("Forever feed couldn't read %s", url, exc_info=True)
            return None

    async def _is_forever_category(self, category_id: int) -> bool:
        cached = self._category_is_forever.get(category_id)
        if cached is not None:
            return cached
        show = await self._get_json(f"{FORUMS_URL}/c/{category_id}/show.json", CategoryShow)
        if show is None:
            return False  # not cached: asked again next tick
        self._category_is_forever[category_id] = "forever" in show.category.slug
        return self._category_is_forever[category_id]

    async def _fetch_blue_items(self) -> list[FeedItem]:
        page = await self._get_json(TRACKER_URL, TrackerPage)
        if page is None:
            return list(self._blue_items.values())
        listed = {post.id for post in page.posts}
        self._blue_items = {post_id: item for post_id, item in self._blue_items.items() if post_id in listed}
        forever_ids = {
            category_id
            for category_id in {post.category_id for post in page.posts}
            if await self._is_forever_category(category_id)
        }
        now = datetime.now(UTC)
        for post in page.posts:
            if post.id in self._blue_items or now - post.created_at > MAX_AGE:
                continue
            if not is_forever_post(post, forever_ids):
                continue
            detail = await self._get_json(f"{FORUMS_URL}/posts/{post.id}.json", PostDetail)
            if detail is not None:
                self._blue_items[post.id] = blue_post_item(post, detail)
        return list(self._blue_items.values())

    async def _fetch_wowhead_items(self) -> list[FeedItem]:
        try:
            response = await self._http.get(WOWHEAD_RSS_URL)
            response.raise_for_status()
            return parse_wowhead_rss(response.text)
        except httpx.HTTPError, ElementTree.ParseError, DefusedXmlException:
            logger.warning("Forever feed couldn't read %s", WOWHEAD_RSS_URL, exc_info=True)
            return []

    async def _post_new(self, channel: discord.TextChannel, items: Sequence[FeedItem]) -> None:
        me = self.bot.user
        if me is None:
            return
        posted_urls: set[str] = set()
        has_history = False
        async for message in channel.history(limit=HISTORY_LIMIT):
            if message.author.id != me.id:
                continue
            has_history = True
            posted_urls.update(embed.url for embed in message.embeds if embed.url)
        for item in select_unposted(items, posted_urls, has_history=has_history, now=datetime.now(UTC)):
            await channel.send(embed=build_item_embed(item))


async def setup(bot: SideQuestBot) -> None:
    """Register the cog."""
    await bot.add_cog(ForeverFeedCog(bot))
