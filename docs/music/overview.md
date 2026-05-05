# Music

## Purpose
Stream audio in voice channels with a clean persistent control panel — no typing commands mid-song.

## Stack
- **yt-dlp** — audio extraction from YouTube, SoundCloud
- **FFmpeg** — audio encoding/streaming to Discord
- **discord.py voice** — voice channel connection
- **Spotify API** (optional) — search by track name, resolve to yt-dlp query

## Features

### Playback
- `/play <query or URL>` — search YouTube or paste a direct URL
- `/pause` — pause current track
- `/resume` — resume
- `/skip` — skip to next in queue
- `/stop` — stop and disconnect
- `/volume <0-100>` — set volume

### Queue
- `/queue` — show current queue
- `/shuffle` — shuffle queue
- `/remove <position>` — remove a track from queue
- `/clear` — clear the queue

### Control Panel
- Persistent embed posted in a designated music channel
- Buttons: ⏮ Previous | ⏸ Pause/Resume | ⏭ Skip | ⏹ Stop | 🔀 Shuffle
- Shows current track, thumbnail, duration, requester
- Updates live as tracks change

### Setup
- `/music setchannel #channel` — designate the music control panel channel
- Panel auto-posts on first `/play`

## Notes
- yt-dlp must be installed on VPS: `pip install yt-dlp`
- FFmpeg must be installed: `apt install ffmpeg`
- Voice streaming is CPU/bandwidth heavy — fine for small servers on current VPS
- YouTube may throttle — SoundCloud is a reliable fallback
- Spotify integration: use Spotify API for search metadata only, yt-dlp for actual audio
