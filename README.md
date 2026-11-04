# NEWR Stats

Server stats for NEWR LABS. Slash commands only.

`/stats` and `/serverinfo` for overviews, `/stats_setup` for live
count channels (Members, Humans, Bots, Online, refreshed every 10
minutes), activity tracking (messages and voice minutes, counted from
when the bot first started, no history backfill), `/leaderboard`,
`/activity`, `/rank`, a small reputation system (`/rep give`, `/rep
show`, `/rep top`, one gift per giver per day), and staff-run events
with RSVP and reminders.

Event times are typed in WIB. Reminders go to the event channel,
never by DM.

Run it:

```bash
~/workspace/bots/ops/run-newr-stats.sh
```

Token goes in `.env` (`DISCORD_TOKEN`, `GUILD_ID` defaults to NEWR
LABS). The script sets up the venv, refuses to start on an empty
token, and logs to `~/workspace/bots/logs/newr-stats.log`. Needs
Server Members Intent and Message Content Intent. Data lives in
`data.db` (SQLite, local).
