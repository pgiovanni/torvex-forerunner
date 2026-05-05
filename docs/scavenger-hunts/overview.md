# Scavenger Hunts

## Purpose
Multi-stage, clue-based events where members solve puzzles to progress through a chain. Each clue leads to the next. First to finish wins.

## How It Works
1. Staff creates a hunt with a series of clues
2. Bot DMs or posts the first clue to participants
3. Members submit answers
4. Correct answer unlocks the next clue
5. First member to complete all stages wins

## Clue Types

| Type | How It Works |
|------|-------------|
| Text answer | Member types the answer in a designated channel or DM |
| Channel find | Clue points to a hidden message somewhere in the server |
| Reaction | Member must react to a specific message with a specific emoji |
| Image | Staff uploads an image clue, member deciphers it |
| Riddle | Text riddle, member submits answer |

## Commands
- `/hunt create name:"Summer Hunt"` — start building a hunt
- `/hunt addclue <hunt_id> clue:"What has keys but no locks?" answer:"keyboard" type:text`
- `/hunt start <hunt_id>` — begin the hunt, post first clue
- `/hunt status <hunt_id>` — see who's on what stage
- `/hunt end <hunt_id>` — end hunt and announce winner
- `/hunt hint <hunt_id>` — post a hint for the current stage (staff triggered)

## Member Flow
- Members join via `/hunt join <hunt_id>` or a join button in the start embed
- Each member tracks their own progress independently
- Answers submitted via `/answer <text>` in a designated hunt channel or DM

## Leaderboard
- Tracks who completed how many stages and time taken
- Final standings posted when hunt ends

## Future
- Tie completion into orb rewards via Torvex API
- Cross-channel clue placement (bot posts clues in random channels)
- Timed stages with countdowns
