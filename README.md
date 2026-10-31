# NEWR Stats

Discord bot for NEWR LABS (guild ID `1465422166790242377`) that shows
server stats, tracks member activity locally, and manages simple
server events with channel reminders.

## Slash commands only

**Every command in this bot is a slash command** (`discord.app_commands`,
guild-synced to NEWR LABS). There is no prefix command, no chat-triggered
command, and no feature that requires typing a prefix — `command_prefix`
is `None` in `bot.py`. The only non-command listeners are passive
activity tracking (`on_message` counting and `on_voice_state_update`
timing), which run automatically in the background and never respond
to a user message.

| Slash command | Who | What it does |
|---|---|---|
| `/stats` | Anyone in the server | Server overview: total members, humans/bots, verified/unverified counts (if verification roles exist), text/voice channel counts, boost count/level. |
| `/leaderboard [text\|voice]` | Anyone in the server | Top 10 by activity in the last 30 days. `text` = messages, `voice` = voice minutes. Defaults to `text`. |
| `/activity <member>` | Anyone in the server | That member's message count + voice minutes in the last 30 days. |
| `/event_create <name> <YYYY-MM-DD HH:MM WIB> [description]` | Staff only | Create an event in the channel where the command is used. Time is WIB (Asia/Jakarta, UTC+7). |
| `/event_list` | Anyone in the server | List upcoming (non-cancelled) events. |
| `/event_cancel <id>` | Staff only | Cancel an event by its ID from `/event_list`. No reminder will be posted for it. |
| `/event_remind` | Staff only | Post a reminder now, in each event's own channel, for events starting within the next 24 hours. |
| `/serverinfo` | Anyone in the server | Detailed guild info: owner, created date, roles count, emojis, channels, boosts, verification level. |

Total: **8 slash commands** (under the 30-command limit).

**Staff gate:** guild owner, or a member with the **Administrator** or
**Manage Guild** permission. Non-staff users get an ephemeral
"Staff only" reply from `/event_create`, `/event_cancel`, and
`/event_remind`. All commands are guild-only; used in DMs they reply
that they must be used inside a server.

## Activity tracking — what it does and does not do

- **Does:** on every non-bot message in the guild, increment that user's
  message count for that UTC day in local SQLite (`data.db`). On voice
  join/leave/move/disconnect, accumulate that user's voice minutes
  (session start kept in memory, flushed to SQLite on leave/move;
  best-effort flush on bot shutdown).
- **Does not:** read any channel history, ever. There is no backfill.
  `/leaderboard` and `/activity` only query the last 30 days of the
  local database, so **all counts start from when the bot first runs**
  and are empty/zero before that. Voice sessions open when the bot
  restarts are not recovered (only sessions started while the bot was
  running are counted).
- **Does not** DM anyone. Reminders go to event channels only.

## Events and reminders

- Times are typed in WIB as `YYYY-MM-DD HH:MM`, e.g. `2026-11-05 19:00`,
  and stored as UTC ISO strings. An invalid date (or a past time) gets
  an ephemeral error message with the format example — never a crash.
- A background loop runs every 60 seconds: for events starting within
  the next 1 hour that have not been reminded yet, it posts a reminder
  embed in the event's channel and marks the event reminded, so it
  fires exactly once. Cancelled events are never reminded. If the
  event channel no longer exists or is not a text channel, the event
  is marked reminded so it does not retry forever.
- `/event_remind` also marks events reminded after posting, so an
  event reminded manually will **not** get a second 1-hour reminder.
  Each event is reminded at most once, by either path.
- Events table: `id, guild_id, channel_id, name, description,
  start_at (UTC ISO), created_by, cancelled, reminded, created_at`.

## Verification counts

`/stats` finds verification roles by role **name** (copied idea from
NEWR Support, no shared import, no hardcoded role IDs): a role whose
name contains "verif" counts as verified, except names containing
"unverif", which count as unverified ("unverified" contains
"verified", so it is checked first). If the guild has no such role,
`/stats` says so instead of showing counts. Counts use cached
members (members intent).

## Setup

Prerequisites: Python 3.11+, a Discord application + bot for NEWR Stats
(not created by this repo — see below), and these privileged intents
enabled in the Developer Portal: **Server Members Intent**,
**Message Content Intent**. Voice states are a non-privileged intent
but must be on for voice tracking.

1. Put the bot token in `.env` (never commit `.env`). The shipped
   `.env` placeholder contains only `DISCORD_TOKEN=` — just fill that
   in. `GUILD_ID` defaults to NEWR LABS (`1465422166790242377`) in
   `bot.py` when unset; `.env.example` also lists it explicitly:
   ```bash
   # edit ~/workspace/bots/newr-stats/.env: DISCORD_TOKEN=<bot token>
   ```
2. Invite the bot to NEWR LABS with at least: Send Messages, Embed
   Links, Read Message History is **not** required (no history is
   read), Connect is not required (the bot never joins voice itself).
   The bot only needs to see members/messages/voice states to track.
3. Run it (foreground, log to `~/workspace/bots/logs/newr-stats.log`):
   ```bash
   ~/workspace/bots/ops/run-newr-stats.sh
   ```
   The script creates `~/workspace/bots/newr-stats/.venv` on first run,
   installs `requirements.txt`, refuses to start with an empty/placeholder
   token, then runs `python -u bot.py` from the bot directory. On
   ready it logs `Logged in as ...` and `Synced N slash commands to
   guild 1465422166790242377`. Slash commands may take a few minutes
   to appear in the client after first sync.
4. VM egress note (same as NEWR Support/NEWR AI): direct TLS to Discord
   fails from this VM, so `bot.py` passes the proxy from `HTTPS_PROXY`
   explicitly via `_proxy_kwargs()`. If the bot runs elsewhere without
   a proxy set, that function returns `{}` and the bot connects directly.

## Files

- `bot.py` — entrypoint, intents, proxy kwargs, guild-only slash sync.
- `db.py` — SQLite: message counts, voice minutes, events.
- `helpers.py` — WIB time parsing/formatting, staff gate, verification counts.
- `cogs/stats.py` — `/stats`, `/serverinfo`.
- `cogs/activity.py` — passive tracking + `/leaderboard`, `/activity`.
- `cogs/events.py` — `/event_create`, `/event_list`, `/event_cancel`,
  `/event_remind` + 60-second reminder loop.
- `requirements.txt` — discord.py 2.7, python-dotenv, aiohttp.
- `.env.example`, `.env` — token config (`.env` ships empty, chmod 600).
