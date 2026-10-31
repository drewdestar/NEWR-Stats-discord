"""Shared helpers for NEWR Stats. No imports from other bots: the
verified/unverified idea is copied (small, by role name) from
newr-support/roles.py, and the staff gate is this bot's own rule.

Staff gate (documented in README): a member is staff for event
commands if they are the guild owner, or hold the Administrator or
Manage Guild permission. No role-name list is used.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import discord

WIB = timezone(timedelta(hours=7))
TIME_FORMAT = "YYYY-MM-DD HH:MM"
TIME_EXAMPLE = "2026-11-05 19:00"


def parse_wib_datetime(text: str) -> datetime:
    """Parse 'YYYY-MM-DD HH:MM' as Asia/Jakarta (WIB, UTC+7).

    Returns an aware UTC datetime. Raises ValueError with a clear
    message + format example for anything unparseable; callers turn
    that into an ephemeral reply, never a crash.
    """
    cleaned = (text or "").strip()
    try:
        naive = datetime.strptime(cleaned, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError(
            f"Invalid date/time: {cleaned!r}. Use the format "
            f"{TIME_FORMAT} in WIB, e.g. {TIME_EXAMPLE}.")
    return naive.replace(tzinfo=WIB).astimezone(timezone.utc)


def format_wib(utc_iso: str) -> str:
    """UTC ISO string -> 'YYYY-MM-DD HH:MM WIB' display string."""
    dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M WIB")


def format_created(dt: datetime) -> str:
    """A guild/member creation datetime -> WIB + UTC display string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(WIB).strftime("%Y-%m-%d %H:%M WIB")
            + " (" + dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + ")")


def is_staff(member: discord.Member) -> bool:
    """Guild owner, Administrator, or Manage Guild permission."""
    if member.guild is not None and member.guild.owner_id == member.id:
        return True
    perms = member.guild_permissions
    return bool(perms.administrator or perms.manage_guild)


def verified_counts(guild: discord.Guild) -> tuple[int | None, int | None]:
    """(verified, unverified) member counts, or (None, None) if the
    guild has no role whose name mentions verification at all.

    Matching is by role NAME, copied from the newr-support idea:
    'unverif' is checked BEFORE 'verif', because 'unverified'
    contains the substring 'verified'. Counts use cached members
    (members intent); a member holding both kinds of role counts
    as unverified only.
    """
    verif_roles = [r for r in guild.roles if "verif" in r.name.lower()]
    if not verif_roles:
        return None, None
    unverif_ids = {r.id for r in guild.roles
                   if "unverif" in r.name.lower()}
    verif_ids = {r.id for r in guild.roles
                 if "verif" in r.name.lower()} - unverif_ids
    verified = unverified = 0
    for m in guild.members:
        role_ids = {r.id for r in m.roles}
        if role_ids & unverif_ids:
            unverified += 1
        elif role_ids & verif_ids:
            verified += 1
    return verified, unverified


def format_voice_minutes(minutes: float) -> str:
    """e.g. 90 -> '1h 30m', 45 -> '45m'."""
    total = int(round(minutes))
    hours, mins = divmod(total, 60)
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


async def guild_only(interaction: discord.Interaction) -> discord.Member | None:
    """Reply ephemerally and return None if used outside a guild."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "This command can only be used inside a server.", ephemeral=True)
        return None
    return interaction.user


async def staff_only(interaction: discord.Interaction) -> discord.Member | None:
    """Guild + staff gate. Replies ephemerally and returns None on failure."""
    member = await guild_only(interaction)
    if member is None:
        return None
    if not is_staff(member):
        await interaction.response.send_message(
            "Staff only: this command requires Administrator or "
            "Manage Guild permission.", ephemeral=True)
        return None
    return member
