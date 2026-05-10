# peepos-reclaimer — Claude Context

## What It Is
All-in-one Discord bot for the Torvex community. Moderation, security, events, games, and Torvex Lescala RPG integration. Python + discord.py.

**Repo:** `C:\Users\pgiovanni\source\repos\peepos-reclaimer\`
**VPS:** 187.77.215.240 (Debian 13, root, SSH key-based)
**Guild ID:** `1215140346800119868`

---

## Stack
- Python, `discord.py >= 2.3.0`
- Slash commands via `app_commands`; cogs loaded dynamically from `commands.json`
- `asyncpg` — PostgreSQL (shared with Torvex web app's `discord_users` table)
- `aiohttp` — Torvex API calls
- `Pillow` — image rendering (chess board, wordle tiles)
- `python-chess` + Stockfish — chess engine
- `python-dotenv` — `.env` for secrets

---

## Architecture

### Cog System
- `bot.py` reads `commands.json` on startup, loads cogs dynamically
- Each cog is a file in `cogs/`
- `commands.json` maps commands → cog file names
- Utilities live in `utils/` (renderers, helpers)
- Static data lives in `data/` (words.json, roasts.json)

### Signal Bus (planned — not yet built)
- Central `SignalBus` class that all gateway events feed into
- Routes to: heat system, alt detector, anti-nuke, mod log
- Keeps cogs decoupled — no cog calls another directly
- **Build this first** before moderation features

### Key Patterns
- Cogs use `sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))` for utils imports
- Renderers return `io.BytesIO`; sent as `discord.File(buf, filename="x.png")`
- Edit message with new image: `attachments=[discord.File(...)]` (not `file=`)
- After `defer()`, use `interaction.edit_original_response()` not `edit_message()`
- Font: `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` (confirmed on VPS)
- One game per channel: keyed by `channel_id` in a module-level dict
- All data scoped per `guild_id + user_id` — no cross-guild leakage
- Per-user cooldowns via `@app_commands.checks.cooldown()`

### Storage
- Config, heat scores, watchlists, etc. stored per guild in SQLite or JSON (not yet implemented — design as needed per feature)
- Timezone data: `{ "user_id": "America/New_York" }`
- **Never hardcode secrets** — all in `.env`, which is gitignored

---

## Current Status

### Cogs (Built)
| File | Commands | Status |
|------|----------|--------|
| `cogs/fun.py` | `/roast`, `/8ball`, `/tictactoe`, `/connect4` (PvP) | ✅ Built |
| `cogs/games.py` | `/tictactoe_bot`, `/connect4_bot` (vs AI) | ✅ Built |
| `cogs/wordle.py` | `/wordle` (animated GIF reveal, threads) | ✅ Built |
| `cogs/economy.py` | Peepo Bucks — balance, levels, leaderboard | ✅ Built |
| `cogs/pvp.py` | PvP battles (uses Discord level for stats) | ✅ Built |
| `cogs/rpg.py` | Torvex RPG — fight, level up, earn orbs | ✅ Built |
| `cogs/tickets.py` | `/close` ticket channel | ✅ Built |
| `cogs/emojis.py` | `/backup_emojis` | ✅ Built |
| `cogs/gear.py` | Item/monster dictionary | ✅ Built |
| `cogs/chess_cog.py` | `/chess` — vs Stockfish or PvP | ✅ Built (deploy status unknown) |

### Features (Planned — Not Yet Started)
See `docs/` for full specs on each.

| # | Feature | Docs | Notes |
|---|---------|------|-------|
| 1 | Signals & Pruning Analysis | `docs/signals-analysis/` | **Build first** — everything else depends on it |
| 2 | Mod Logs | `docs/mod-logs/` | First consumer of signal bus |
| 3 | Moderation | `docs/moderation/` | warn, mute, kick, ban, lockdown, purge |
| 4 | Heat System & Watchlists | `docs/heat-system/` | Behavioral scoring, decay, auto-escalation |
| 5 | Anti-Nuke | `docs/anti-nuke/` | Mass action detection, role freeze, owner alert |
| 6 | Alt Detection | `docs/alt-detection/` | Heuristic confidence scoring on join |
| 7 | Tickets | `docs/tickets/` | Private channel per request, `/close` exists |
| 8 | Timezones | `docs/timezones/` | Register once, used everywhere |
| 9 | Birthdays | `docs/birthdays/` | Midnight announce in user's local timezone |
| 10 | Giveaways | `docs/giveaways/` | React-to-enter, timed, optional requirements |
| 11 | Competitions | `docs/competitions/` | Art, trivia, points race, custom |
| 12 | Scavenger Hunts | `docs/scavenger-hunts/` | Multi-stage clue chains |
| 13 | Torvex Lescala Integration | `docs/torvex-lescala/` | Account linking, RPG commands via API |
| 14 | Music | `docs/music/` | yt-dlp + FFmpeg, queue, persistent control panel |
| 15 | Checkers | `docs/checkers/` | PvP or vs minimax AI |

---

## Recommended Build Order
1. **Signals** — event bus foundation
2. **Mod Logs** — first signal consumer
3. **Moderation** — kick, ban, mute, warn, lockdown
4. **Heat System** — auto-escalation from warns
5. **Anti-Nuke** — mass action detection
6. **Alt Detection** — join heuristics
7. **Tickets** — polish existing `/close`
8. **Timezones** — prerequisite for birthdays + events
9. **Birthdays** — depends on timezones
10. **Giveaways** — basic event system
11. **Competitions** — ties into giveaways + games
12. **Scavenger Hunts** — multi-stage events
13. **Torvex Lescala** — RPG account linking + commands
14. **Music** — voice channel streaming

---

## Game Design Document (GDD)
**Source of truth for all RPG mechanics:** `C:\Users\pgiovanni\source\repos\peeposredemption\docs\GAME_DESIGN.md`

The GDD covers:
- Vision & influences (FFX, DQ8, SMT, RuneScape, Darkest Dungeon, etc.)
- Combat system: Press Turn economy, turn icons, positioning (Front/Mid/Back at Lv20)
- Combat styles: Melee / Ranged / Magic / Summoning — no classes, style = what you equip
- 12 elements + damage types, multipliers, elemental status effects
- Stats: STR, DEF, INT, MDEF, DEX, VIT, LUK, SPD + formulas
- Items: 5 tiers, 9 equipment slots, craftable weapons/armor, named uniques
- Enchanting: slots by rarity, named enchants T1/T2/T3, overenchanting risk
- Economy: Coins (grind) + Orbs (premium), loot crates, NPC shop, marketplace
- 8 skills: Combat, Mining, Woodcutting, Fishing, Smithing, Alchemy, Cooking, Enchanting
- Zones: 11 zones (Plains → Void Realm), each with dominant element + weaknesses
- 200+ monsters across all zones with special abilities and drop tables
- Bosses: zone bosses, raid bosses, Counter-All endgame bosses
- Progression: level cap 100, XP formula, gradual unlock by level
- Peepo Collectibles: rarity system, crate odds, pricing, economy
- Summons: 9 summons (Ifrit/Shiva/Bahamut etc.), summon gauge system
- Party System: 2-4 players, co-op combat, turn order, targeting

**Always read the GDD before implementing any RPG feature.** It is the authority.

---

## Torvex API Integration
- Bot calls `TORVEX_API_URL/api/bot/*` endpoints
- Authenticated via `X-Bot-Key` header (stored in `.env` as `TORVEX_BOT_KEY`)
- On every message: fire-and-forget POST to `/api/bot/orbs/message-reward`
- On startup: auto-sync guild emojis → peepo catalog via `/api/bot/peepos/sync`
- asyncpg pool reads `discord_users` table for Discord-level stats (used by PvP)
- Torvex API URL: `https://torvex.app` (prod) / `http://localhost:5000` (dev)

---

## Intents Required
```python
intents.message_content = True
intents.members = True
intents.presences = True  # needs verification for >100 member servers
```

---

## VPS Deploy
- SCP changed files to VPS bot directory (ask user for path + service name if unknown)
- `pip install -r requirements.txt` on VPS after dependency changes
- `apt install stockfish` for chess engine (path: `/usr/games/stockfish`)
- `apt install ffmpeg` for music feature
- Restart bot systemd service after deploy
- Bot runs as non-root user; `.env` is `chmod 600`

---

## Chess — Deploy Checklist (outstanding)
Files that may need deploying:
- `utils/chess_renderer.py`
- `cogs/chess_cog.py`
- `commands.json`
- `requirements.txt` (added `python-chess`)

VPS needs: `pip install python-chess` + `apt install stockfish`
