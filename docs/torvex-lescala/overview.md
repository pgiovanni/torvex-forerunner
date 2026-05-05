# Torvex Lescala Integration

## Purpose
Let Discord members interact with the Torvex Lescala text-based RPG directly from Discord. Data lives in the torvex.app database — the bot hits the API to read/write game state.

## Key Design Principle
Discord account → linked to torvex.app account. If a user transfers from Discord to the web app, their character, inventory, and progress carry over seamlessly.

## Account Linking
- `/link <torvex_token>` — user generates a link token on torvex.app, pastes it in Discord to connect accounts
- Once linked, Discord user ID maps to their torvex.app User ID
- `/unlink` — disconnect accounts

## Commands (Planned)

### Character
- `/character` — view your character stats (class, level, HP, XP)
- `/inventory` — view your items

### Combat
- `/fight` — start a combat session against a random monster
- `/attack` — use during combat
- `/flee` — escape combat

### Economy
- `/balance` — check orb balance
- `/shop` — view available items
- `/buy <item>` — purchase an item

### Social
- `/trade @user` — initiate a trade
- `/leaderboard` — top players by level/XP

## API Integration
- Bot calls `https://torvex.app/api/` endpoints
- Authenticated via a shared bot API key (stored in `.env`)
- Read-heavy: character stats, inventory, leaderboard
- Write: combat outcomes, purchases, trades

## Data Flow
```
Discord user runs /fight
  → bot checks link table for torvex.app user ID
  → calls POST /api/game/combat/start with user ID
  → returns monster + combat state
  → bot manages turn-by-turn flow in Discord
  → calls POST /api/game/combat/resolve with outcome
  → XP/loot written to DB via API
```

## Rollout Order
1. Account linking
2. Character view + inventory (read-only)
3. Basic combat
4. Economy (shop, balance)
5. Social (trades, leaderboard)
