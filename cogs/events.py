"""Slash commands: /event_create, /event_list, /event_cancel,
/event_remind, plus a 60-second reminder loop.

Staff gate: guild owner, Administrator, or Manage Guild (see
helpers.is_staff). Reminders are posted in the event's channel
only — the bot never DMs members about events. Each event is
reminded at most once: the loop and /event_remind both mark an
event reminded after posting, so they cannot double-post it.

Times: users type WIB (Asia/Jakarta, UTC+7) as YYYY-MM-DD HH:MM;
times are stored as UTC ISO strings and displayed back in WIB.
Invalid dates get an ephemeral error with the format example,
never a crash.

All user-triggered actions here are slash commands only. There is
no prefix command and no chat-triggered command in this bot.
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from helpers import TIME_EXAMPLE, TIME_FORMAT, format_wib, parse_wib_datetime, staff_only


class Events(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    # -- reminder loop ---------------------------------------------------

    @tasks.loop(seconds=60)
    async def reminder_loop(self) -> None:
        for row in db.due_reminders():
            try:
                channel = self.bot.get_channel(row["channel_id"])
                if not isinstance(channel, discord.TextChannel):
                    # Channel gone or not a text channel: mark reminded
                    # so a dead event does not retry forever.
                    db.mark_reminded(row["id"])
                    continue
                embed = discord.Embed(
                    title=f"⏰ Reminder: {row['name']} starts soon!",
                    description=(row["description"] or "See you there!"),
                    colour=discord.Colour.gold())
                embed.add_field(name="Starts", value=format_wib(row["start_at"]))
                embed.add_field(name="Event ID", value=str(row["id"]))
                await channel.send(embed=embed)
                db.mark_reminded(row["id"])
                print(f"[events] reminded event {row['id']} in #{channel.name}")
            except Exception as e:
                print(f"[events] reminder failed for event {row['id']}: {e}")

    @reminder_loop.before_loop
    async def _before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    # -- slash commands --------------------------------------------------

    @app_commands.command(name="event_create", description="Create a server event (staff only).")
    @app_commands.describe(
        name="Event name.",
        start=f"Start time in WIB, format {TIME_FORMAT}, e.g. {TIME_EXAMPLE}.",
        description="Optional event description.")
    async def event_create(self, interaction: discord.Interaction,
                           name: str, start: str,
                           description: str | None = None) -> None:
        member = await staff_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        try:
            start_utc = parse_wib_datetime(start)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        if start_utc <= datetime.now(timezone.utc):
            await interaction.response.send_message(
                "That time is in the past. Please use a future time in WIB, "
                f"format {TIME_FORMAT}, e.g. {TIME_EXAMPLE}.", ephemeral=True)
            return
        event_id = db.create_event(
            guild.id, interaction.channel_id, name, description or "",
            start_utc.isoformat(), member.id)
        embed = discord.Embed(title=f"✅ Event created: {name}",
                              colour=discord.Colour.green())
        embed.add_field(name="Event ID", value=str(event_id))
        embed.add_field(name="Starts", value=format_wib(start_utc.isoformat()))
        if description:
            embed.add_field(name="Description", value=description[:500],
                            inline=False)
        embed.add_field(name="Channel",
                        value=f"<#{interaction.channel_id}>", inline=False)
        embed.set_footer(text="A reminder will be posted here 1 hour before it starts.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="event_list", description="List upcoming server events.")
    async def event_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True)
            return
        guild = interaction.guild
        rows = db.list_events(guild.id, upcoming_only=True)
        if not rows:
            await interaction.response.send_message(
                "No upcoming events. Staff can create one with /event_create.",
                ephemeral=True)
            return
        lines = []
        for row in rows[:25]:
            desc = f" — {row['description'][:80]}" if row["description"] else ""
            lines.append(f"**#{row['id']} {row['name']}** — "
                         f"{format_wib(row['start_at'])} in <#{row['channel_id']}>{desc}")
        embed = discord.Embed(title="📅 Upcoming events",
                              description="\n".join(lines),
                              colour=discord.Colour.blurple())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="event_cancel", description="Cancel a server event (staff only).")
    @app_commands.describe(event_id="The event ID from /event_list.")
    async def event_cancel(self, interaction: discord.Interaction,
                           event_id: int) -> None:
        member = await staff_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        row = db.get_event(event_id, guild.id)
        if row is None:
            await interaction.response.send_message(
                f"No event with ID {event_id} in this server.", ephemeral=True)
            return
        if row["cancelled"]:
            await interaction.response.send_message(
                f"Event #{event_id} ({row['name']}) is already cancelled.",
                ephemeral=True)
            return
        db.cancel_event(event_id, guild.id)
        await interaction.response.send_message(
            f"✅ Event #{event_id} ({row['name']}) cancelled. "
            "No reminder will be posted for it.")

    @app_commands.command(name="event_remind", description="Post a reminder now for events starting within the next 24h (staff only).")
    async def event_remind(self, interaction: discord.Interaction) -> None:
        member = await staff_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        rows = [r for r in db.upcoming_within(guild.id, 24)
                if not r["reminded"]]
        if not rows:
            await interaction.response.send_message(
                "No upcoming events within the next 24 hours need a reminder.",
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        reminded, failed = [], []
        for row in rows:
            try:
                channel = self.bot.get_channel(row["channel_id"])
                if not isinstance(channel, discord.TextChannel):
                    db.mark_reminded(row["id"])
                    failed.append(f"#{row['id']} (channel unavailable)")
                    continue
                embed = discord.Embed(
                    title=f"⏰ Reminder: {row['name']} starts soon!",
                    description=(row["description"] or "See you there!"),
                    colour=discord.Colour.gold())
                embed.add_field(name="Starts", value=format_wib(row["start_at"]))
                embed.add_field(name="Event ID", value=str(row["id"]))
                await channel.send(embed=embed)
                db.mark_reminded(row["id"])
                reminded.append(f"#{row['id']} {row['name']}")
            except Exception as e:
                failed.append(f"#{row['id']} ({e})")
        summary = f"Reminded {len(reminded)} event(s) in their event channels."
        if reminded:
            summary += "\n" + "\n".join(reminded)
        if failed:
            summary += "\nFailed: " + "; ".join(failed)
        summary += ("\nNote: each event is reminded at most once, so events "
                    "reminded here will not get a second 1-hour reminder.")
        await interaction.followup.send(summary, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
