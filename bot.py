"""NEWR Stats bot entrypoint. Config comes only from environment variables.

Required: DISCORD_TOKEN, GUILD_ID (guild commands sync target).
Optional: DB_PATH.
Run:  pip install -r requirements.txt && python3 bot.py
       (or use ../ops/run-newr-stats.sh)

SLASH COMMANDS ONLY: command_prefix is None and no prefix command,
no chat-triggered command, and no message-content command exists
anywhere in this bot. Every user-triggered action is a discord
app_command (slash command) synced to the guild below. The only
message/voice listeners are passive activity tracking (on_message
counts, on_voice_state_update minutes); they never respond to a
user and never require typing a prefix.

Structure copied from newr-support/bot.py: discord.py 2.7,
_proxy_kwargs() for VM egress, guild-only sync on ready.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import discord
from discord.ext import commands

try:
    from dotenv import load_dotenv  # optional, only if installed
    load_dotenv()
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402
from cogs.activity import flush_all_sessions  # noqa: E402

COGS = ["cogs.stats", "cogs.activity", "cogs.events"]
# Guild-sync target. Defaults to NEWR LABS so the shipped placeholder
# .env (which contains only DISCORD_TOKEN=) still syncs guild-only;
# setting GUILD_ID in .env overrides the default.
DEFAULT_GUILD_ID = 1465422166790242377
try:
    GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else DEFAULT_GUILD_ID
except ValueError:
    GUILD_ID = DEFAULT_GUILD_ID


def _proxy_kwargs() -> dict:
    """VM egress goes through an HTTP proxy; direct TLS to Discord fails
    here (SSL WRONG_VERSION_NUMBER), while aiohttp with trust_env works.
    discord.py does not read proxy env vars itself, so pass them explicitly.
    Credentials stay in the environment, never in code or logs."""
    from urllib.parse import unquote, urlparse

    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not raw:
        return {}
    parsed = urlparse(raw)
    proxy = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 3128}"
    kwargs: dict = {"proxy": proxy}
    if parsed.username:
        import aiohttp

        kwargs["proxy_auth"] = aiohttp.BasicAuth(
            unquote(parsed.username),
            unquote(parsed.password or ""))
    return kwargs


class StatsBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True  # privileged: needed for member/human/bot counts
        intents.messages = True
        intents.message_content = True  # privileged: needed for on_message counting
        intents.voice_states = True  # needed for voice-time tracking
        # command_prefix=None: slash commands ONLY. No prefix commands
        # can be registered or triggered in this bot.
        super().__init__(command_prefix=None, intents=intents, help_command=None,
                         **_proxy_kwargs())
        self.started_at = datetime.now(timezone.utc)

    async def setup_hook(self) -> None:
        db.init_db()
        for cog in COGS:
            await self.load_extension(cog)

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (id={self.user.id})")
        if getattr(self, "_synced", False):
            return
        self._synced = True
        # Always guild-only: GUILD_ID always has a value (env or default).
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash commands to guild {GUILD_ID}")

    async def close(self) -> None:
        # Best-effort: flush open voice sessions so minutes are not lost.
        try:
            flush_all_sessions()
        except Exception as e:
            print(f"[activity] shutdown flush failed: {e}")
        await super().close()


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set. See .env.example.")
    asyncio.run(StatsBot().start(token))


if __name__ == "__main__":
    main()
