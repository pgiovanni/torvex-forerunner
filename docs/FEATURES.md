# Peepo's Reclaimer — Feature Index

All-in-one Discord bot. Moderation, security, events, and Torvex Lescala integration.

---

## Feature Areas

| # | Feature | Status | Docs |
|---|---------|--------|------|
| 1 | Mod Logs | Planned | [docs/mod-logs/](mod-logs/) |
| 2 | Moderation | Planned | [docs/moderation/](moderation/) |
| 3 | Anti-Nuke | Planned | [docs/anti-nuke/](anti-nuke/) |
| 4 | Alt Detection | Planned | [docs/alt-detection/](alt-detection/) |
| 5 | Heat System & Watchlists | Planned | [docs/heat-system/](heat-system/) |
| 6 | Tickets | In Progress | [docs/tickets/](tickets/) |
| 7 | Competitions | Planned | [docs/competitions/](competitions/) |
| 8 | Giveaways | Planned | [docs/giveaways/](giveaways/) |
| 9 | Scavenger Hunts | Planned | [docs/scavenger-hunts/](scavenger-hunts/) |
| 10 | Torvex Lescala Integration | Planned | [docs/torvex-lescala/](torvex-lescala/) |
| 11 | Timezones | Planned | [docs/timezones/](timezones/) |
| 12 | Birthdays | Planned | [docs/birthdays/](birthdays/) |
| 13 | Discord Signals & Pruning Analysis | Planned | [docs/signals-analysis/](signals-analysis/) |
| 14 | Fun Commands | In Progress | [docs/fun/](fun/) |
| 15 | Security | Ongoing | [docs/security/](security/) |
| 16 | Music | Planned | [docs/music/](music/) |
| 17 | Chess | Planned | [docs/chess/](chess/) |
| 18 | Checkers | Planned | [docs/checkers/](checkers/) |
| 19 | Wordle | Planned | [docs/wordle/](wordle/) |

---

## Build Order

1. **Signals Analysis** — event bus foundation everything else listens on
2. **Mod Logs** — first consumer of the signal bus
3. **Moderation** — kick, ban, mute, warn, lockdown
4. **Heat System & Watchlists** — auto-escalation, blacklist
5. **Anti-Nuke** — mass action detection, freeze
6. **Alt Detection** — behavioral/heuristic flagging
7. **Tickets** — support request flow
8. **Timezones** — register once, used everywhere
9. **Birthdays** — depends on timezones
10. **Giveaways** — basic event system
11. **Competitions** — points-based, custom types
12. **Scavenger Hunts** — clue chains, multi-stage
13. **Torvex Lescala** — API integration, character/game commands
