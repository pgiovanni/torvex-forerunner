# Heat System & Watchlists

## Purpose
Track user behavior over time. Instead of binary banned/not-banned, users accumulate "heat" from infractions. Heat decays over time. Thresholds trigger escalating auto-actions.

## Heat Sources

| Action | Heat Added |
|--------|-----------|
| Warn | +10 |
| Mute / Timeout | +20 |
| Kick | +35 |
| Flagged by alt detection | +25 |
| Added to watchlist manually | +15 |
| Spam detection trigger | +10 |

## Heat Decay
- Heat decays by 5 points per 24 hours of no infractions
- Full decay to 0 takes ~30 days of clean behavior
- Decay pauses while user is on watchlist or blacklist

## Heat Thresholds & Auto-Actions

| Heat | Auto-Action |
|------|------------|
| 30+ | Added to watchlist automatically |
| 50+ | Staff pinged on any message from user |
| 70+ | Auto 1-hour timeout on next infraction |
| 90+ | Auto 24-hour timeout on next infraction |
| 100 | Auto-ban recommendation (staff must confirm) |

All thresholds are configurable per server.

## Watchlist
- Users on watchlist have all messages monitored
- Staff gets a subtle ping (or log entry) when they send a message
- `/watchlist add @user reason` — manual add
- `/watchlist remove @user`
- `/watchlist view` — shows all watchlisted users + heat + reason

## Blacklist
- Immediate action on join (kick or ban depending on config)
- `/blacklist add @user reason`
- `/blacklist remove @user`
- `/blacklist view`
- Blacklist is per-guild, not global

## Commands
- `/heat @user` — view current heat score + history
- `/heatconfig` — configure thresholds and auto-actions
- `/heatchart` — visual breakdown of heat distribution across server members (staff only)
