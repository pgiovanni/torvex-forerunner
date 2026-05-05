# Competitions

## Purpose
Run structured competitions in the server. Staff creates a competition, members participate, bot tracks entries and picks winners.

## Competition Types

### 1. Art / Submission Competition
- Members submit entries (image, text, link) in a designated channel
- Staff reviews and picks winner, or community votes via reactions
- `/comp create art name:"Summer Art Jam" channel:#submissions deadline:"2026-06-01"`

### 2. Trivia
- Bot posts questions one at a time in a channel
- First to answer correctly wins the round
- Points accumulated over rounds, leaderboard at end
- `/comp create trivia name:"Game Night" rounds:10 channel:#trivia`

### 3. Points Race
- Members earn points via specific actions (messages, reactions, event participation)
- Leaderboard tracked over competition duration
- Winner at end of period
- `/comp create race name:"May Grind" duration:7d channel:#race-leaderboard`

### 4. Custom / Manual
- Staff manages scoring manually
- Bot tracks and displays leaderboard
- `/comp create custom name:"Speedrun Contest"`
- `/comp score @user 50` — add points manually

## General Commands
- `/comp list` — active competitions
- `/comp end <id>` — end and announce winner
- `/comp leaderboard <id>` — show current standings
- `/comp enter <id>` — enter a competition (if entry-based)

## Prizes
- Announced in the competition embed
- Bot does not automatically grant prizes — staff handles fulfilment
- Future: tie into orb system for automatic orb prizes (Torvex Lescala integration)
