# Moderation

## Commands

| Command | Description | Permission |
|---------|-------------|------------|
| `/warn @user reason` | Issue a warning, logged + DMd to user | Moderator |
| `/mute @user duration reason` | Timeout user (Discord native timeout) | Moderator |
| `/unmute @user` | Remove timeout | Moderator |
| `/kick @user reason` | Kick from server | Moderator |
| `/ban @user reason` | Ban from server | Admin |
| `/unban user_id reason` | Unban by ID | Admin |
| `/lockdown #channel` | Set channel to read-only for @everyone | Moderator |
| `/unlock #channel` | Restore channel permissions | Moderator |
| `/purge amount` | Bulk delete messages (up to 100) | Moderator |
| `/history @user` | View mod action history for a user | Moderator |

## Warn System
- Warns are stored per user per guild
- Each warn has: reason, issuing mod, timestamp
- Warn thresholds feed into the Heat System (see heat-system/overview.md)
- User gets a DM on warn with reason + server name

## Channel Lockdown
- `/lockdown` sets `Send Messages = False` for `@everyone` on the target channel
- Moderators retain send access via role override
- `/unlock` restores the previous permission state
- Lockdown is logged to mod log channel

## Purge
- Deletes up to 100 messages at a time (Discord API limit)
- Logs: mod who ran it, channel, count deleted
- Messages older than 14 days cannot be bulk deleted (Discord limit) — bot warns if this applies
