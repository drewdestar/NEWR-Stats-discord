"""Slash commands: /stats and /serverinfo.

Both are read-only server overviews. No prefix commands exist in
this bot at all: every user-triggered action is an app_command
(slash command) synced to the guild.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from helpers import format_created, guild_only, verified_counts


class Stats(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

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
        embed = discord.Embed(title=f"📊 {guild.name} — Stats",
                              colour=discord.Colour.blurple())
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Total members",
                        value=str(guild.member_count or len(guild.members)))
        embed.add_field(name="Humans", value=str(humans))
        embed.add_field(name="Bots", value=str(bots))
        if verified is None:
            embed.add_field(name="Verified / Unverified",
                            value="No verification roles found")
        else:
            embed.add_field(name="Verified", value=str(verified))
            embed.add_field(name="Unverified", value=str(unverified))
        embed.add_field(name="Text channels", value=str(text_channels))
        embed.add_field(name="Voice channels", value=str(voice_channels))
        embed.add_field(name="Boosts",
                        value=f"{guild.premium_subscription_count or 0} "
                              f"(Level {guild.premium_tier})")
        embed.set_footer(text="Member counts use cached members; "
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
        embed = discord.Embed(title=f"ℹ️ {guild.name} — Server Info",
                              colour=discord.Colour.blurple())
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Server ID", value=str(guild.id), inline=False)
        embed.add_field(name="Owner", value=owner_text, inline=False)
        embed.add_field(name="Created", value=format_created(guild.created_at),
                        inline=False)
        embed.add_field(name="Members",
                        value=str(guild.member_count or len(guild.members)))
        embed.add_field(name="Roles", value=str(len(guild.roles)))
        embed.add_field(name="Emojis",
                        value=f"{len(guild.emojis)} total "
                              f"({sum(1 for e in guild.emojis if e.animated)} animated)")
        embed.add_field(name="Channels",
                        value=f"{len(guild.text_channels)} text · "
                              f"{len(guild.voice_channels)} voice · "
                              f"{len(guild.categories)} categories")
        embed.add_field(name="Boosts",
                        value=f"{guild.premium_subscription_count or 0} "
                              f"(Level {guild.premium_tier})")
        embed.add_field(name="Verification level",
                        value=str(guild.verification_level))
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Stats(bot))
