# Mod Logs — full plan (rev 2026-07-05)

## Purpose
Every moderation action and message event logged to a designated channel with
full attribution — staff never guess who did what. End state: **one owned
mod-log replaces the three third-party log bots running today.**

## What we're replacing (surveyed 2026-07-05, live channel history)

| Channel | Bot | What it captures |
|---|---|---|
| `#mod-logs` | **MEE6** | member-scoped log embeds (joins/roles/etc.), no delete attribution |
| `#mod-logs-2` | **Carl-bot Logging** | role add/remove (the bulk of volume), join/leave, message deleted (no WHO), voice join/leave, name/nick changes, bans, role/channel create/delete, server updates, timeouts |
| `#mod-logs-3` | **Quark** | the gold standard: message edited (before/after), **Message Deleted vs modDelete = attributed mod deletion**, mention flag on deleted msgs, reaction removed, role given/taken, join/leave, kick/ban, voice join/leave w/ mute state, boosts, role/channel permission diffs, nickname, streams |
| `#modlogs` | Wick (dead since 6/20) | old quarantine/kick/automod-filter logs |
| `#torvex-mod-logs` | **Peepo's Reclaimer** | security suite alerts (AltGuard, LinkGuard, anti-nuke, verify-prune, recon) |

Quark's differentiators we must match: audit-log delete attribution, before/after
edits, and (ours goes further) **re-posting the actual deleted media** — Quark
only names files, because a deleted attachment's CDN URL dies with the message.

## Architecture

Foundation = a full **message archive** (`messages.db`, SQLite WAL) + a disk
**media cache** (`media_cache/`). The gateway tells you a message died, not what
it said — logging content/media requires having stored it first.

- `messages` table: one row per message (ids, author, content, attachments
  metadata json, reply ref, sticker names) + deletion columns updated in place
  (`deleted_ts`, `deleted_by`, `delete_kind` self|mod|bulk|unknown).
- `edits` table: full before/after history per edit.
- Writes batched in memory, flushed every 30 s (stats.py pattern); a capped
  in-memory `recent` map gives instant lookups for delete/edit events.
- Media ≤ 25 MB/file cached on arrival; pruned after `msglog_media_days` (30 d
  default — re-posts made into the log channel persist in Discord anyway).
- Storage math: 1.25 M messages ≈ 300–600 MB; box has 37 GB free. Growth ≈
  0.5 GB/yr at ~1 M msgs/yr. Media cache is retention-bounded.
- Backfill: `backfill_history.py` (REST-only, adapted from the LinkGuard audit
  crawler) imports the entire pre-cog history; `INSERT OR IGNORE` = resumable.

### WHO-deleted-it attribution (the Quark trick)
Self-deletes never appear in the audit log; mod deletes do — but Discord
**aggregates** them: a mod deleting another message by the same author in the
same channel within ~5 min bumps `count` on the existing entry instead of
creating a new one. So the cog:
1. primes an `{entry_id: count}` cache per guild at startup,
2. on a delete event waits ~1.3 s (audit lag), fetches recent
   `message_delete` entries,
3. attributes iff a **fresh new entry** or a **count increase** matches the
   deleted message's channel + author; otherwise → self-delete.
Bulk deletes use the same logic against `message_bulk_delete` (channel + count).
Matcher is a pure function, unit-tested (`tests/test_mod_log.py`).

### Known edges (accepted for phase 1)
- Ban with `delete_days` cascades deletions with no message_delete audit
  entries → logged as "unattributed (possibly ban cascade)". Phase 2: correlate
  with a fresh ban entry.
- `/prune-messages` (our own purge) attributes to the bot, not the invoking
  mod. Phase 2: moderation cog stashes invoker for the bulk handler to claim.
- Messages older than the archive log as "not in the archive" until the
  backfill completes.

## Phases

### Phase 1 — message layer (BUILT, this cog: `cogs/mod_log.py`)
- [x] Archive every message + media cache
- [x] Message deleted — content, author, channel, sent-time, mention flag,
      **deleted-by attribution**, **cached media re-posted** into the log
- [x] Message edited — before/after, jump link, edit history table
      (embed-unfurl MESSAGE_UPDATEs filtered by content-equality — the
      LinkGuard FP lesson)
- [x] Bulk delete — chronological transcript .txt from the archive,
      attribution, uncached-count called out
- [x] `/msglog enable|disable|channel-ignore|status|deleted` (Manage Server);
      `deleted user:` = a user's recent deletions from the archive
- [x] Per-guild opt-in via `msglog_*` keys in security_config; log channel
      falls back to `modlog_channel_id`
- [x] Backfill script

### Phase 2 — member/role/channel layer (Carl-bot/Quark parity)
Emit embeds for what `server_backup` already records in DB (join/leave/kick/ban
via audit log, roster) plus: role given/taken (with who via audit log), role &
channel create/delete/permission diffs (audit log), nickname/username changes,
timeout add/remove, unban, voice join/leave/move, boosts, reaction-clear.
Individual reaction-remove = optional (Quark's noisiest, low value).
Design: same cog family, one `member_log.py` / reuse server_backup events;
same audit-entry cache infrastructure (generalize `match_delete_entry`).

### Phase 3 — moderation-action cases + surfaces
- Bot-issued warn/mute/kick/ban → numbered cases (Carl-bot `case #142` style),
  reasons, `/case` lookup, per-user modlog dossier fusing archive +
  `linkguard.db` trips + AltGuard verification (`/invite-intel` pattern).
- Archive-powered analytics feed statbot-parity graphs (message counts already
  derivable from `messages`; stats.db stays as the cheap counter).
- Retire MEE6 `#mod-logs`, Carl-bot `#mod-logs-2`, Quark `#mod-logs-3` once
  parity is verified side-by-side.

## Rollout
1. Deploy cog → enable pointed at `#mod-logs-3` (Quark's channel) so every
   event can be compared 1:1 against Quark's embed until trusted.
2. Run backfill (few hours, REST, rate-limit-aware).
3. After a week of parity: move to a dedicated `#message-logs`, keep
   `#torvex-mod-logs` for security alerts (message logs are high-volume and
   would drown them), build Phase 2, then retire the three bots.

## Log format
Embeds: orange = self-delete, dark red = mod-delete/bulk, blue = edit; footer =
message id; content replayed with `AllowedMentions.none()` so replays never
ping; deleted media re-attached as real files (≤ guild upload limit, ≤ 9/msg).
