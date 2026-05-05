# Wordle

## Purpose
Play Wordle anytime with random words — not locked to a daily word.

## How It Works
- `/wordle` — start a new game, get a random 5-letter word
- 6 guesses max
- Each guess shows emoji feedback:
  - 🟩 correct letter, correct position
  - 🟨 correct letter, wrong position
  - ⬛ letter not in word
- `/guess <word>` — submit a guess (or just type in the wordle thread)

## Word List
- Stored in `data/words.json`
- Standard common 5-letter English words
- Can add custom words anytime

## Features
- One active game per user per server
- Shows keyboard state (which letters used/remaining)
- `/wordle stats` — your win rate, average guesses, streak
