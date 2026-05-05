# Fun Commands

## Purpose
Lightweight, engaging commands that make the server feel alive. No single feature has to be amazing — the collection keeps people coming back.

## Command List

| Command | Description | Status |
|---------|-------------|--------|
| `/roast @user` | Curated roast card deck — savage but not 18+ | In Progress |
| `/8ball <question>` | Classic magic 8-ball | In Progress |
| `/tictactoe @user` | Challenge someone, bot manages board with buttons | In Progress |
| `/coinflip` | Heads or tails | Planned |
| `/roll <dice>` | Dice roller e.g. `/roll 2d6` | Planned |
| `/trivia` | Random trivia question (Open Trivia DB, free, no key) | Planned |
| `/wouldyourather` | Curated WYR question cards | Planned |
| `/rps @user` | Rock paper scissors vs another member | Planned |
| `/poll <question> [options]` | Reaction-based poll | Planned |
| `/fact` | Random fun fact (free API) | Planned |

## Roast Deck Notes
- Hand-crafted cards — better than AI, you control the vibe
- Nothing 18+, can still be savage
- Cards stored in `data/roasts.json`
- Deck can grow over time — just add to the JSON
- See [roasts.md](roasts.md) for full card list and guidelines

## Trivia Notes
- Uses Open Trivia DB (opentdb.com) — completely free, no API key
- Categories: general, gaming, pop culture
- Timed response window (30s), first correct answer wins

## Tic Tac Toe Notes
- Uses Discord UI buttons for the board
- Challenges expire after 60s if opponent doesn't accept
- Bot enforces turn order, detects win/draw
