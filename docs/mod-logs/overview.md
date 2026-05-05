# Mod Logs

## Purpose
Every moderation action and message event gets logged to a designated channel with full attribution. Staff should never have to guess who did what.

## Log Channel Setup
- `/setlogchannel #channel` — sets the server's mod log channel
- Stored in config (JSON or SQLite per guild)

## Events to Log

### Message Events
- **Message deleted** — content, author, channel, who deleted it
  - Check Discord audit log for `MESSAGE_DELETE` entry within a short window (~3s)
  - If audit entry found → attribute to the moderator who deleted it
  - If no audit entry → deleted by the message author themselves
  - If no audit entry and message author is owner → owner deleted it
- **Message edited** — before/after content, author, channel, timestamp
- **Bulk message delete** — who triggered it, how many, channel

### Member Events
- Member joined — account age, avatar, flags
- Member left / kicked — distinguish via audit log
- Member banned
- Member unbanned
- Nickname changed
- Role added / removed

### Moderation Actions (bot-issued)
- Warn issued — reason, issuing mod
- Mute / timeout — duration, reason, mod
- Kick — reason, mod
- Ban — reason, mod
- Lockdown start / end — channel, mod

### Server Events
- Channel created / deleted
- Role created / deleted
- Bulk actions (triggers anti-nuke alert)

## Log Format

Each log entry is a Discord embed:
- **Color**: action type (red = ban/kick, yellow = warn/mute, blue = info)
- **Title**: action name (e.g. "Message Deleted")
- **Fields**: relevant details (user, mod, reason, content, timestamps)
- **Footer**: event ID + timestamp

## Audit Log Timing
Discord's audit log has a delay. The bot should:
1. Catch the event
2. Wait ~2-3 seconds
3. Fetch recent audit log entries filtered by action type
4. Match by target user ID + timestamp proximity
5. Attribute accordingly
