# Anti-Nuke

## Purpose
Detect and freeze mass destructive actions in real time before they cause irreversible damage. No automatic punitive actions beyond freezing the offending account's ability to take further action in that server.

## What Counts as a Nuke
Any single account performing multiple destructive actions in a short window:

| Action | Threshold | Window |
|--------|-----------|--------|
| Channel deletes | 3+ | 10 seconds |
| Channel creates | 5+ | 10 seconds |
| Role deletes | 3+ | 10 seconds |
| Mass bans | 5+ | 10 seconds |
| Mass kicks | 5+ | 10 seconds |
| Webhook creates | 5+ | 10 seconds |

Thresholds are configurable per server via `/antinuke config`.

## Response on Detection
1. **Freeze** — remove all roles from offending account (strips permissions instantly)
2. **Alert** — ping server owner via DM + post in mod log channel with full details
3. **Audit entry** — full log of what actions were taken and by whom
4. No auto-ban — owner decides next step

## Manual Recovery
- `/antinuke unfreeze @user` — restores roles, owner only
- `/antinuke status` — shows currently frozen accounts
- Owner dashboard in mod log shows full action trail

## Whitelist
- `/antinuke whitelist add @user` — exempt trusted bots/admins from detection
- `/antinuke whitelist remove @user`
- `/antinuke whitelist list`

## Per-Server Config
- `/antinuke config` — view/edit thresholds
- `/antinuke enable` / `/antinuke disable`
- Thresholds stored per guild in config
