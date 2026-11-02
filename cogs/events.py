"""Slash commands: /event_create, /event_list, /event_cancel,
/event_remind, /event_rsvp, /event_attendees, /event_edit,
plus a 60-second reminder loop.

Staff gate: guild owner, Administrator, or Manage Guild (see
helpers.is_staff). Reminders are posted in the event's channel
only — the bot never DMs members about events. Each event is
reminded at most once: the loop and /event_remind both mark an
event reminded after posting, so they cannot double-post it.
Reminders mention RSVP attendees in the channel message
(channel mention, not DM, to avoid DM spam).

Times: users type WIB (Asia/Jakarta, UTC+7) as YYYY-MM-DD HH:MM;
times are stored as UTC ISO strings and displayed back in WIB.
Invalid dates get an ephemeral error with the format example,
never a crash.

RSVP: /event_rsvp <event_id> <join|leave> toggles attendance in
SQLite. RSVP to a cancelled or past event is rejected with a
clear ephemeral error. /event_attendees lists participants.

Editing: /event_edit (staff) changes a pending event's name,
start time (WIB), description, and/or channel. Moving the start
time resets the reminded flag so the moved event can be
reminded again; other edits keep it.

Recurring: /event_create repeat=weekly creates the next
occurrence (+7 days, same name/description/channel) once the
current one has passed, via the reminder loop. Cancelled
events never spawn a successor, and each event spawns at most
one successor (next_created flag), so restarts cannot duplicate.

All user-triggered actions here are slash commands only. There is
no prefix command and no chat-triggered command in this bot.
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import ui
from helpers import TIME_EXAMPLE, TIME_FORMAT, format_wib, parse_wib_datetime, staff_only


def _is_past(row) -> bool:
    try:
        start = datetime.fromisoformat(row["start_at"].replace("Z", "+00:00"))
    except ValueError:
        return True
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start <= datetime.now(timezone.utc)


def _attendee_mentions(guild: discord.Guild, event_id: int) -> str:
    """Channel mentions for RSVP attendees, capped at 25 so a
    reminder cannot spam-mention an unbounded list."""
    ids = db.event_attendees(event_id)
    mentions = []
    for uid in ids[:25]:
        member = guild.get_member(uid)
        mentions.append(member.mention if member is not None else f"<@{uid}>")
    extra = len(ids) - len(mentions)
    text = " ".join(mentions)
    if extra > 0:
        text += f" (+{extra} more attending)"
    return text


def _reminder_embed(row) -> discord.Embed:
    return ui.make(
        f"⏰ Reminder: {row['name']} starts soon!",
        row["description"] or "See you there!",
        colour=ui.GOLD,
        fields=[
            ("Starts", format_wib(row["start_at"]), True),
            ("Event ID", str(row["id"]), True),
        ])


def _validate_pending(row, event_id: int) -> str | None:
    """Error message if an event is missing/cancelled/past, else None."""
    if row is None:
        return f"No event with ID {event_id} in this server."
    if row["cancelled"]:
        return f"Event #{event_id} ({row['name']}) is cancelled."
    if _is_past(row):
        return f"Event #{event_id} ({row['name']}) has already passed."
    return None


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
        # Recurring events: spawn the next weekly occurrence once
        # the current one has passed, before handling reminders.
        for row in db.passed_weekly_events():
            try:
                new_id = db.create_next_occurrence(row)
                print(f"[events] weekly event {row['id']} spawned occurrence {new_id}")
            except Exception as e:
                print(f"[events] recurrence failed for event {row['id']}: {e}")
        for row in db.due_reminders():
            try:
                channel = self.bot.get_channel(row["channel_id"])
                if not isinstance(channel, discord.TextChannel):
                    # Channel gone or not a text channel: mark reminded
                    # so a dead event does not retry forever.
                    db.mark_reminded(row["id"])
                    continue
                mentions = _attendee_mentions(channel.guild, row["id"])
                content = f"Attending: {mentions}" if mentions else None
                await channel.send(content=content, embed=_reminder_embed(row))
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
        description="Optional event description.",
        repeat="none = one-off (default). weekly = a new occurrence is created 7 days after this one passes.")
    @app_commands.choices(repeat=[
        app_commands.Choice(name="none", value="none"),
        app_commands.Choice(name="weekly", value="weekly"),
    ])
    async def event_create(self, interaction: discord.Interaction,
                           name: str, start: str,
                           description: str | None = None,
                           repeat: app_commands.Choice[str] | None = None) -> None:
        member = await staff_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        if not name.strip():
            await interaction.response.send_message(
                embed=ui.err("❌ Event name empty",
                             "Event name cannot be empty."),
                ephemeral=True)
            return
        try:
            start_utc = parse_wib_datetime(start)
        except ValueError as e:
            await interaction.response.send_message(
                embed=ui.err("❌ Invalid date/time", str(e)), ephemeral=True)
            return
        if start_utc <= datetime.now(timezone.utc):
            await interaction.response.send_message(
                embed=ui.err(
                    "❌ Time is in the past",
                    "That time is in the past. Please use a future time "
                    f"in WIB, format {TIME_FORMAT}, e.g. {TIME_EXAMPLE}."),
                ephemeral=True)
            return
        repeat_value = repeat.value if repeat is not None else "none"
        event_id = db.create_event(
            guild.id, interaction.channel_id, name.strip(), description or "",
            start_utc.isoformat(), member.id, repeat=repeat_value)
        fields: list[tuple[str, str, bool]] = [
            ("Event ID", str(event_id), True),
            ("Starts", format_wib(start_utc.isoformat()), True),
        ]
        if description:
            fields.append(("Description", description[:500], False))
        fields.append(("Channel", f"<#{interaction.channel_id}>", False))
        if repeat_value == "weekly":
            fields.append(("Repeats",
                           "Weekly - next occurrence is created after this "
                           "one passes.", False))
        embed = ui.make(f"✅ Event created: {name.strip()}", None,
                        colour=ui.SUCCESS, fields=fields,
                        footer="A reminder will be posted here 1 hour "
                               "before it starts.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="event_list", description="List upcoming server events.")
    async def event_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=ui.err("🚫 Server only",
                             "This command can only be used inside a server."),
                ephemeral=True)
            return
        guild = interaction.guild
        rows = db.list_events(guild.id, upcoming_only=True)
        if not rows:
            await interaction.response.send_message(
                embed=ui.info(
                    "ℹ️ No upcoming events",
                    "No upcoming events. Staff can create one with "
                    "/event_create."),
                ephemeral=True)
            return
        lines = []
        for row in rows[:25]:
            desc = f" - {row['description'][:80]}" if row["description"] else ""
            repeat = " 🔁 weekly" if row["repeat"] == "weekly" else ""
            lines.append(f"**#{row['id']} {row['name']}** - "
                         f"{format_wib(row['start_at'])} in <#{row['channel_id']}>{repeat}{desc}")
        embed = ui.make("📅 Upcoming events", "\n".join(lines),
                        colour=ui.BLURPLE)
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
                embed=ui.err("❌ Event not found",
                             f"No event with ID {event_id} in this server."),
                ephemeral=True)
            return
        if row["cancelled"]:
            await interaction.response.send_message(
                embed=ui.info(
                    "ℹ️ Already cancelled",
                    f"Event #{event_id} ({row['name']}) is already cancelled."),
                ephemeral=True)
            return
        db.cancel_event(event_id, guild.id)
        await interaction.response.send_message(
            embed=ui.ok(
                f"✅ Event #{event_id} cancelled",
                f"Event #{event_id} ({row['name']}) cancelled. "
                "No reminder will be posted for it."))

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
                embed=ui.info(
                    "ℹ️ No reminders needed",
                    "No upcoming events within the next 24 hours need "
                    "a reminder."),
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
                mentions = _attendee_mentions(guild, row["id"])
                content = f"Attending: {mentions}" if mentions else None
                await channel.send(content=content, embed=_reminder_embed(row))
                db.mark_reminded(row["id"])
                reminded.append(f"#{row['id']} {row['name']}")
            except Exception as e:
                failed.append(f"#{row['id']} ({e})")
        fields: list[tuple[str, str, bool]] = []
        if reminded:
            fields.append(("Reminded", "\n".join(reminded), False))
        if failed:
            fields.append(("Failed", "; ".join(failed), False))
        embed = ui.ok(
            f"✅ Reminders sent for {len(reminded)} event(s)",
            "Reminders were posted in their event channels.",
            fields=fields if fields else None)
        # Keep the at-most-once / never-DM honesty note as a field
        # so it survives the embed conversion (ui footer is standard).
        embed.add_field(
            name="Note",
            value="Each event is reminded at most once, so events "
                  "reminded here will not get a second 1-hour reminder. "
                  "Attendees were mentioned in the channel reminder "
                  "(never DM'd).",
            inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="event_rsvp", description="Join or leave an event's RSVP list.")
    @app_commands.describe(event_id="The event ID from /event_list.",
                           action="join = attend, leave = stop attending.")
    @app_commands.choices(action=[
        app_commands.Choice(name="join", value="join"),
        app_commands.Choice(name="leave", value="leave"),
    ])
    async def event_rsvp(self, interaction: discord.Interaction,
                         event_id: int,
                         action: app_commands.Choice[str]) -> None:
        member = await guild_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        row = db.get_event(event_id, guild.id)
        error = _validate_pending(row, event_id)
        if error is not None:
            await interaction.response.send_message(
                embed=ui.err("❌ Cannot RSVP", error), ephemeral=True)
            return
        try:
            if action.value == "join":
                result = db.rsvp_join(event_id, guild.id, member.id)
                if result == "already":
                    await interaction.response.send_message(
                        embed=ui.info(
                            "ℹ️ Already attending",
                            f"You are already on the RSVP list for event "
                            f"#{event_id} ({row['name']})."),
                        ephemeral=True)
                elif result == "not_found":
                    await interaction.response.send_message(
                        embed=ui.err(
                            "❌ Event not found",
                            f"No event with ID {event_id} in this server."),
                        ephemeral=True)
                else:
                    await interaction.response.send_message(
                        embed=ui.ok(
                            f"✅ You are attending event #{event_id}",
                            f"You are now attending event #{event_id} "
                            f"({row['name']}) on {format_wib(row['start_at'])}. "
                            "You will be mentioned in its channel reminder."))
            else:
                result = db.rsvp_leave(event_id, guild.id, member.id)
                if result == "not_attending":
                    await interaction.response.send_message(
                        embed=ui.info(
                            "ℹ️ Not attending",
                            f"You are not on the RSVP list for event "
                            f"#{event_id} ({row['name']})."),
                        ephemeral=True)
                elif result == "not_found":
                    await interaction.response.send_message(
                        embed=ui.err(
                            "❌ Event not found",
                            f"No event with ID {event_id} in this server."),
                        ephemeral=True)
                else:
                    await interaction.response.send_message(
                        embed=ui.ok(
                            f"✅ Left RSVP list for event #{event_id}",
                            f"You left the RSVP list for event #{event_id} "
                            f"({row['name']})."))
        except Exception:
            await interaction.response.send_message(
                embed=ui.err("❌ Could not update RSVP",
                             "Could not update your RSVP right now. "
                             "Please try again later."),
                ephemeral=True)

    @app_commands.command(name="event_attendees", description="List the members attending an event.")
    @app_commands.describe(event_id="The event ID from /event_list.")
    async def event_attendees(self, interaction: discord.Interaction,
                              event_id: int) -> None:
        member = await guild_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        row = db.get_event(event_id, guild.id)
        if row is None:
            await interaction.response.send_message(
                embed=ui.err("❌ Event not found",
                             f"No event with ID {event_id} in this server."),
                ephemeral=True)
            return
        ids = db.event_attendees(event_id)
        if not ids:
            await interaction.response.send_message(
                embed=ui.info(
                    "ℹ️ No attendees yet",
                    f"No one has RSVP'd to event #{event_id} "
                    f"({row['name']}) yet. Use /event_rsvp to join."),
                ephemeral=True)
            return
        lines = []
        for uid in ids[:50]:
            m = guild.get_member(uid)
            lines.append(m.mention if m is not None else f"User ID {uid}")
        extra = len(ids) - len(lines)
        if extra > 0:
            lines.append(f"and {extra} more.")
        embed = ui.make(
            f"👥 Attendees for event #{event_id} ({row['name']})",
            f"{len(ids)} attending:\n" + "\n".join(lines),
            colour=ui.BLURPLE)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="event_edit", description="Edit a pending event's name, time, description, and/or channel (staff only).")
    @app_commands.describe(
        event_id="The event ID from /event_list.",
        name="New event name (leave empty to keep the current name).",
        start=f"New start time in WIB, format {TIME_FORMAT}, e.g. {TIME_EXAMPLE} (leave empty to keep it).",
        description="New description (leave empty to keep the current one).",
        channel="New channel for the event and its reminder (leave empty to keep it).")
    async def event_edit(self, interaction: discord.Interaction,
                         event_id: int,
                         name: str | None = None,
                         start: str | None = None,
                         description: str | None = None,
                         channel: discord.TextChannel | None = None) -> None:
        member = await staff_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        row = db.get_event(event_id, guild.id)
        error = _validate_pending(row, event_id)
        if error is not None:
            await interaction.response.send_message(
                embed=ui.err("❌ Cannot edit event", error), ephemeral=True)
            return
        new_name = name.strip() if name and name.strip() else None
        new_desc = description if description else None
        new_start_iso = None
        if start:
            try:
                start_utc = parse_wib_datetime(start)
            except ValueError as e:
                await interaction.response.send_message(
                    embed=ui.err("❌ Invalid date/time", str(e)), ephemeral=True)
                return
            if start_utc <= datetime.now(timezone.utc):
                await interaction.response.send_message(
                    embed=ui.err(
                        "❌ Time is in the past",
                        "That time is in the past. Please use a future time "
                        f"in WIB, format {TIME_FORMAT}, e.g. {TIME_EXAMPLE}."),
                    ephemeral=True)
                return
            new_start_iso = start_utc.isoformat()
        if new_name is None and new_desc is None and new_start_iso is None and channel is None:
            await interaction.response.send_message(
                embed=ui.info(
                    "ℹ️ Nothing to change",
                    "Nothing to change: give at least one of name, start, "
                    "description, or channel."),
                ephemeral=True)
            return
        try:
            ok = db.update_event(event_id, guild.id, name=new_name,
                                 description=new_desc,
                                 start_at_utc_iso=new_start_iso,
                                 channel_id=channel.id if channel is not None else None)
        except Exception:
            await interaction.response.send_message(
                embed=ui.err("❌ Could not edit event",
                             "Could not edit the event right now. "
                             "Please try again later."),
                ephemeral=True)
            return
        if not ok:
            await interaction.response.send_message(
                embed=ui.err("❌ Event not found",
                             f"No event with ID {event_id} in this server."),
                ephemeral=True)
            return
        updated = db.get_event(event_id, guild.id)
        fields: list[tuple[str, str, bool]] = [
            ("Event ID", str(event_id), True),
            ("Starts", format_wib(updated["start_at"]), True),
        ]
        if updated["description"]:
            fields.append(("Description", updated["description"][:500], False))
        fields.append(("Channel", f"<#{updated['channel_id']}>", False))
        embed = ui.make(f"✅ Event updated: {updated['name']}", None,
                        colour=ui.SUCCESS, fields=fields,
                        footer="Start time moved, so this event will be "
                               "reminded again."
                        if new_start_iso is not None else None)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
