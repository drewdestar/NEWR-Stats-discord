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
| `/serverinfo` | Anyone in the server | Detailed guild info: owner, created date, roles count, emojis, channels, boosts, verification level. |
| `/stats_setup [category]` | Staff only | Create (or update/reuse) live stats channels in a category (default `Server Stats`): voice channels named `Members: N`, `Humans: N`, `Bots: N`, `Online: N`. They refresh every 10 minutes, renaming a channel only when its count changed. Failures (e.g. missing Manage Channels) get a clear error, never a crash. |
| `/stats_disable` | Staff only | Stop live stats updates and delete the stats channels this bot created; the category is deleted only if it is then empty. |
| `/leaderboard [text\|voice] [window]` | Anyone in the server | Top 10 by activity. `text` = messages, `voice` = voice minutes (default `text`). Window: `30d` = last 30 days (default, unchanged behaviour), `week` = last 7 days, `month` = since the 1st of the current UTC month. |
| `/activity <member>` | Anyone in the server | That member's message count + voice minutes in the last 30 days. |
| `/rank` | Anyone in the server | Your own rank and percentile, separately for text and voice, over the last 30 days (rank 1 = top; percentile = share of tracked members ranked below you). If you have no activity in a kind, it says so instead of ranking you; with no activity at all it says so clearly. |
| `/rep give <member> [reason]` | Anyone in the server | Give 1 reputation point to another member. No self-rep, no bots, and each giver can give only 1 point per 24 hours (cooldown error states the remaining time). The reason is shown in the confirmation only and is not stored. |
| `/rep show <member>` | Anyone in the server | That member's reputation point total. |
| `/rep_top` | Anyone in the server | Top 10 members by reputation points. |
| `/event_create <name> <YYYY-MM-DD HH:MM WIB> [description] [repeat]` | Staff only | Create an event in the channel where the command is used. Time is WIB (Asia/Jakarta, UTC+7). `repeat`: `none` (default, one-off) or `weekly` (see below). |
| `/event_list` | Anyone in the server | List upcoming (non-cancelled) events; weekly events are marked 🔁 weekly. |
| `/event_cancel <id>` | Staff only | Cancel an event by its ID from `/event_list`. No reminder will be posted for it, and a weekly event cancelled before it passes spawns no successor. |
| `/event_remind` | Staff only | Post a reminder now, in each event's own channel, for events starting within the next 24 hours, mentioning RSVP attendees in the channel (never DM). |
| `/event_rsvp <id> <join\|leave>` | Anyone in the server | Join or leave an event's RSVP list (stored in SQLite). RSVP to a cancelled or past event is rejected with a clear ephemeral error. Joining twice says you are already attending; leaving when not attending says so. |
| `/event_attendees <id>` | Anyone in the server | List the members attending an event (up to 50 shown, plus a count if more). |
| `/event_edit <id> [name] [start] [description] [channel]` | Staff only | Edit a pending event's name, WIB start time, description, and/or channel (at least one field required). Rejects a missing, cancelled, or past event and a past new time. Moving the start time resets the reminded flag so the moved event can be reminded again. |

Total: **16 top-level slash commands** (17 counting the two `/rep`
subcommands separately), under the 100-command limit.

**Staff gate:** guild owner, or a member with the **Administrator** or
**Manage Guild** permission. Non-staff users get an ephemeral
"Staff only" reply from `/event_create`, `/event_cancel`,
`/event_remind`, `/event_edit`, `/stats_setup`, and `/stats_disable`.
All commands are guild-only; used in DMs they reply
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
- **Does not** DM anyone. Reminders go to event channels only, and
  attendee mentions are channel mentions in the reminder message,
  never DMs.
- `/rank` and `/leaderboard` windows use the same local data; the
  `month` window is the current UTC calendar month, not a second
  copy of the 30-day window.

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
  Each event is reminded at most once, by either path — except an
  event whose start time is moved with `/event_edit`, which resets
  the flag so it can be reminded again.
- Both reminder paths mention RSVP attendees (up to 25 mentions,
  plus a "+N more" note) in the channel message, never by DM.
- Recurring events: a `weekly` event spawns exactly one successor
  (+7 days, same name/description/channel/creator) once its start
  has passed, checked by the 60-second loop. The original is flagged
  (`next_created`) so restarts cannot duplicate the successor, and
  the successor is itself weekly, so the chain continues. Cancelled
  events never spawn.
- Events table: `id, guild_id, channel_id, name, description,
  start_at (UTC ISO), created_by, cancelled, reminded, repeat
  ('none'/'weekly'), next_created, created_at`. RSVPs live in
  `event_attendees (event_id, guild_id, user_id)`. Live-stats
  channels live in `stats_channels (guild_id, kind, channel_id,
  category_id)`. Reputation lives in `rep_points
  (guild_id, user_id, points)` and `rep_cooldowns
  (guild_id, giver_id, last_given)`. Older databases are migrated
  on `init_db()` (missing `repeat`/`next_created` columns are added).

## Live stats channels

`/stats_setup` creates (or reuses) a category — default name
`Server Stats`, overridable with the `category` parameter — and
four voice channels inside it: `Members: N`, `Humans: N`,
`Bots: N`, `Online: N` (voice channels so the count shows in the
channel list without joining). A loop in `cogs/stats.py` refreshes
them every **10 minutes** and renames a channel only when its count
actually changed, respecting Discord's channel-rename rate limit;
it never updates every minute. `Online` counts cached members
whose status is not offline. Re-running `/stats_setup` updates the
existing channels in place (and moves them into the named category)
instead of creating duplicates. `/stats_disable` stops updates,
deletes the bot's stats channels, and deletes the category only
if it is then empty. The bot needs Manage Channels for both.

## Reputation

`/rep give` awards 1 point; anti-abuse rules are: no self-rep, no
rep to bots, and one rep gift per giver per 24 hours (per giver,
not per recipient — giving to one member starts the cooldown for
all). The cooldown error states the remaining time. Points are
per guild in SQLite; there is no decay and no per-recipient limit
beyond the giver cooldown.

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
- `cogs/stats.py` — `/stats`, `/serverinfo`, `/stats_setup`,
  `/stats_disable` + 10-minute live-stats loop.
- `cogs/activity.py` — passive tracking + `/leaderboard` (windows),
  `/activity`, `/rank`, `/rep give`, `/rep show`, `/rep_top`.
- `cogs/events.py` — `/event_create` (incl. weekly repeat),
  `/event_list`, `/event_cancel`, `/event_remind`, `/event_rsvp`,
  `/event_attendees`, `/event_edit` + 60-second reminder loop
  (also spawns weekly successors).
- `requirements.txt` — discord.py 2.7, python-dotenv, aiohttp.
- `.env.example`, `.env` — token config (`.env` ships empty, chmod 600).
