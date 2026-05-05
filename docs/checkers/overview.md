# Checkers

## Purpose
Play checkers in Discord — PvP or vs bot. Simpler than chess, board rendered as emoji grid or image.

## Stack
- Custom checkers logic (no external engine needed — minimax is sufficient)
- **Pillow** — optional image rendering (can also use emoji grid)
- Minimax with alpha-beta pruning for bot AI

## Modes

### PvP
- `/checkers @opponent` — challenge another member
- Opponent accepts via button
- Moves submitted via `/move <from> <to>` (e.g. `/move a3 b4`)

### vs Bot
- `/checkers difficulty:easy|medium|hard`
- **Easy** — random legal moves
- **Medium** — minimax depth 3
- **Hard** — minimax depth 7 with alpha-beta pruning, very strong

## Rules
- Standard 8x8 American checkers
- Mandatory jumps enforced
- King promotion on back row
- Multi-jump chains supported

## Commands
- `/checkers @opponent` — PvP challenge
- `/checkers difficulty:<easy|medium|hard>` — vs bot
- `/move <from> <to>` — submit a move
- `/resign` — forfeit
- `/board` — re-display current board

## Board Rendering

### Option A — Emoji Grid
```
⬛🔴⬛🔴⬛🔴⬛🔴
🔴⬛🔴⬛🔴⬛🔴⬛
⬛🔴⬛🔴⬛🔴⬛🔴
⬜⬛⬜⬛⬜⬛⬜⬛
⬛⬜⬛⬜⬛⬜⬛⬜
⚫⬛⚫⬛⚫⬛⚫⬛
⬛⚫⬛⚫⬛⚫⬛⚫
⚫⬛⚫⬛⚫⬛⚫⬛
```
- Fast, no dependencies
- Less visual but works fine for small boards

### Option B — Image (Pillow)
- Cleaner, matches chess rendering style
- More work but better UX

## VPS Setup
- `pip install Pillow` (if using image rendering)
- No external engine needed

## Game State
- Stored in memory per guild+channel
- One game per channel

## Planned
- Win/loss stats per user
- Ties into Competitions/Tournament feature
