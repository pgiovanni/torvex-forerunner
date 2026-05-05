# Giveaways

## Purpose
Run fair, transparent giveaways. Members enter, bot picks winners randomly at end time.

## Flow
1. Staff runs `/giveaway start`
2. Bot posts a giveaway embed with a react-to-enter button
3. Members click to enter
4. At end time, bot picks winner(s) randomly and announces

## Commands
- `/giveaway start prize:"Discord Nitro" duration:24h winners:1 channel:#giveaways`
- `/giveaway end <message_id>` — end early and pick winner now
- `/giveaway reroll <message_id>` — pick a new winner (if original can't claim)
- `/giveaway list` — show active giveaways
- `/giveaway cancel <message_id>` — cancel without picking a winner

## Entry Requirements (Optional)
- Min account age (e.g. 30 days)
- Must have a specific role
- Must be in server for X days

## Embed Format
```
🎉 GIVEAWAY
Prize: Discord Nitro
Ends: in 24 hours
Winners: 1
Hosted by: @Staff

Click the button below to enter!
[Enter Giveaway] — 42 entries
```

## Winner Announcement
- Tags winner(s) in the giveaway channel
- DMs winner with prize info if provided
- Original embed updated to show winner

## Future
- Tie prize into orb system — auto-grant orbs to winner via Torvex API
