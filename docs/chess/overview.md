# Chess

## Purpose
Play chess in Discord — PvP or vs bot at varying difficulty levels. Board rendered as an image each move.

## Stack
- **python-chess** — board logic, move validation, legal move generation
- **Stockfish** — bot AI engine (free, open source, installed on VPS)
- **Pillow** — render board as image and send as attachment each move

## Modes

### PvP
- `/chess @opponent` — challenge another member
- Opponent accepts via button
- Each player submits moves in algebraic notation (e.g. `e2e4`) or via `/move e2e4`
- Board image updates each turn

### vs Bot
- `/chess difficulty:easy|medium|hard`
- **Easy** — Stockfish depth 1 (random-ish, makes blunders)
- **Medium** — Stockfish depth 5 (decent, beatable)
- **Hard** — Stockfish depth 15+ (very strong, will punish mistakes)

## Commands
- `/chess @opponent` — PvP challenge
- `/chess difficulty:<easy|medium|hard>` — vs bot
- `/move <algebraic>` — submit a move (e.g. `/move e2e4`, `/move Nf3`)
- `/resign` — forfeit the game
- `/draw` — offer a draw (opponent must accept)
- `/board` — re-display the current board image

## Board Rendering
- 8x8 board rendered via Pillow with piece images
- Sent as image attachment on each move
- Highlights last move made
- Shows captured pieces, turn indicator, move history

## VPS Setup
- `apt install stockfish`
- `pip install python-chess Pillow`
- Stockfish binary path: `/usr/games/stockfish`

## Game State
- Stored in memory per guild+channel (one game per channel)
- Optional: persist to SQLite for resumable games

## Planned
- ELO tracking per user
- `/chess leaderboard` — top players by ELO
- Tournament mode (ties into Competitions feature)
