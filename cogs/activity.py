"""Slash commands: /leaderboard and /activity, plus passive tracking.

Passive tracking is NOT a command: on_message (non-bot, in-guild
only) increments a per-day message count, and on_voice_state_update
accumulates voice minutes when a user leaves, disconnects, or moves
channels. No channel history is ever read, so counts start from
when the bot first runs — see README.

User-triggered actions here are slash commands only. There is no
prefix command and no chat-triggered command in this bot.
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import db
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

    @app_commands.command(name="leaderboard", description="Top 10 members by activity in the last 30 days.")
    @app_commands.describe(kind="Text = messages sent, Voice = minutes in voice channels.")
    @app_commands.choices(kind=[
        app_commands.Choice(name="text", value="text"),
        app_commands.Choice(name="voice", value="voice"),
    ])
    async def leaderboard(self, interaction: discord.Interaction,
                          kind: app_commands.Choice[str] | None = None) -> None:
        member = await guild_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        chosen = kind.value if kind is not None else "text"
        rows = db.leaderboard(guild.id, chosen, limit=10)
        if not rows:
            await interaction.response.send_message(
                f"No {chosen} activity recorded in the last 30 days yet. "
                "Counts start from when this bot first runs — "
                "there is no backfill from channel history.",
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
            lines.append(f"{rank}. {name} — {value}")
        label = "Messages" if chosen == "text" else "Voice time"
        embed = discord.Embed(title=f"🏆 Leaderboard — {label} (last 30 days)",
                              description="\n".join(lines),
                              colour=discord.Colour.gold())
        embed.set_footer(text="Tracked locally since this bot started; no history backfill.")
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
        embed = discord.Embed(title=f"📈 Activity — {member.display_name}",
                              colour=discord.Colour.blurple())
        embed.add_field(name="Messages (last 30 days)",
                        value=str(data["messages"]))
        embed.add_field(name="Voice time (last 30 days)",
                        value=format_voice_minutes(data["voice_minutes"]))
        embed.set_footer(text="Tracked locally since this bot started; no history backfill.")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Activity(bot))
