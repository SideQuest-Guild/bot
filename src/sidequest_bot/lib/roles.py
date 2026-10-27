"""Rules for roles the bot hands out on its own (level rewards, auto-role, self-serve role menus).

Two independent checks, both required:

* :func:`grant_block_reason` - is the role itself safe for the bot to give away? Checked when the role is configured
  AND again right before every grant, because an admin can add permissions to a role after it was configured.
* :func:`configure_block_reason` - may this member set the role up to be handed out? Without it, anyone with Manage
  Server could configure a role above their own and escalate through the bot's permissions.
"""

import discord

DANGEROUS_PERMISSIONS = discord.Permissions(
    administrator=True,
    ban_members=True,
    kick_members=True,
    manage_channels=True,
    manage_events=True,
    manage_expressions=True,
    manage_guild=True,
    manage_messages=True,
    manage_nicknames=True,
    manage_roles=True,
    manage_threads=True,
    manage_webhooks=True,
    mention_everyone=True,
    moderate_members=True,
    mute_members=True,
    deafen_members=True,
    move_members=True,
    view_audit_log=True,
)
"""Permissions a role must not carry if the bot grants it automatically or on request."""


def dangerous_permissions(permissions: discord.Permissions) -> list[str]:
    """Names of the dangerous permissions set in ``permissions``, sorted."""
    return sorted(name for name, enabled in permissions & DANGEROUS_PERMISSIONS if enabled)


def grant_block_reason(role: discord.Role) -> str | None:
    """Why the bot must not hand out ``role`` automatically, or None if it may."""
    if role.is_default():
        return "@everyone can't be granted."
    if role.managed:
        return f"{role.mention} is managed by an integration and can't be granted."
    if not role.is_assignable():
        return f"{role.mention} is at or above my highest role; move my role above it first."
    if risky := dangerous_permissions(role.permissions):
        return f"{role.mention} has moderator/admin permissions ({', '.join(risky)}), so I won't hand it out."
    return None


def configure_block_reason(member: discord.Member, role: discord.Role) -> str | None:
    """Why ``member`` may not configure ``role`` to be handed out by the bot, or None if they may.

    Requires Manage Roles and that the role sits below the member's highest role (the server owner is exempt).
    """
    if member.id == member.guild.owner_id:
        return None
    if not member.guild_permissions.manage_roles:
        return "You need the Manage Roles permission to set up roles the bot hands out."
    if role >= member.top_role:
        return f"{role.mention} is at or above your highest role."
    return None
