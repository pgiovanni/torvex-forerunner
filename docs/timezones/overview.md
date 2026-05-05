# Timezones

## Purpose
Users register their timezone once. The bot uses it everywhere — event times, birthday announcements, scheduled competition start/end times — all shown in each user's local time.

## Commands
- `/timezone set <tz>` — set your timezone (e.g. `America/New_York`, `Europe/Stockholm`)
- `/timezone get @user` — see what timezone a user is in
- `/timezone list` — show all registered timezones in the server (useful for scheduling)
- `/time @user` — show what time it currently is for that user
- `/convert 3pm EST to PST` — quick one-off conversion without registration

## Storage
- Per user, per guild (or global — TBD)
- Stored in SQLite/JSON config: `{ "user_id": "America/Chicago" }`

## Integration Points
- **Birthdays** — announced at midnight in the user's local timezone
- **Giveaway end times** — displayed in viewer's local time in embeds
- **Competition deadlines** — shown in local time
- **Scavenger hunt countdowns** — localized
- **Event scheduling** — `/event create` shows time to each member in their zone

## Timezone Input
- Accept IANA timezone names (`America/New_York`, `Europe/Paris`)
- Accept common abbreviations with disambiguation (`EST`, `PST`, `CET`)
- Bot suggests closest match if input is slightly off
- Use `pytz` or `zoneinfo` (Python 3.9+) for conversion

## Display Format
Always show timezone abbreviation alongside time:
`Ends at 6:00 PM EST (11:00 PM UTC)`
