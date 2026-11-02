"""Slash commands: /stats, /serverinfo, /stats_setup, /stats_disable.

Live stats channels (/stats_setup): staff-only. It creates (or
reuses, if already set up) one category and four voice channels
inside it, named with live counts, e.g. "Members: 123". A
10-minute loop renames a channel only when its count changed,
so Discord's channel-rename rate limit is respected (never
every minute, never a rename for an unchanged count). Counts
use cached members (members intent). "Online" does NOT use
member.status, because that needs the Presence Intent, which
this bot did not enable, so it always read 0. It now uses
Discord's guild preview count (approximate_presence_count via
fetch_guild(with_counts=True)), which works without that
intent, falling back to cached status only if the fetch fails.
The Server Stats category is forced to position 0 (top).
/stats_disable stops the updates
and deletes the channels this bot created (best effort), and
deletes the category only if it is then empty.

Both overviews are read-only. No prefix commands exist in
this bot at all: every user-triggered action is an app_command
(slash command) synced to the guild.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
import ui
from helpers import format_created, guild_only, staff_only, verified_counts

# kind -> channel-name template. Voice channels are used so the
# count shows in the channel list without anyone joining them.
_STATS_KINDS = [
    ("members", "Members: {n}"),
    ("humans", "Humans: {n}"),
    ("bots", "Bots: {n}"),
    ("online", "Online: {n}"),
]


def _live_counts(guild: discord.Guild) -> dict[str, int]:
    humans = sum(1 for m in guild.members if not m.bot)
    bots = sum(1 for m in guild.members if m.bot)
    online = sum(1 for m in guild.members
                 if str(m.status) != "offline")
    return {"members": guild.member_count or len(guild.members),
            "humans": humans, "bots": bots, "online": online}


class Stats(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.stats_loop.start()

    async def cog_unload(self) -> None:
        self.stats_loop.cancel()

    # -- live-stats loop (10 minutes, rename only on change) -----------

    @tasks.loop(minutes=10)
    async def stats_loop(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self._update_guild(guild)
            except Exception as e:
                print(f"[stats] live-stats update failed for guild {guild.id}: {e}")

    @stats_loop.before_loop
    async def _before_stats_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _update_guild(self, guild: discord.Guild) -> None:
        rows = db.get_stats_channels(guild.id)
        if not rows:
            return
        counts = _live_counts(guild)
        # Online fix: guild preview presence count, not member.status
        # (which is offline for everyone without Presence Intent).
        try:
            fresh = await self.bot.fetch_guild(guild.id, with_counts=True)
            if fresh.approximate_presence_count is not None:
                counts["online"] = fresh.approximate_presence_count
            if fresh.approximate_member_count is not None:
                counts["members"] = fresh.approximate_member_count
        except Exception as e:
            print(f"[stats] guild-count fetch failed, using cached counts: {e}")
        # Category to the very top, so the stats are easy to see.
        try:
            cat_id = rows[0]["category_id"]
            cat = guild.get_channel(cat_id) if cat_id else None
            if isinstance(cat, discord.CategoryChannel) and cat.position != 0:
                await cat.edit(position=0)
        except Exception as e:
            print(f"[stats] category move failed: {e}")
        templates = dict(_STATS_KINDS)
        for row in rows:
            channel = guild.get_channel(row["channel_id"])
            if not isinstance(channel, discord.VoiceChannel):
                continue  # deleted / wrong type: leave the record; setup recreates it
            wanted = templates[row["kind"]].format(n=counts.get(row["kind"], 0))
            if channel.name != wanted:
                try:
                    await channel.edit(name=wanted)
                except Exception as e:
                    print(f"[stats] rename failed for channel {channel.id}: {e}")

    # -- slash commands --------------------------------------------------

    @app_commands.command(name="stats", description="Server overview: members, channels, boosts, verification.")
    async def stats(self, interaction: discord.Interaction) -> None:
        member = await guild_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        humans = sum(1 for m in guild.members if not m.bot)
        bots = sum(1 for m in guild.members if m.bot)
        verified, unverified = verified_counts(guild)
        text_channels = len(guild.text_channels)
        voice_channels = len(guild.voice_channels)
        fields: list[tuple[str, str, bool]] = [
            ("Total members", str(guild.member_count or len(guild.members)), True),
            ("Humans", str(humans), True),
            ("Bots", str(bots), True),
        ]
        if verified is None:
            fields.append(("Verified / Unverified",
                           "No verification roles found", True))
        else:
            fields.append(("Verified", str(verified), True))
            fields.append(("Unverified", str(unverified), True))
        fields.extend([
            ("Text channels", str(text_channels), True),
            ("Voice channels", str(voice_channels), True),
            ("Boosts",
             f"{guild.premium_subscription_count or 0} "
             f"(Level {guild.premium_tier})", True),
        ])
        embed = ui.make(
            f"📊 {guild.name} Stats", None, colour=ui.BLURPLE,
            fields=fields,
            thumbnail=guild.icon.url if guild.icon is not None else None,
            footer="Member counts use cached members; "
                   "verification counts need verification roles.")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="Detailed guild info: owner, created date, roles, emojis, and more.")
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        member = await guild_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        owner = guild.owner
        owner_text = (f"{owner.mention} (ID {owner.id})" if owner is not None
                      else f"ID {guild.owner_id}")
        embed = ui.make(
            f"ℹ️ {guild.name} Server Info", None, colour=ui.BLURPLE,
            fields=[
                ("Server ID", str(guild.id), False),
                ("Owner", owner_text, False),
                ("Created", format_created(guild.created_at), False),
                ("Members", str(guild.member_count or len(guild.members)), True),
                ("Roles", str(len(guild.roles)), True),
                ("Emojis",
                 f"{len(guild.emojis)} total "
                 f"({sum(1 for e in guild.emojis if e.animated)} animated)", True),
                ("Channels",
                 f"{len(guild.text_channels)} text · "
                 f"{len(guild.voice_channels)} voice · "
                 f"{len(guild.categories)} categories", True),
                ("Boosts",
                 f"{guild.premium_subscription_count or 0} "
                 f"(Level {guild.premium_tier})", True),
                ("Verification level", str(guild.verification_level), True),
            ],
            thumbnail=guild.icon.url if guild.icon is not None else None)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="stats_setup", description="Set up live stats channels (staff only). Creates or updates them.")
    @app_commands.describe(category="Name of the category to create or reuse for the stats channels.")
    async def stats_setup(self, interaction: discord.Interaction,
                          category: str | None = None) -> None:
        member = await staff_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        category_name = (category or "Server Stats").strip() or "Server Stats"
        if len(category_name) > 100:
            await interaction.response.send_message(
                embed=ui.err("❌ Category name too long",
                             "Category name is too long (max 100 characters)."),
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            existing_cat = next(
                (c for c in guild.categories if c.name == category_name), None)
            if existing_cat is not None:
                cat = existing_cat
            else:
                cat = await guild.create_category(name=category_name)
        except Exception as e:
            await interaction.followup.send(
                embed=ui.err(
                    "❌ Could not create category",
                    f"Could not create the category {category_name!r}: {e}. "
                    "Check that the bot has the Manage Channels permission."),
                ephemeral=True)
            return
        counts = _live_counts(guild)
        created, reused, failed = [], [], []
        for kind, template in _STATS_KINDS:
            wanted = template.format(n=counts[kind])
            channel = None
            for row in db.get_stats_channels(guild.id):
                if row["kind"] == kind:
                    found = guild.get_channel(row["channel_id"])
                    if isinstance(found, discord.VoiceChannel):
                        channel = found
                    break
            try:
                if channel is None:
                    channel = await guild.create_voice_channel(
                        name=wanted, category=cat)
                    created.append(wanted)
                else:
                    if channel.category_id != cat.id:
                        await channel.edit(category=cat)
                    if channel.name != wanted:
                        await channel.edit(name=wanted)
                    reused.append(wanted)
                db.set_stats_channel(guild.id, kind, channel.id, cat.id)
            except Exception as e:
                failed.append(f"{kind} ({e})")
        lines = []
        if created:
            lines.append("Created: " + ", ".join(created))
        if reused:
            lines.append("Updated/reused: " + ", ".join(reused))
        if failed:
            lines.append("Failed (check Manage Channels permission): "
                         + "; ".join(failed))
        embed = ui.ok(
            "✅ Live stats channels set up",
            f"Live stats channels are set up in category {cat.name!r} "
            "and refresh every 10 minutes (only when a count changes).",
            fields=[("Channels", "\n".join(lines), False)] if lines else None)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="stats_disable", description="Turn off live stats channels and delete the ones this bot created (staff only).")
    async def stats_disable(self, interaction: discord.Interaction) -> None:
        member = await staff_only(interaction)
        if member is None:
            return
        guild = interaction.guild
        assert guild is not None
        rows = db.get_stats_channels(guild.id)
        if not rows:
            await interaction.response.send_message(
                embed=ui.info(
                    "ℹ️ Live stats not set up",
                    "Live stats channels are not set up in this server. "
                    "Use /stats_setup to set them up."),
                ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        category_ids = {r["category_id"] for r in rows if r["category_id"]}
        failed = []
        for row in rows:
            channel = guild.get_channel(row["channel_id"])
            if channel is None:
                continue
            try:
                await channel.delete()
            except Exception as e:
                failed.append(f"{channel.name} ({e})")
        db.clear_stats_channels(guild.id)
        # Delete a category only if it is now empty, so categories
        # shared with other channels are never removed.
        for cat_id in category_ids:
            cat = guild.get_channel(cat_id)
            if isinstance(cat, discord.CategoryChannel) and not cat.channels:
                try:
                    await cat.delete()
                except Exception as e:
                    failed.append(f"category ({e})")
        desc = ("Live stats channels are turned off and the channels "
                "this bot created were deleted.")
        fields = None
        if failed:
            fields = [("Some deletions failed",
                       "Check Manage Channels permission: " + "; ".join(failed),
                       False)]
        await interaction.followup.send(
            embed=ui.ok("✅ Live stats turned off", desc, fields=fields),
            ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Stats(bot))
