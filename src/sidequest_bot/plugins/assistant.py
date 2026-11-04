"""The AI helper: a Quest Master mentions the bot with a request, a cheap model plans it, and a person approves it.

A member holding a ``WEB_GM_ROLE_IDS`` role mentions the bot (a reply that pings the bot counts, and the message
they replied to goes along as context). The model can look at the server's channels, roles and members, and can
*propose* changes that only add things: a role with no permissions, a channel or category, letting a role use a
channel, giving a member a role, posting a message. It has no tool that deletes, kicks, bans, takes a role away,
hides a channel or gives a role permissions.

Nothing changes until someone clicks **Approve** on the plan. The clicker must be a Quest Master and hold the
Discord permissions each step needs, so the bot never does what the approver couldn't do by hand. Approved plans are
logged to the log channel with who asked and who approved, and Discord's audit log carries the same as the reason.

Off unless ``AI_API_KEY`` is set. ``AI_BASE_URL`` and ``AI_MODEL`` pick any OpenAI-compatible provider and model.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import discord
from discord.ext import commands
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from sidequest_bot.bot import SideQuestBot  # noqa: TC001  # discord.py evaluates app-command annotations at runtime
from sidequest_bot.lib.embeds import Colors, make_embed, truncate, user_line
from sidequest_bot.lib.llm import ChatClient, LLMError, ToolCall, Usage
from sidequest_bot.lib.roles import configure_block_reason, grant_block_reason
from sidequest_bot.plugins.signup_notices import clicker_is_gm
from sidequest_common.settings import WebSettings

logger = logging.getLogger(__name__)

MAX_ROUNDS: Final = 8
"""Model turns per request; each turn may call several tools."""
MAX_STEPS: Final = 10
PLAN_TIMEOUT_SECONDS: Final = 15 * 60
MAX_FOUND_MEMBERS: Final = 10
MAX_NAME_LENGTH: Final = 100
MAX_MESSAGE_LENGTH: Final = 2000
MAX_TOPIC_LENGTH: Final = 1024

ChannelKind = Literal["text", "voice", "category"]
CHANNEL_KINDS: Final[tuple[ChannelKind, ...]] = ("text", "voice", "category")

_TEXT_ACCESS: Final = {"view_channel": True, "send_messages": True, "read_message_history": True, "add_reactions": True}
_VOICE_ACCESS: Final = {
    "view_channel": True,
    "connect": True,
    "speak": True,
    "stream": True,
    "use_voice_activation": True,
    "send_messages": True,
    "read_message_history": True,
}
ACCESS: Final[dict[ChannelKind, dict[str, bool]]] = {
    "text": _TEXT_ACCESS,
    "voice": _VOICE_ACCESS,
    "category": _TEXT_ACCESS | _VOICE_ACCESS,
}
"""What "this role can use the channel" turns on. None of it is a moderator permission."""

OUT_OF_ROUNDS: Final = "That took me too many steps to work out. Try asking for one thing at a time."
NO_ANSWER: Final = "I couldn't work out what to do with that."
LLM_DOWN: Final = "My AI helper isn't answering right now. Try again in a bit."
BUSY: Final = "I'm still working on your last request."

SYSTEM_PROMPT: Final = """\
You are Game Master, the Discord bot of SideQuest, a World of Warcraft: Forever guild. A Quest Master (a guild \
officer) is asking you for help with the Discord server.

Look at the server with list_channels, list_roles and find_member. Propose changes with create_role, \
create_channel, allow_role_in_channel, give_role and post_message. Proposed changes are not made yet: they are shown \
to the officers as a plan, and nothing happens until a Quest Master approves it.

You cannot delete anything, kick, ban, time out, take a role away, hide a channel, or give a role permissions. If \
asked to, say so in one sentence and suggest they do it by hand.

- Look before you propose, so names match exactly and you don't create something that already exists.
- A new channel for guild members goes in the category of similar channels or copies access from a similar \
channel, so it is not open to everyone by accident.
- New roles have no permissions. To let a role use a channel, use allow_roles on create_channel or \
allow_role_in_channel.
- You may refer to a role or channel that an earlier step of your plan creates by its name.
- If the request is unclear, ask one short question instead of proposing changes.
- End with a short, plain reply of one or two sentences saying what you planned and anything the officers should \
decide. The plan is shown separately, so don't list it. No headings."""


class AssistantSettings(BaseSettings):
    """AI helper settings (``AI_*``). Any OpenAI-compatible chat completions API works."""

    model_config = SettingsConfigDict(env_prefix="AI_", env_file=".env", extra="ignore", case_sensitive=False)

    API_KEY: SecretStr | None = None
    BASE_URL: str = "https://openrouter.ai/api/v1"
    MODEL: str = "anthropic/claude-haiku-5.5"
    REASONING: str = "low"
    """OpenRouter ``reasoning.effort``: none, minimal, low, medium, high. Thinking is billed as output."""


class PlanError(Exception):
    """A tool call the model got wrong; the message goes back to the model so it can fix it."""


class StepError(Exception):
    """An approved step that couldn't be carried out."""


@dataclass(frozen=True, slots=True)
class Pending:
    """A role or channel that an earlier step of the same plan creates, referred to by its name."""

    name: str


type Ref = int | Pending
"""An existing role or channel by id, or one the plan creates."""


@dataclass(slots=True)
class Execution:
    """State while carrying out an approved plan: what it has created so far, by name."""

    guild: discord.Guild
    reason: str
    roles: dict[str, discord.Role] = field(default_factory=dict)
    channels: dict[str, discord.abc.GuildChannel] = field(default_factory=dict)

    def role(self, ref: Ref) -> discord.Role:
        """The role ``ref`` points at now."""
        role = self.roles.get(ref.name.casefold()) if isinstance(ref, Pending) else self.guild.get_role(ref)
        if role is None:
            msg = "that role no longer exists"
            raise StepError(msg)
        return role

    def channel(self, ref: Ref) -> discord.abc.GuildChannel:
        """The channel ``ref`` points at now."""
        channel = self.channels.get(ref.name.casefold()) if isinstance(ref, Pending) else self.guild.get_channel(ref)
        if channel is None:
            msg = "that channel no longer exists"
            raise StepError(msg)
        return channel


def _role_label(guild: discord.Guild, ref: Ref) -> str:
    if isinstance(ref, Pending):
        return f"@{ref.name} (new)"
    role = guild.get_role(ref)
    return "@everyone" if role is not None and role.is_default() else f"@{role.name if role else ref}"


def _channel_label(guild: discord.Guild, ref: Ref) -> str:
    if isinstance(ref, Pending):
        return f"{ref.name} (new)"
    channel = guild.get_channel(ref)
    return channel.name if channel else str(ref)


def _missing(approver: discord.Member, *permissions: str) -> str | None:
    perms = approver.guild_permissions
    lacking = [name for name in permissions if not getattr(perms, name)]
    return f"needs {', '.join(lacking)}" if lacking else None


def _access_overwrite(existing: discord.PermissionOverwrite | None, kind: ChannelKind) -> discord.PermissionOverwrite:
    """``existing`` with the access permissions for ``kind`` turned on; nothing is turned off."""
    overwrite = discord.PermissionOverwrite(**dict(existing)) if existing else discord.PermissionOverwrite()
    overwrite.update(**ACCESS[kind])
    return overwrite


def channel_kind(channel: discord.abc.GuildChannel) -> ChannelKind:
    """The plan's word for ``channel``'s type; stage channels count as voice, forums and the rest as text."""
    if isinstance(channel, discord.CategoryChannel):
        return "category"
    if isinstance(channel, discord.VoiceChannel | discord.StageChannel):
        return "voice"
    return "text"


@dataclass(frozen=True, slots=True)
class CreateRole:
    """A new role with no permissions, at the bottom of the role list."""

    name: str
    colour: int | None = None
    hoist: bool = False
    mentionable: bool = False

    def describe(self, _guild: discord.Guild) -> str:
        """One line for the plan."""
        extras = ["no permissions"]
        if self.colour is not None:
            extras.append(f"colour #{self.colour:06x}")
        if self.hoist:
            extras.append("shown separately in the member list")
        if self.mentionable:
            extras.append("anyone can @mention it")
        return f"Create role **@{self.name}** ({', '.join(extras)})"

    def blocker(self, approver: discord.Member) -> str | None:
        """Why ``approver`` may not approve this step, or None."""
        return _missing(approver, "manage_roles")

    async def run(self, ctx: Execution) -> str:
        """Create the role and remember it for later steps."""
        role = await ctx.guild.create_role(
            name=self.name,
            permissions=discord.Permissions.none(),
            colour=discord.Colour(self.colour or 0),
            hoist=self.hoist,
            mentionable=self.mentionable,
            reason=ctx.reason,
        )
        ctx.roles[self.name.casefold()] = role
        return f"Created {role.mention}"


@dataclass(frozen=True, slots=True)
class CreateChannel:
    """A new channel or category. Access is copied from ``copy_from`` (or the category), then ``allow_roles`` added."""

    name: str
    kind: ChannelKind
    category: Ref | None = None
    copy_from: Ref | None = None
    allow_roles: tuple[Ref, ...] = ()
    topic: str | None = None

    def describe(self, guild: discord.Guild) -> str:
        """One line for the plan."""
        noun = "category" if self.kind == "category" else f"{self.kind} channel"
        line = f"Create {noun} **{self.name}**"
        if self.category is not None:
            line += f" in **{_channel_label(guild, self.category)}**"
        source = self.copy_from if self.copy_from is not None else self.category
        access = f"access copied from **{_channel_label(guild, source)}**" if source is not None else "open to everyone"
        if self.allow_roles:
            access += ", plus " + ", ".join(f"**{_role_label(guild, ref)}**" for ref in self.allow_roles)
        return f"{line}; {access}"

    def blocker(self, approver: discord.Member) -> str | None:
        """Why ``approver`` may not approve this step, or None."""
        if self.copy_from is not None or self.category is not None or self.allow_roles:
            return _missing(approver, "manage_channels", "manage_roles")
        return _missing(approver, "manage_channels")

    async def run(self, ctx: Execution) -> str:
        """Build the overwrites, create the channel, and remember it for later steps."""
        category = ctx.channel(self.category) if self.category is not None else None
        if category is not None and not isinstance(category, discord.CategoryChannel):
            msg = f"{category.name} isn't a category"
            raise StepError(msg)
        source = ctx.channel(self.copy_from) if self.copy_from is not None else category
        overwrites: dict[Any, discord.PermissionOverwrite] = dict(source.overwrites) if source is not None else {}
        for ref in self.allow_roles:
            role = ctx.role(ref)
            overwrites[role] = _access_overwrite(overwrites.get(role), self.kind)
        channel: discord.abc.GuildChannel
        if self.kind == "category":
            channel = await ctx.guild.create_category(self.name, overwrites=overwrites, reason=ctx.reason)
        elif self.kind == "voice":
            channel = await ctx.guild.create_voice_channel(
                self.name, category=category, overwrites=overwrites, reason=ctx.reason
            )
        else:
            channel = await ctx.guild.create_text_channel(
                self.name, category=category, overwrites=overwrites, topic=self.topic or "", reason=ctx.reason
            )
        ctx.channels[self.name.casefold()] = channel
        return f"Created {channel.mention}"


@dataclass(frozen=True, slots=True)
class AllowRole:
    """Let a role see and use an existing (or newly created) channel. Only turns permissions on."""

    channel: Ref
    role: Ref

    def describe(self, guild: discord.Guild) -> str:
        """One line for the plan."""
        return f"Let **{_role_label(guild, self.role)}** see and use **{_channel_label(guild, self.channel)}**"

    def blocker(self, approver: discord.Member) -> str | None:
        """Why ``approver`` may not approve this step, or None."""
        return _missing(approver, "manage_roles")

    async def run(self, ctx: Execution) -> str:
        """Add the access permissions to the role's overwrite on the channel."""
        channel = ctx.channel(self.channel)
        role = ctx.role(self.role)
        overwrite = _access_overwrite(channel.overwrites_for(role), channel_kind(channel))
        await channel.set_permissions(role, overwrite=overwrite, reason=ctx.reason)
        return f"{role.mention} can now use {channel.mention}"


@dataclass(frozen=True, slots=True)
class GiveRole:
    """Give a member a role the bot is allowed to hand out."""

    member_id: int
    role: Ref

    def describe(self, guild: discord.Guild) -> str:
        """One line for the plan."""
        member = guild.get_member(self.member_id)
        who = f"{member.mention}" if member else f"<@{self.member_id}>"
        return f"Give **{_role_label(guild, self.role)}** to {who}"

    def blocker(self, approver: discord.Member) -> str | None:
        """Why ``approver`` may not approve this step, or None."""
        if isinstance(self.role, Pending):
            return _missing(approver, "manage_roles")
        role = approver.guild.get_role(self.role)
        if role is None:
            return "that role no longer exists"
        return configure_block_reason(approver, role)

    async def run(self, ctx: Execution) -> str:
        """Re-check the role is safe to hand out, then add it."""
        role = ctx.role(self.role)
        if reason := grant_block_reason(role):
            raise StepError(reason)
        member = ctx.guild.get_member(self.member_id)
        if member is None:
            msg = "that member has left the server"
            raise StepError(msg)
        await member.add_roles(role, reason=ctx.reason)
        return f"Gave {role.mention} to {member.mention}"


@dataclass(frozen=True, slots=True)
class PostMessage:
    """Post a message as the bot. Mentions never ping."""

    channel: Ref
    text: str

    def describe(self, guild: discord.Guild) -> str:
        """One line for the plan."""
        return f"Post in **{_channel_label(guild, self.channel)}**: {truncate(self.text, 300)}"

    def blocker(self, approver: discord.Member) -> str | None:
        """Why ``approver`` may not approve this step, or None."""
        if isinstance(self.channel, Pending):
            return None
        channel = approver.guild.get_channel(self.channel)
        if channel is None:
            return "that channel no longer exists"
        return None if channel.permissions_for(approver).send_messages else f"can't post in {channel.name}"

    async def run(self, ctx: Execution) -> str:
        """Send the message."""
        channel = ctx.channel(self.channel)
        if not isinstance(channel, discord.TextChannel | discord.VoiceChannel | discord.Thread):
            msg = f"{channel.name} doesn't take messages"
            raise StepError(msg)
        sent = await channel.send(self.text, allowed_mentions=discord.AllowedMentions.none())
        return f"Posted {sent.jump_url}"


type Step = CreateRole | CreateChannel | AllowRole | GiveRole | PostMessage


@dataclass(slots=True)
class Plan:
    """Changes the model proposed, in order."""

    steps: list[Step] = field(default_factory=list)

    def created_role(self, name: str) -> CreateRole | None:
        """The earlier step that creates a role called ``name``, if any."""
        return next((s for s in self.steps if isinstance(s, CreateRole) and s.name.casefold() == name.casefold()), None)

    def created_channel(self, name: str) -> CreateChannel | None:
        """The earlier step that creates a channel called ``name``, if any."""
        return next(
            (s for s in self.steps if isinstance(s, CreateChannel) and s.name.casefold() == name.casefold()), None
        )

    def blockers(self, approver: discord.Member) -> list[str]:
        """Why ``approver`` may not approve this plan, one line per blocked step; empty if they may."""
        return [
            f"Step {number}: {reason}"
            for number, step in enumerate(self.steps, start=1)
            if (reason := step.blocker(approver))
        ]


async def execute(plan: Plan, guild: discord.Guild, reason: str) -> list[tuple[bool | None, str]]:
    """Carry out ``plan`` in order. After the first failure the rest are skipped, since later steps may need it.

    Returns ``(True, result)`` for a done step, ``(False, why)`` for the failed one and ``(None, …)`` for skipped ones.
    """
    ctx = Execution(guild, reason)
    results: list[tuple[bool | None, str]] = []
    failed = False
    for step in plan.steps:
        summary = step.describe(guild)
        if failed:
            results.append((None, f"Skipped: {summary}"))
            continue
        try:
            results.append((True, await step.run(ctx)))
        except StepError as exc:
            failed = True
            results.append((False, f"{summary}: {exc}"))
        except discord.Forbidden:
            failed = True
            results.append((False, f"{summary}: I'm missing a permission. Check my role's position and permissions."))
        except discord.HTTPException as exc:
            failed = True
            results.append((False, f"{summary}: Discord said {exc.text or exc.status}"))
    return results


# --- What the model may call ---------------------------------------------------------------------------------------

_STRING = {"type": "string"}
_ROLE_REF = {"type": "string", "description": "A role's exact name, id, or <@&id> mention."}
_CHANNEL_REF = {"type": "string", "description": "A channel or category's exact name, id, or <#id> mention."}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SPECS: Final = [
    _tool("list_channels", "Every channel and category, with ids, and which roles can or can't see each.", {}, []),
    _tool("list_roles", "Every role from highest to lowest, with ids and member counts.", {}, []),
    _tool(
        "find_member",
        "Find members whose username or display name contains the query.",
        {"query": _STRING},
        ["query"],
    ),
    _tool(
        "create_role",
        "Propose a new role. It gets no permissions and goes at the bottom of the role list.",
        {
            "name": _STRING,
            "colour": {"type": "string", "description": "Optional hex colour such as #3498db."},
            "hoist": {"type": "boolean", "description": "Show its members separately in the member list."},
            "mentionable": {"type": "boolean", "description": "Let anyone @mention the role."},
        },
        ["name"],
    ),
    _tool(
        "create_channel",
        "Propose a new text channel, voice channel or category. Without category or copy_access_from it is open "
        "to everyone.",
        {
            "name": _STRING,
            "kind": {"type": "string", "enum": list(CHANNEL_KINDS)},
            "category": {**_CHANNEL_REF, "description": "Category to put it in; its access is copied by default."},
            "copy_access_from": {**_CHANNEL_REF, "description": "Channel or category whose access to copy."},
            "allow_roles": {"type": "array", "items": _ROLE_REF, "description": "Extra roles that may use it."},
            "topic": {"type": "string", "description": "Text channels only."},
        },
        ["name", "kind"],
    ),
    _tool(
        "allow_role_in_channel",
        "Propose letting a role see and use a channel or category. Only adds access, never removes it.",
        {"channel": _CHANNEL_REF, "role": _ROLE_REF},
        ["channel", "role"],
    ),
    _tool(
        "give_role",
        "Propose giving a member a role.",
        {
            "member": {"type": "string", "description": "A member's id, <@id> mention, or exact name."},
            "role": _ROLE_REF,
        },
        ["member", "role"],
    ),
    _tool(
        "post_message",
        "Propose posting a message as the bot. Mentions in it won't ping anyone.",
        {"channel": _CHANNEL_REF, "text": _STRING},
        ["channel", "text"],
    ),
]

_MENTION_ID = re.compile(r"^<(?:@&|@!?|#)(\d+)>$")
_COLOUR = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _ref_id(ref: str) -> int | None:
    if match := _MENTION_ID.match(ref):
        return int(match[1])
    return int(ref) if ref.isdigit() else None


def _text(args: dict[str, Any], key: str, *, limit: int = MAX_NAME_LENGTH) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"{key} is required"
        raise PlanError(msg)
    if len(value) > limit:
        msg = f"{key} is longer than {limit} characters"
        raise PlanError(msg)
    return value.strip()


def _optional_text(args: dict[str, Any], key: str, *, limit: int = MAX_NAME_LENGTH) -> str | None:
    return _text(args, key, limit=limit) if args.get(key) not in (None, "") else None


def _flag(args: dict[str, Any], key: str) -> bool:
    return args.get(key) is True


def _role_names(roles: list[discord.Role]) -> str:
    return ", ".join("@everyone" if role.is_default() else f"@{role.name}" for role in roles)


def describe_access(channel: discord.abc.GuildChannel) -> str:
    """Who can and can't see ``channel``, from its role overwrites."""
    if channel.category is not None and channel.permissions_synced:
        return "same access as its category"
    visible: list[discord.Role] = []
    hidden: list[discord.Role] = []
    for target, overwrite in channel.overwrites.items():
        if not isinstance(target, discord.Role):
            continue
        if overwrite.view_channel is True:
            visible.append(target)
        elif overwrite.view_channel is False:
            hidden.append(target)
    parts = []
    if hidden:
        parts.append(f"hidden from {_role_names(hidden)}")
    if visible:
        parts.append(f"visible to {_role_names(visible)}")
    return "; ".join(parts) or "no access rules of its own"


class Toolbox:
    """Answers the model's tool calls against one guild, adding proposed changes to ``plan``."""

    def __init__(self, guild: discord.Guild, plan: Plan) -> None:
        self.guild = guild
        self.plan = plan

    def call(self, call: ToolCall) -> str:
        """Run one tool call; a mistake comes back as ``error: …`` text for the model to fix."""
        handler = getattr(self, f"tool_{call.name}", None)
        if handler is None:
            return f"error: there is no tool called {call.name}"
        if call.arguments is None:
            return "error: the arguments weren't valid JSON"
        try:
            return handler(call.arguments)
        except PlanError as exc:
            return f"error: {exc}"

    def _propose(self, step: Step) -> str:
        if len(self.plan.steps) >= MAX_STEPS:
            msg = f"a plan can have at most {MAX_STEPS} steps"
            raise PlanError(msg)
        self.plan.steps.append(step)
        return f"Added step {len(self.plan.steps)} to the plan: {step.describe(self.guild)}"

    def role_ref(self, ref: str) -> Ref:
        """Resolve a role the model named: an existing role's id, or a role an earlier step creates."""
        ref = ref.strip()
        if (role_id := _ref_id(ref)) is not None:
            if self.guild.get_role(role_id) is None:
                msg = f"there is no role with id {role_id}"
                raise PlanError(msg)
            return role_id
        name = ref.removeprefix("@")
        if name.casefold() == "everyone":
            return self.guild.default_role.id
        matches = [role for role in self.guild.roles if role.name.casefold() == name.casefold()]
        if len(matches) > 1:
            msg = f"{len(matches)} roles are called {name}; use the id ({', '.join(str(r.id) for r in matches)})"
            raise PlanError(msg)
        if matches:
            return matches[0].id
        if step := self.plan.created_role(name):
            return Pending(step.name)
        msg = f"there is no role called {name}; call list_roles, or create_role first"
        raise PlanError(msg)

    def channel_ref(self, ref: str) -> tuple[Ref, ChannelKind]:
        """Resolve a channel the model named, with its kind."""
        ref = ref.strip()
        if (channel_id := _ref_id(ref)) is not None:
            channel = self.guild.get_channel(channel_id)
            if channel is None:
                msg = f"there is no channel with id {channel_id}"
                raise PlanError(msg)
            return channel_id, channel_kind(channel)
        name = ref.removeprefix("#")
        matches = [channel for channel in self.guild.channels if channel.name.casefold() == name.casefold()]
        if len(matches) > 1:
            msg = f"{len(matches)} channels are called {name}; use the id ({', '.join(str(c.id) for c in matches)})"
            raise PlanError(msg)
        if matches:
            return matches[0].id, channel_kind(matches[0])
        if step := self.plan.created_channel(name):
            return Pending(step.name), step.kind
        msg = f"there is no channel called {name}; call list_channels, or create_channel first"
        raise PlanError(msg)

    def member_id(self, ref: str) -> int:
        """Resolve a member the model named to their id."""
        ref = ref.strip()
        if (member_id := _ref_id(ref)) is not None:
            if self.guild.get_member(member_id) is None:
                msg = f"there is no member with id {member_id}"
                raise PlanError(msg)
            return member_id
        name = ref.removeprefix("@").casefold()
        matches = [
            member
            for member in self.guild.members
            if name in {member.name.casefold(), member.display_name.casefold(), (member.global_name or "").casefold()}
        ]
        if len(matches) != 1:
            msg = f"{len(matches)} members are called {ref}; call find_member and use an id"
            raise PlanError(msg)
        return matches[0].id

    def _not_everyone(self, ref: Ref) -> Ref:
        if ref == self.guild.default_role.id:
            msg = "opening a channel to @everyone isn't something I can propose"
            raise PlanError(msg)
        return ref

    # Read tools.

    def tool_list_channels(self, _args: dict[str, Any]) -> str:
        """Channels grouped by category, with ids and access."""
        lines: list[str] = []
        for category, channels in self.guild.by_category():
            if category is None:
                lines.append("Not in a category:")
            else:
                lines.append(f"category {category.name} (id {category.id}): {describe_access(category)}")
            lines.extend(
                f"  {channel_kind(channel)} {channel.name} (id {channel.id}): {describe_access(channel)}"
                for channel in channels
            )
        return "\n".join(lines) or "The server has no channels."

    def tool_list_roles(self, _args: dict[str, Any]) -> str:
        """Roles, highest first."""
        lines = []
        for role in reversed(self.guild.roles):
            line = (
                f"{'@everyone' if role.is_default() else '@' + role.name} (id {role.id}, {len(role.members)} members)"
            )
            if not role.is_default() and (reason := grant_block_reason(role)):
                line += f"; I can't give it out: {reason}"
            lines.append(line)
        return "\n".join(lines)

    def tool_find_member(self, args: dict[str, Any]) -> str:
        """Members matching a name fragment."""
        query = _text(args, "query").removeprefix("@").casefold()
        found = [
            member
            for member in self.guild.members
            if any(query in (name or "").casefold() for name in (member.name, member.display_name, member.global_name))
        ][:MAX_FOUND_MEMBERS]
        if not found:
            return f"No member's name contains {query}."
        return "\n".join(
            f"{member.display_name} (@{member.name}, id {member.id}); roles: {_role_names(member.roles[1:]) or 'none'}"
            for member in found
        )

    # Proposal tools.

    def tool_create_role(self, args: dict[str, Any]) -> str:
        """Propose a role with no permissions."""
        name = _text(args, "name").removeprefix("@")
        if any(role.name.casefold() == name.casefold() for role in self.guild.roles) or self.plan.created_role(name):
            msg = f"a role called {name} already exists"
            raise PlanError(msg)
        colour = None
        if raw := _optional_text(args, "colour"):
            if not (match := _COLOUR.match(raw)):
                msg = "colour must look like #3498db"
                raise PlanError(msg)
            colour = int(match[1], 16)
        return self._propose(
            CreateRole(name, colour=colour, hoist=_flag(args, "hoist"), mentionable=_flag(args, "mentionable"))
        )

    def tool_create_channel(self, args: dict[str, Any]) -> str:
        """Propose a channel or category."""
        name = _text(args, "name").removeprefix("#")
        kind = args.get("kind")
        if kind not in CHANNEL_KINDS:
            msg = f"kind must be one of {', '.join(CHANNEL_KINDS)}"
            raise PlanError(msg)
        category = None
        if raw := _optional_text(args, "category"):
            if kind == "category":
                msg = "a category can't go inside another category"
                raise PlanError(msg)
            category, category_kind = self.channel_ref(raw)
            if category_kind != "category":
                msg = f"{raw} isn't a category"
                raise PlanError(msg)
        copy_from = self.channel_ref(raw)[0] if (raw := _optional_text(args, "copy_access_from")) else None
        raw_roles = args.get("allow_roles") or []
        if not isinstance(raw_roles, list) or not all(isinstance(ref, str) for ref in raw_roles):
            msg = "allow_roles must be a list of role names or ids"
            raise PlanError(msg)
        allow_roles = tuple(self._not_everyone(self.role_ref(ref)) for ref in raw_roles)
        if self.plan.created_channel(name) or any(
            channel.name.casefold() == name.casefold() and channel_kind(channel) == kind
            for channel in self.guild.channels
        ):
            msg = f"a {kind} called {name} already exists"
            raise PlanError(msg)
        topic = _optional_text(args, "topic", limit=MAX_TOPIC_LENGTH) if kind == "text" else None
        return self._propose(CreateChannel(name, kind, category, copy_from, allow_roles, topic))

    def tool_allow_role_in_channel(self, args: dict[str, Any]) -> str:
        """Propose letting a role use a channel."""
        channel, _kind = self.channel_ref(_text(args, "channel"))
        role = self._not_everyone(self.role_ref(_text(args, "role")))
        return self._propose(AllowRole(channel, role))

    def tool_give_role(self, args: dict[str, Any]) -> str:
        """Propose giving a member a role."""
        member_id = self.member_id(_text(args, "member"))
        role = self.role_ref(_text(args, "role"))
        if isinstance(role, int):
            existing = self.guild.get_role(role)
            if existing is not None and (reason := grant_block_reason(existing)):
                raise PlanError(reason)
            member = self.guild.get_member(member_id)
            if member is not None and member.get_role(role) is not None:
                msg = f"{member.display_name} already has that role"
                raise PlanError(msg)
        return self._propose(GiveRole(member_id, role))

    def tool_post_message(self, args: dict[str, Any]) -> str:
        """Propose a message from the bot."""
        channel, kind = self.channel_ref(_text(args, "channel"))
        if kind == "category":
            msg = "can't post in a category; pick a channel in it"
            raise PlanError(msg)
        return self._propose(PostMessage(channel, _text(args, "text", limit=MAX_MESSAGE_LENGTH)))


@dataclass(slots=True)
class PlanResult:
    """What one request produced: the model's reply, its plan, what it cost, and every tool call with its result."""

    text: str
    plan: Plan
    usage: Usage = field(default_factory=Usage)
    rounds: int = 0
    calls: list[tuple[ToolCall, str]] = field(default_factory=list)


async def plan_request(llm: ChatClient, guild: discord.Guild, prompt: str) -> PlanResult:
    """Let the model look around and propose changes for ``prompt``."""
    result = PlanResult(text=OUT_OF_ROUNDS, plan=Plan())
    toolbox = Toolbox(guild, result.plan)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    for _ in range(MAX_ROUNDS):
        reply = await llm.complete(messages, TOOL_SPECS)
        result.rounds += 1
        result.usage += reply.usage
        messages.append(reply.message)
        if not reply.tool_calls:
            result.text = reply.text
            return result
        for call in reply.tool_calls:
            output = toolbox.call(call)
            result.calls.append((call, output))
            messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
    return result


# --- Discord side --------------------------------------------------------------------------------------------------


async def _replied_to(message: discord.Message) -> discord.Message | None:
    reference = message.reference
    if reference is None or reference.message_id is None:
        return None
    if isinstance(reference.resolved, discord.Message):
        return reference.resolved
    try:
        return await message.channel.fetch_message(reference.message_id)
    except discord.HTTPException:
        return None


async def build_prompt(message: discord.Message, bot_id: int) -> str:
    """The request as the model sees it: who asked, where, what, and the message they replied to."""
    text = re.sub(rf"<@!?{bot_id}>", "", message.content).strip()
    channel_name = getattr(message.channel, "name", "a thread")
    lines = [
        f"{message.author.display_name} (id {message.author.id}) wrote in #{channel_name}:",
        text or "(just the mention, no text)",
    ]
    if (replied := await _replied_to(message)) is not None:
        lines += ["", f"They were replying to {replied.author.display_name} (id {replied.author.id}), who wrote:"]
        lines.append(replied.content or "(no text)")
        lines.extend(embed.description for embed in replied.embeds if embed.description)
    return "\n".join(lines)


def plan_embed(plan: Plan, guild: discord.Guild, requester: discord.Member) -> discord.Embed:
    """The proposed plan, waiting for approval."""
    steps = "\n".join(f"{number}. {step.describe(guild)}" for number, step in enumerate(plan.steps, start=1))
    embed = make_embed(f"Plan for {requester.display_name}", truncate(steps, 4096), color=Colors.GOLD)
    embed.set_footer(text="Nothing changes until a Quest Master approves. Expires in 15 minutes.")
    return embed


def results_embed(
    results: list[tuple[bool | None, str]], requester: discord.Member, approver: discord.Member
) -> discord.Embed:
    """What happened when the plan ran."""
    marks = {True: "✅", False: "❌", None: "⏭️"}
    lines = "\n".join(f"{marks[ok]} {text}" for ok, text in results)
    ok = all(done for done, _ in results)
    embed = make_embed(
        "Plan done" if ok else "Plan stopped", truncate(lines, 4096), color=Colors.GREEN if ok else Colors.ORANGE
    )
    embed.add_field(name="Asked by", value=user_line(requester), inline=False)
    embed.add_field(name="Approved by", value=user_line(approver), inline=False)
    return embed


class PlanView(discord.ui.View):
    """Approve and Cancel buttons under a plan. Not persistent: a restart or 15 minutes ends it."""

    def __init__(self, plan: Plan, *, requester: discord.Member, gm_role_ids: list[int]) -> None:
        super().__init__(timeout=PLAN_TIMEOUT_SECONDS)
        self.plan = plan
        self.requester = requester
        self.gm_role_ids = gm_role_ids
        self.message: discord.Message | None = None
        self._handled = False

    def _is_gm(self, member: discord.Member) -> bool:
        return clicker_is_gm({role.id for role in member.roles}, self.gm_role_ids)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction[SideQuestBot], _button: discord.ui.Button[Any]) -> None:
        """Check the clicker may approve every step, then carry the plan out and show what happened."""
        approver = interaction.user
        guild = interaction.guild
        if guild is None or not isinstance(approver, discord.Member):
            return
        if not self._is_gm(approver):
            await interaction.response.send_message("Only Quest Masters can approve.", ephemeral=True)
            return
        if blockers := self.plan.blockers(approver):
            await interaction.response.send_message("You can't approve this:\n" + "\n".join(blockers), ephemeral=True)
            return
        if self._handled:
            await interaction.response.send_message("Someone already handled this plan.", ephemeral=True)
            return
        self._handled = True
        self.stop()
        working = plan_embed(self.plan, guild, self.requester)
        working.set_footer(text=f"Approved by {approver.display_name}; working on it…")
        await interaction.response.edit_message(embed=working, view=None)
        reason = truncate(
            f"AI helper: asked by {self.requester} ({self.requester.id}), approved by {approver} ({approver.id})", 512
        )
        results = await execute(self.plan, guild, reason)
        embed = results_embed(results, self.requester, approver)
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:
            logger.warning("Couldn't show the results of an approved plan", exc_info=True)
        await interaction.client.send_log(guild, embed.copy())

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction[SideQuestBot], _button: discord.ui.Button[Any]) -> None:
        """Drop the plan. The person who asked or any Quest Master may."""
        member = interaction.user
        if not isinstance(member, discord.Member) or (member.id != self.requester.id and not self._is_gm(member)):
            await interaction.response.send_message("Only Quest Masters can cancel this.", ephemeral=True)
            return
        if self._handled:
            await interaction.response.send_message("Someone already handled this plan.", ephemeral=True)
            return
        self._handled = True
        self.stop()
        embed = make_embed("Plan cancelled", f"Cancelled by {member.mention}.", color=Colors.SLATE)
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self) -> None:
        """Take the buttons off so nobody approves a stale plan."""
        if self.message is None or self._handled:
            return
        self._handled = True
        embed = make_embed("Plan expired", "Nobody approved it within 15 minutes. Ask again.", color=Colors.SLATE)
        try:
            await self.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            logger.info("Couldn't mark an expired plan", exc_info=True)


class AssistantCog(commands.Cog):
    """Answers Quest Masters who mention the bot, one request per person at a time."""

    def __init__(self, bot: SideQuestBot, *, llm: ChatClient, gm_role_ids: list[int]) -> None:
        self.bot = bot
        self.llm = llm
        self.gm_role_ids = gm_role_ids
        self._busy: set[int] = set()

    async def cog_unload(self) -> None:
        """Close the model API client."""
        await self.llm.aclose()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Plan a Quest Master's request when they mention the bot."""
        me = self.bot.user
        author = message.author
        guild = message.guild
        if me is None or guild is None or author.bot or not isinstance(author, discord.Member):
            return
        if all(user.id != me.id for user in message.mentions):
            return
        if not clicker_is_gm({role.id for role in author.roles}, self.gm_role_ids):
            return
        if author.id in self._busy:
            await self._reply(message, BUSY)
            return
        self._busy.add(author.id)
        try:
            async with message.channel.typing():
                prompt = await build_prompt(message, me.id)
                result = await plan_request(self.llm, guild, prompt)
        except LLMError:
            logger.warning("AI helper request failed", exc_info=True)
            await self._reply(message, LLM_DOWN)
            return
        finally:
            self._busy.discard(author.id)
        usage = result.usage
        logger.info(
            "AI request by %s: %d rounds, %d steps, %d in / %d out tokens, $%s",
            author.id,
            result.rounds,
            len(result.plan.steps),
            usage.input_tokens,
            usage.output_tokens,
            f"{usage.cost:.5f}" if usage.cost is not None else "?",
        )
        text, plan = result.text, result.plan
        if not plan.steps:
            await self._reply(message, text or NO_ANSWER)
            return
        view = PlanView(plan, requester=author, gm_role_ids=self.gm_role_ids)
        view.message = await self._reply(
            message, text or "Here's my plan.", embed=plan_embed(plan, guild, author), view=view
        )

    async def _reply(
        self,
        message: discord.Message,
        text: str,
        *,
        embed: discord.Embed = discord.utils.MISSING,
        view: discord.ui.View = discord.utils.MISSING,
    ) -> discord.Message | None:
        try:
            return await message.reply(
                truncate(text, MAX_MESSAGE_LENGTH),
                embed=embed,
                view=view,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logger.warning("Couldn't answer a request in channel %s", message.channel.id, exc_info=True)
            return None


async def setup(bot: SideQuestBot) -> None:
    """Register the cog when ``AI_API_KEY`` is set."""
    settings = AssistantSettings()
    if settings.API_KEY is None:
        logger.info("AI helper is off; set AI_API_KEY to turn it on")
        return
    gm_role_ids = WebSettings().GM_ROLE_IDS
    llm = ChatClient(
        settings.BASE_URL,
        settings.API_KEY.get_secret_value(),
        settings.MODEL,
        reasoning=settings.REASONING or None,
    )
    await bot.add_cog(AssistantCog(bot, llm=llm, gm_role_ids=gm_role_ids))
    logger.info("AI helper is on, using %s with %s reasoning", settings.MODEL, settings.REASONING or "default")
