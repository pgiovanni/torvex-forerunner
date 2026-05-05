# Alt Detection

## Purpose
Identify likely alt accounts using behavioral and heuristic signals. Discord does not expose IPs or device fingerprints, so detection is inference-based — confidence scoring, not certainty.

## Signals (Heuristic)

| Signal | Weight | Notes |
|--------|--------|-------|
| Account age < 7 days | High | Very new accounts joining are suspicious |
| Account age < 30 days | Medium | Still worth flagging |
| No avatar | Medium | Common with throwaway accounts |
| Username similarity to banned user | High | Levenshtein distance check against ban list |
| Join → immediate activity | Medium | Messaging within 60s of joining |
| Joined multiple servers the bot is in | Medium | Cross-server pattern |
| Rejoined after ban | Very High | Same username/discriminator pattern |
| No mutual servers with existing members | Low | Isolation indicator |
| Default username pattern (e.g. "User1234567") | Low | New Discord username format alts |

## Confidence Score
Each signal contributes to a 0-100 confidence score:
- 0-30: clean
- 31-60: watch (added to watchlist automatically)
- 61-80: flag (staff pinged)
- 81-100: high confidence alt (auto-action based on server config)

## Actions by Config
- **Alert only** — ping staff with score breakdown
- **Auto watchlist** — added to watchlist at threshold
- **Auto kick** — kick if score exceeds configured threshold
- **Auto ban** — ban if score exceeds configured threshold (off by default)

## Commands
- `/altcheck @user` — run alt check on a user manually, shows score + signals
- `/altconfig` — set thresholds and auto-action for the server

## Limitations
- This is probabilistic, not definitive — staff should always review before punishing
- False positives are possible, especially for new legitimate users
- No IP data available from Discord API
