"""Slash commands: /leaderboard, /activity, /rank, /rep give,
/rep show, /rep_top, plus passive tracking.

Passive tracking is NOT a command: on_message (non-bot, in-guild
only) increments a per-day message count, and on_voice_state_update
accumulates voice minutes when a user leaves, disconnects, or moves
channels. No channel history is ever read, so counts start from
when the bot first runs — see README.

/leaderboard windows: '30d' (default, last 30 days, unchanged
behaviour), 'week' (last 7 days), 'month' (since the 1st of the
current UTC month). /rank reports the invoking user's own rank
and percentile separately for text and voice over the last
30 days, using the same local data; a user with no activity in
a kind is told so instead of being ranked for that kind.

Reputation: /rep give awards 1 point to another member (never
yourself, never a bot) with a 24-hour per-giver cooldown — one
rep gift per giver per day, to anyone. /rep show shows a
member's total. /rep_top lists the top 10. The optional reason
on /rep give is shown in the confirmation only; it is not stored.

User-triggered actions here are slash commands only. There is no
prefix command and no chat-triggered command in this bot.
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import db
import ui
from helpers import format_voice_minutes, guild_only

# (guild_id, user_id) -> session start (aware UTC datetime).
_voice_sessions: dict[tuple[int, int], datetime] = {}


def _flush_session(guild_id: int, user_id: int, end: datetime) -> None:
    """Move one open voice session's minutes into the DB, then close it."""
    start = _voice_sessions.pop((guild_id, user_id), None)
    if start is None:
        return
    minutes = (end - start).total_seconds() / 60.0
    db.record_voice_minutes(guild_id, user_id, minutes)


def flush_all_sessions(end: datetime | None = None) -> None:
    """Best-effort flush of every open session (used on shutdown)."""
    at = end or datetime.now(timezone.utc)
    for guild_id, user_id in list(_voice_sessions):
        _flush_session(guild_id, user_id, at)


def _format_cooldown(seconds: int) -> str:
    hours, rem = divmod(int(seconds), 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class Activity(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -- passive listeners (not commands) -------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        try:
            db.record_message(message.guild.id, message.author.id)
        except Exception as e:
            print(f"[activity] record_message failed: {e}")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        if member.bot:
            return
        if before.channel == after.channel:
            return  # mute/deafen/stream-only change: ignore
        now = datetime.now(timezone.utc)
        try:
            if before.channel is not None:
                # Leave, disconnect, or move: close the old session.
                _flush_session(member.guild.id, member.id, now)
            if after.channel is not None:
                # Join or move: open a new session.
                _voice_sessions[(member.guild.id, member.id)] = now
        except Exception as e:
            print(f"[activity] voice tracking failed: {e}")

    def cog_unload(self) -> None:
        flush_all_sessions()

    # -- slash commands --------------------------------------------------

    @app_commands.command(name="leaderboard", description="Top 10 members by activity.")
    @app_commands.describe(
        kind="Text = messages sent, Voice = minutes in voice channels.",
        window="30d = last 30 days (default), week = last 7 days, month = this calendar month (UTC).")
    @app_commands.choices(kind=[
        app_commands.Choice(name="text", value="text"),
        app_commands.Choice(name="voice", value="voice"),
    ])
    @app_commands.choices(window=[
        app_commands.Choice(name="30d", value="30d"),
        app_commands.Choice(name="week", value="week"),
        app_commands.Choice(name="month", value="month"),
    ])
    async def leaderboard(self, interaction: discord.Interaction,
                          kind: app_commands.Choice[str] | None = None,
                          window: app_commands.Choice[str] | None = None) -> None:
        member = await guild_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        chosen = kind.value if kind is not None else "text"
        chosen_window = window.value if window is not None else "30d"
        try:
            rows = db.leaderboard(guild.id, chosen, limit=10, window=chosen_window)
        except Exception:
            await interaction.response.send_message(
                embed=ui.err("❌ Could not load leaderboard",
                             "Could not load the leaderboard right now. "
                             "Please try again later."),
                ephemeral=True)
            return
        label_window = db.window_label(chosen_window)
        if not rows:
            await interaction.response.send_message(
                embed=ui.info(
                    "ℹ️ No activity yet",
                    f"No {chosen} activity recorded in the {label_window} yet. "
                    "Counts start from when this bot first runs, "
                    "there is no backfill from channel history."),
                ephemeral=True)
            return
        lines = []
        for rank, row in enumerate(rows, start=1):
            user = guild.get_member(row["user_id"])
            name = user.mention if user is not None else f"User ID {row['user_id']}"
            if chosen == "text":
                value = f"{int(row['total'])} messages"
            else:
                value = format_voice_minutes(float(row["total"]))
            lines.append(f"{rank}. {name} - {value}")
        label = "Messages" if chosen == "text" else "Voice time"
        embed = ui.make(f"🏆 Leaderboard: {label} ({label_window})",
                        "\n".join(lines), colour=ui.GOLD,
                        footer="Tracked locally since this bot started; "
                               "no history backfill")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="activity", description="Show a member's message count and voice minutes in the last 30 days.")
    @app_commands.describe(member="The member to look up.")
    async def activity(self, interaction: discord.Interaction,
                       member: discord.Member) -> None:
        if await guild_only(interaction) is None:
            return
        guild = interaction.guild
        assert guild is not None
        data = db.user_activity(guild.id, member.id)
        embed = ui.make(
            f"📈 Activity: {member.display_name}", None, colour=ui.BLURPLE,
            fields=[
                ("Messages (last 30 days)", str(data["messages"]), True),
                ("Voice time (last 30 days)",
                 format_voice_minutes(data["voice_minutes"]), True),
            ],
            footer="Tracked locally since this bot started; "
                   "no history backfill")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="rank", description="Show your own text and voice rank and percentile in the last 30 days.")
    async def rank(self, interaction: discord.Interaction) -> None:
        member = await guild_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        try:
            text_rank = db.user_rank(guild.id, member.id, "text")
            voice_rank = db.user_rank(guild.id, member.id, "voice")
        except Exception:
            await interaction.response.send_message(
                embed=ui.err("❌ Could not load rank",
                             "Could not load your rank right now. "
                             "Please try again later."),
                ephemeral=True)
            return
        if text_rank is None and voice_rank is None:
            await interaction.response.send_message(
                embed=ui.info(
                    "ℹ️ No rank yet",
                    "You have no recorded activity in the last 30 days yet, "
                    "so you do not have a rank. Counts start from when this "
                    "bot first runs, there is no backfill from channel history."),
                ephemeral=True)
            return
        fields: list[tuple[str, str, bool]] = []
        if text_rank is None:
            fields.append(("Text rank",
                           "No text activity in the last 30 days.", True))
        else:
            fields.append((
                "Text rank",
                f"#{text_rank['rank']} of {text_rank['tracked']} "
                f"({int(text_rank['total'])} messages, "
                f"{text_rank['percentile']:.0f}th percentile)", True))
        if voice_rank is None:
            fields.append(("Voice rank",
                           "No voice activity in the last 30 days.", True))
        else:
            fields.append((
                "Voice rank",
                f"#{voice_rank['rank']} of {voice_rank['tracked']} "
                f"({format_voice_minutes(float(voice_rank['total']))}, "
                f"{voice_rank['percentile']:.0f}th percentile)", True))
        embed = ui.make(f"🎖️ Your rank: {member.display_name}", None,
                        colour=ui.BLURPLE, fields=fields,
                        footer="Percentile = share of tracked members ranked "
                               "below you; rank 1 is the top member. "
                               "Last 30 days, local data only.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="rep_top", description="Top 10 members by reputation points.")
    async def rep_top(self, interaction: discord.Interaction) -> None:
        member = await guild_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        try:
            rows = db.rep_top(guild.id, limit=10)
        except Exception:
            await interaction.response.send_message(
                embed=ui.err("❌ Could not load reputation",
                             "Could not load the reputation leaderboard "
                             "right now."),
                ephemeral=True)
            return
        if not rows:
            await interaction.response.send_message(
                embed=ui.info(
                    "ℹ️ No reputation yet",
                    "No reputation points have been given yet. "
                    "Use /rep give to award a point to another member."),
                ephemeral=True)
            return
        lines = []
        for rank, row in enumerate(rows, start=1):
            user = guild.get_member(row["user_id"])
            name = user.mention if user is not None else f"User ID {row['user_id']}"
            points = int(row["points"])
            lines.append(f"{rank}. {name} - {points} point{'s' if points != 1 else ''}")
        embed = ui.make("⭐ Reputation leaderboard", "\n".join(lines),
                        colour=ui.GOLD,
                        footer="Give 1 point per day with /rep give. "
                               "No self-rep.")
        await interaction.response.send_message(embed=embed)


class Rep(commands.GroupCog, group_name="rep",
          group_description="Give and view reputation points."):
    """Group: /rep give, /rep show. Counts as one top-level command
    (/rep) with two subcommands."""

    @app_commands.command(name="give", description="Give 1 reputation point to another member (once per day).")
    @app_commands.describe(member="The member to give reputation to.",
                           reason="Optional reason, shown in the confirmation only.")
    async def rep_give(self, interaction: discord.Interaction,
                       member: discord.Member, reason: str | None = None) -> None:
        giver = await guild_only(interaction)
        if giver is None:
            return
        guild = interaction.guild
        assert guild is not None
        if member.bot:
            await interaction.response.send_message(
                embed=ui.err("❌ Cannot give reputation",
                             "You cannot give reputation to a bot."),
                ephemeral=True)
            return
        if member.id == giver.id:
            await interaction.response.send_message(
                embed=ui.err("❌ Cannot give reputation",
                             "You cannot give reputation to yourself."),
                ephemeral=True)
            return
        try:
            status, value = db.give_rep(guild.id, giver.id, member.id)
        except Exception:
            await interaction.response.send_message(
                embed=ui.err("Could not give reputation",
                             "Could not give reputation right now. "
                             "Please try again later."),
                ephemeral=True)
            return
        if status == "self":
            await interaction.response.send_message(
                embed=ui.err("❌ Cannot give reputation",
                             "You cannot give reputation to yourself."),
                ephemeral=True)
            return
        if status == "cooldown":
            await interaction.response.send_message(
                embed=ui.err(
                    "⏳ Reputation cooldown",
                    f"You already gave reputation in the last 24 hours. "
                    f"Try again in {_format_cooldown(value or 0)}. "
                    "Each member can give 1 reputation point per day."),
                ephemeral=True)
            return
        fields: list[tuple[str, str, bool]] = [
            ("Given by", giver.mention, True),
            ("Total points",
             f"{value} point{'s' if value != 1 else ''}", True),
        ]
        if reason:
            fields.append(("Reason", reason[:300], False))
        embed = ui.make(
            f"⭐ Reputation given to {member.display_name}",
            f"{giver.mention} gave 1 reputation point to {member.mention}.",
            colour=ui.GOLD, fields=fields)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="show", description="Show a member's reputation points.")
    @app_commands.describe(member="The member to look up.")
    async def rep_show(self, interaction: discord.Interaction,
                       member: discord.Member) -> None:
        if await guild_only(interaction) is None:
            return
        guild = interaction.guild
        assert guild is not None
        try:
            points = db.rep_points_for(guild.id, member.id)
        except Exception:
            await interaction.response.send_message(
                embed=ui.err("❌ Could not load reputation",
                             "Could not load reputation right now. "
                             "Please try again later."),
                ephemeral=True)
            return
        embed = ui.make(
            f"⭐ Reputation: {member.display_name}",
            f"{member.mention} has {points} reputation "
            f"point{'s' if points != 1 else ''}.",
            colour=ui.GOLD)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Activity(bot))
    await bot.add_cog(Rep(bot))
