# Bot Security

## Token Security
- Token stored in `.env` only — never hardcoded, never committed
- `.env` is in `.gitignore`
- If token is ever exposed (posted in chat, committed to git), reset it immediately at discord.com/developers/applications
- On VPS, `.env` readable only by the bot's service user

## Command Authorization
- All sensitive commands use `@app_commands.checks.has_permissions()` or role checks
- No command trusts user-supplied IDs blindly — always resolve against guild objects
- Staff-only commands verify role at runtime, not just on registration

## Input Validation
- All user-supplied strings are capped in length before processing or storing
- No eval, no exec, no shell commands triggered by user input
- Slash command options are typed — Discord enforces types before the bot even sees them

## Rate Limiting
- Per-user cooldowns on fun commands to prevent spam
  - `/roast` — 10 second cooldown per user
  - `/8ball` — 5 second cooldown per user
  - `/tictactoe` — one active game per user at a time
- Implemented via `@app_commands.checks.cooldown()`

## Dependency Security
- `requirements.txt` pins major versions
- Regularly run `pip list --outdated` to check for updates
- Never install packages from untrusted sources

## VPS Hardening
- Bot runs as a non-root user (`botuser`) with minimal permissions
- `.env` file permissions: `chmod 600 .env`
- No ports exposed — bot only makes outbound connections to Discord API
- systemd service runs as dedicated user, not www-data or root

## Guild Isolation
- All data is scoped per guild ID
- No cross-guild data leakage possible
- Config, heat scores, watchlists, birthdays — all keyed by guild_id + user_id

## Logging
- All command invocations logged locally (who ran what, when)
- Errors logged with context but without exposing tokens or secrets
- No user message content logged to disk unless explicitly part of a mod-log feature

## What We Don't Do
- No storing message content beyond mod-log purposes
- No tracking user activity outside the server
- No selling or sharing data
