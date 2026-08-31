# Command reference

Every slash command **Torvex Forerunner** exposes. Generated from the *live registered command tree* — what Discord actually has synced — plus an AST pass over the cogs, so it cannot drift from the running bot.

- **226 commands** (100 top-level, the rest subcommands) across 39 cogs
- Regenerate: `python3 tools/gen_command_docs.py`

## How to read this

Signatures use the usual convention — `<required>`, `[optional]`.

**Access** says who may run a command:

| Tier | Meaning |
|---|---|
| Everyone | No permission gate — any member. |
| Requires *permission* | Gated on a Discord permission **and enforced when the command runs**. Widening it in Server Settings → Integrations does not bypass the check. |
| Server owner only | Hard-gated to the guild owner. Cannot be delegated. |

**There are no bot-owner-only commands.** Nothing is reserved to the bot's developer — every gate is a permission your own server controls.

Anything acting in bulk requires Administrator, enforced at runtime, because `default_permissions` on its own is overridable in Integrations. A command carrying only that weaker gate is flagged inline with ⚠️.

## Index

**Security** — `/altguard-check`, `/altguard-gate`, `/altguard-lookup`, `/altguard-release`, `/altguard-sweep`, `/altguard-unwatch`, `/altguard-verify-panel`, `/altguard-watch`, `/antinuke`, `/hitlist`, `/honeypot`, `/member-activity`, `/prune-config`, `/prune-run`, `/prune-status`, `/quarantine-lock`, `/quarantine`, `/recent-leaves`, `/recon-status`, `/recon-unblock`, `/roster-missing`, `/roster-snapshot`, `/security-ai`, `/security`, `/simpleverify`, `/structure-restore`, `/structure-status`, `/unquarantine`, `/verify`

**Moderation** — `/ban`, `/clear-warning`, `/clear-warnings`, `/conduct-forget`, `/evidence`, `/kick`, `/lock`, `/msglog`, `/note`, `/prune-messages`, `/quiet-kick`, `/timeout`, `/unban`, `/unlock`, `/untimeout`, `/warn`, `/warnings`

**Server setup** — `/backup_emojis`, `/picount`, `/rolemenu`, `/server-info`, `/setup`, `/steal-emoji`, `/suggest`, `/welcome`

**Members & invites** — `/activity`, `/backfill-chat-levels`, `/balance`, `/chat-levels`, `/check-perms`, `/invite-intel`, `/invite-lockdown`, `/invite-stats`, `/invite-unlock`, `/invite`, `/levelroles`, `/notifications`, `/rank`, `/redeem`, `/rpg-leaderboard`, `/server-notifications`, `/stats-status`, `/store`, `/tracked-invite`

**Games & fun** — `/8ball`, `/challenge`, `/chess`, `/connect4_bot`, `/connect4`, `/gear`, `/gift`, `/link`, `/market`, `/peepo`, `/roast`, `/rpg`, `/tictactoe_bot`, `/tictactoe`, `/trade`, `/unlink`, `/wordle`

**AI** — `/ai-config`, `/ai-credit-grant`, `/ai-privacy`, `/ai-status`, `/ai-usage`, `/ask`

**Help** — `/add-bot`, `/dashboard`, `/help`

**Other** — `/automation`

---

## Security

### AltGuard (alt detection)

<sub>`cogs/altguard.py`</sub>

#### `/altguard-check`

Verify a member OR an ex-user (by ID) — DM them, or just generate a link

```
/altguard-check [user] [user_id] [dm] [quarantine]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | No | the member to verify (in-server) |
| `user_id` | string | No | raw Discord ID — for an ex-user who left or was banned |
| `dm` | boolean | No | True (default) = try to DM them; False = just give YOU the link |
| `quarantine` | boolean | No | True = strip their roles + hold them until they pass (in-server only) |

#### `/altguard-gate`

Forced quarantine-on-join: turn ON/OFF live (persists), or omit to check status

```
/altguard-gate [enabled]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | No | On = every new joiner is quarantined until they verify. Omit to just see the current state. |

#### `/altguard-lookup`

Inspect a user's fingerprint/verdict history and who they link to

```
/altguard-lookup [user] [user_id]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | No | member (or use user_id for someone not in the server) |
| `user_id` | string | No | raw Discord ID — for banned/left accounts not in the server |

#### `/altguard-release`

Clear a quarantine and restore removed roles — one member (+ their alt group), or everyone held

```
/altguard-release [member] [everyone]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | No | One member to release, together with their matched-alt group |
| `everyone` | boolean | No | True = release EVERY held member in this server (for switching the gate off) |

#### `/altguard-sweep`

DM every human member a verification link (failures get quarantined)

```
/altguard-sweep [dry_run]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `dry_run` | boolean | No | just count, don't DM anyone |

#### `/altguard-unwatch`

Remove an account from the watchlist

```
/altguard-unwatch <user_id>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user_id` | string | Yes | raw Discord ID to stop watching |

#### `/altguard-verify-panel`

Post the click-to-verify button in this channel (admin)

```
/altguard-verify-panel
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/altguard-watch`

Watchlist a (banned) account — loud alert if they ever verify or an alt matches them

```
/altguard-watch <user_id> [reason]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user_id` | string | Yes | raw Discord ID to watch |
| `reason` | string | No | why (e.g. 'banned raider') |

#### `/verify`

Get your own verification link

```
/verify
```

**Access:** Everyone

*No parameters.*

### Anti-nuke

<sub>`cogs/antinuke.py`</sub>

#### `/antinuke admin-lockdown`

Toggle: only owner + this bot may grant Administrator

```
/antinuke admin-lockdown <enabled>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | Yes | … |

#### `/antinuke messages-allowed`

Set the message-flood limit (optionally per channel)

```
/antinuke messages-allowed <count> <window> [channel]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `count` | integer | Yes | messages allowed *(range 1–500)* |
| `window` | integer | Yes | within this many seconds *(range 1–600)* |
| `channel` | channel | No | only this channel (omit = server-wide default) *(channel types: text, announcement)* |

#### `/antinuke role-grants`

Limit how many role-GRANTS an actor may do before it's a nuke

```
/antinuke role-grants <count> <window>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `count` | integer | Yes | role grants allowed *(range 1–100)* |
| `window` | integer | Yes | within this many seconds *(range 1–600)* |

#### `/antinuke role-removes`

Limit how many role-REMOVES an actor may do before it's a nuke

```
/antinuke role-removes <count> <window>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `count` | integer | Yes | role removals allowed *(range 1–100)* |
| `window` | integer | Yes | within this many seconds *(range 1–600)* |

#### `/antinuke set-limit`

Set the rate limit for any destructive vector

```
/antinuke set-limit <vector> <count> <window>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `vector` | string | Yes | which action *(one of: `channel deletes`, `channel creates`, `role deletes`, `role creates`, `bans`, `kicks`, `webhook creates`, `role grants`, `role removes`)* |
| `count` | integer | Yes | allowed in the window *(range 1–100)* |
| `window` | integer | Yes | within this many seconds *(range 1–600)* |

#### `/antinuke status`

Show anti-nuke mode & all thresholds

```
/antinuke status
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/antinuke unwhitelist-channel`

Re-apply flood limits to a channel

```
/antinuke unwhitelist-channel <channel>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | … *(channel types: text, announcement)* |

#### `/antinuke whitelist-channel`

Exempt a channel from flood limits (allowed to be spammed)

```
/antinuke whitelist-channel <channel>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | … *(channel types: text, announcement)* |

#### `/antinuke window-close`

End the open maintenance window now

```
/antinuke window-close
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/antinuke window-open`

Give ONE person time-boxed headroom for bulk role work

```
/antinuke window-open <user> [minutes] [reason]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | Yes | who is doing the bulk work |
| `minutes` | integer | No | 5-120 (default 30) *(range 5–120)* |
| `reason` | string | No | what they're doing — shown in the mod-log card |

### Security config

<sub>`cogs/security.py`</sub>

#### `/quarantine`

Hold a member — strip their roles and lock them out, whatever their verification status.

```
/quarantine <member> [reason] [notify]
```

**Access:** Requires **Manage Roles**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | Yes | Who to hold |
| `reason` | string | No | Why — goes in the log and in their DM |
| `notify` | boolean | No | DM them what happened and how to get out (default: yes) |

#### `/security accept-terms`

Server owner: accept the verification-gate terms for this server.

```
/security accept-terms [confirm]
```

**Access:** **Server owner only**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `confirm` | boolean | No | Yes — I've read /security terms and I accept for this server |

#### `/security audit`

Scan roles AND channel overrides for dangerous permissions.

```
/security audit
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/security disable`

Turn off anti-nuke + quarantine-lock for this server.

```
/security disable
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/security enforce`

Toggle whether anti-nuke actually acts (vs alert-only).

```
/security enforce <on>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `on` | boolean | Yes | True = act (strip/timeout/ban) · False = shadow (alert only) |

#### `/security revoke-terms`

Server owner: withdraw consent and stop the gate screening this server.

```
/security revoke-terms [confirm]
```

**Access:** **Server owner only**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `confirm` | boolean | No | Yes — withdraw consent and switch AltGuard off here |

#### `/security setup`

Enable anti-nuke + quarantine-lock for this server.

```
/security setup <modlog> <verify_channel> [quarantine_role]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `modlog` | channel | Yes | Channel for security alerts *(channel types: text, announcement)* |
| `verify_channel` | channel | Yes | The ONE channel a quarantined member can still see *(channel types: text, announcement)* |
| `quarantine_role` | role | No | Existing lockout role (leave blank to auto-create one) |

#### `/security status`

Show this server's protection settings.

```
/security status
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/security terms`

Read the verification-gate terms and see if this server accepted them.

```
/security terms
```

**Access:** Everyone

*No parameters.*

#### `/security verify-channel`

Change which channel quarantined members can still see.

```
/security verify-channel <channel>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | The one channel that stays visible (read-only) while quarantined *(channel types: text, announcement)* |

#### `/unquarantine`

Lift a hold and give back every role that was stripped.

```
/unquarantine <member> [reason]
```

**Access:** Requires **Manage Roles**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | Yes | Who to release |
| `reason` | string | No | Why — goes in the log |

### Quarantine lock

<sub>`cogs/quarantine_lock.py`</sub>

#### `/quarantine-lock`

Force-lock the quarantine role out of every channel (admin)

```
/quarantine-lock
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

### LinkGuard

<sub>`cogs/link_guard.py`</sub>

#### `/hitlist add`

Add a domain to this server's grabber/canary hitlist.

```
/hitlist add <domain>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `domain` | string | Yes | e.g. grabify.link (a bare word like 'canarytokens' matches as a substring) |

#### `/hitlist disable`

Turn off LinkGuard for this server.

```
/hitlist disable
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/hitlist enable`

Turn on LinkGuard for this server (shadow mode).

```
/hitlist enable [modlog]
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `modlog` | channel | No | Channel for LinkGuard alerts (reuses the security mod-log if set). *(channel types: text, announcement)* |

#### `/hitlist enforce`

Toggle acting (delete + timeout) vs shadow (alert-only).

```
/hitlist enforce <on>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `on` | boolean | Yes | True = delete the message + timeout the poster · False = alert only |

#### `/hitlist invites`

Invite capture: log every Discord invite posted + invite-spam response.

```
/hitlist invites [enabled] [spam_count] [spam_window_sec] [timeout_min] [exempt_channel] [allow_guild]
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | No | Capture invite links at all (on by default while LinkGuard is enabled). |
| `spam_count` | integer | No | Foreign invites inside the window that count as spam (default 3). *(range 2–20)* |
| `spam_window_sec` | integer | No | The spam window, in seconds (default 60). Counted across channels. *(range 10–600)* |
| `timeout_min` | integer | No | Timeout applied on an invite-spam trip, in minutes (default 60). *(range 1–40320)* |
| `exempt_channel` | channel | No | Toggle a channel where invites are ignored entirely (e.g. a promotions channel). *(channel types: text, announcement)* |
| `allow_guild` | string | No | Toggle a friendly server id whose invites are treated like our own. |

#### `/hitlist list`

Show LinkGuard status + this server's domain counts.

```
/hitlist list
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/hitlist remove`

Remove a domain: drop a guild-added one, or allow-list a base one.

```
/hitlist remove <domain>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `domain` | string | Yes | Domain to stop matching in this server. |

#### `/hitlist test`

Dry-run: would this text/URL trip LinkGuard?

```
/hitlist test <text>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `text` | string | Yes | Paste a message or URL — nothing is fetched, matching only. |

### Recon watch

<sub>`cogs/recon_watch.py`</sub>

#### `/recon-status`

Recent reconnaissance signals against the bot and gate.

```
/recon-status
```

**Access:** Requires **Manage Server**

*No parameters.*

#### `/recon-unblock`

Lift a gate IP block early.

```
/recon-unblock <ip>
```

**Access:** Requires **Manage Server**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `ip` | string | Yes | The IP address to unblock |

### Verify prune

<sub>`cogs/verify_prune.py`</sub>

#### `/prune-config`

Set what happens to members who never verify (admin).

```
/prune-config [enabled] [hours] [action] [enforce] [spare_clean]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | No | Off = nobody is ever auto-removed; held members just stay held |
| `hours` | integer | No | How long they get, in hours, from the moment they're held (1–8760) |
| `action` | string | No | Kick lets them rejoin and try again; ban doesn't *(one of: `Kick — they can rejoin and verify`, `Ban — permanent`)* |
| `enforce` | boolean | No | Off = only name them in the mod-log, remove nobody |
| `spare_clean` | boolean | No | Spare anyone the gate already scored clean off their link-open |

#### `/prune-run`

Run the verify-prune sweep right now (admin).

```
/prune-run
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/prune-status`

Show verify-prune config + who's currently overdue (admin).

```
/prune-status
```

**Access:** Requires **Administrator**

*No parameters.*

### Simple verify

<sub>`cogs/simpleverify.py`</sub>

#### `/simpleverify disable`

Turn simple verify off (settings are kept).

```
/simpleverify disable
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/simpleverify lockdown`

Hide every channel except the verify channel from Unverified members.

```
/simpleverify lockdown
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/simpleverify panel`

Post the Verify button in the verify channel.

```
/simpleverify panel [message]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `message` | string | No | Optional text shown above the button. |

#### `/simpleverify set-channel`

Set the channel the verify button lives in.

```
/simpleverify set-channel <channel>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | … *(channel types: text, announcement)* |

#### `/simpleverify set-roles`

Use your own existing Unverified + Verified roles.

```
/simpleverify set-roles <unverified> <verified>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `unverified` | role | Yes | Role given on join. |
| `verified` | role | Yes | Role given after verifying. |

#### `/simpleverify setup-roles`

Create/wire the Unverified + Verified roles and turn verify on.

```
/simpleverify setup-roles [verify_channel]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `verify_channel` | channel | No | Channel members verify in (the button gets posted here). *(channel types: text, announcement)* |

#### `/simpleverify status`

Show this server's verify settings.

```
/simpleverify status
```

**Access:** Requires **Administrator**

*No parameters.*

### Honeypot

<sub>`cogs/honeypot.py`</sub>

#### `/honeypot auto-delete`

Also delete the tripper's messages from the trap channel when it fires.

```
/honeypot auto-delete <enabled>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | Yes | On: sweep their messages/reaction out of the channel. Off: keep them as evidence. |

#### `/honeypot disarm`

Turn the honeypot off (settings are kept).

```
/honeypot disarm
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/honeypot log-channel`

Where honeypot trips are reported (optional).

```
/honeypot log-channel [channel]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | No | … *(channel types: text, announcement)* |

#### `/honeypot punishment`

What happens when someone trips the honeypot.

```
/honeypot punishment <action> [timeout_minutes]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `action` | string | Yes | Timeout, kick, or ban. *(one of: `Timeout — temporary mute`, `Kick — removable, can rejoin`, `Ban — permanent`)* |
| `timeout_minutes` | integer | No | For timeout only: how long (1–40320 min). Default 60. |

#### `/honeypot set-channel`

Point the honeypot at a channel and arm it. Anyone who posts/reacts is punished.

```
/honeypot set-channel <channel>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | … *(channel types: text, announcement)* |

#### `/honeypot status`

Show the honeypot settings for this server.

```
/honeypot status
```

**Access:** Requires **Administrator**

*No parameters.*

### Server backup

<sub>`cogs/server_backup.py`</sub>

#### `/member-activity`

Full join/leave/kick/ban log between snapshots (admin)

```
/member-activity [hours]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `hours` | integer | No | how far back to look (default 24) |

#### `/recent-leaves`

Recent departures — leaves, kicks, bans, with who did them (admin)

```
/recent-leaves [hours]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `hours` | integer | No | how far back to look (default 24) |

#### `/roster-missing`

Members on record who AREN'T in the server now — your re-invite list (admin)

```
/roster-missing
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/roster-snapshot`

Record the member roster right now (admin)

```
/roster-snapshot
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/structure-restore`

Recreate roles/channels deleted since the last backup (admin)

```
/structure-restore [confirm]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `confirm` | boolean | No | True = actually recreate; leave blank for a dry-run preview |

#### `/structure-status`

Show the channel/role backup + what's been deleted since (admin)

```
/structure-status
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

### Security AI (reviewed verdicts)

<sub>`cogs/security_ai.py`</sub>

#### `/security-ai grant`

[Operator] Grant a Security AI tier to a server

```
/security-ai grant <guild_id> <tier> [days] [note]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `guild_id` | string | Yes | Server to entitle |
| `tier` | string | Yes | Review tier *(one of: `Standard`, `Advanced`, `Elite`)* |
| `days` | integer | No | Days until it lapses (0 = no expiry) |
| `note` | string | No | Why — order ref, comp, trial |

#### `/security-ai revoke`

[Operator] Remove a server's Security AI tier

```
/security-ai revoke <guild_id>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `guild_id` | string | Yes | Server to revoke |

#### `/security-ai status`

Security AI tier and monthly review usage for this server

```
/security-ai status
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

*No parameters.*

---

## Moderation

### Moderation

<sub>`cogs/moderation.py`</sub>

#### `/ban`

Ban a member, or pre-ban a user by ID (not in the server).

```
/ban [user] [user_id] [reason] [delete_days]
```

**Access:** Requires **Ban Members** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | No | The member/user to ban (pick them here) |
| `user_id` | string | No | ...or a raw Discord ID — for someone not in the server |
| `reason` | string | No | Why they're being banned (shown in the audit log) |
| `delete_days` | integer | No | Delete their messages from the last N days (0–7, default 0) *(range 0–7)* |

#### `/kick`

Kick a member from the server (they can rejoin with an invite).

```
/kick <member> [reason]
```

**Access:** Requires **Kick Members** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | Yes | The member to kick |
| `reason` | string | No | Why they're being kicked (shown in the audit log) |

#### `/lock`

Lock a channel for ALL roles — nobody talks until /unlock (exempt roles to allow).

```
/lock [channel] [reason] [exempt] [exempt2] [exempt3]
```

**Access:** Requires **Manage Channels** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | No | The channel to lock (default: this one) *(channel types: text, voice, announcement, stage, forum, 16)* |
| `reason` | string | No | Why it's being locked (shown in the channel and the audit log) |
| `exempt` | role | No | A role that can still talk while locked (e.g. staff) |
| `exempt2` | role | No | Another role that can still talk |
| `exempt3` | role | No | Another role that can still talk |

#### `/prune-messages`

Bulk-delete the last N messages in this channel (count-based, not by date).

```
/prune-messages <amount>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `amount` | integer | Yes | How many recent messages to delete (1–1000). *(range 1–1000)* |

#### `/quiet-kick`

Kick a member with no goodbye message and no mod-log embed (still recorded).

```
/quiet-kick [member] [user_ids] [reason]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | No | The member to kick quietly |
| `user_ids` | string | No | Or several at once — user IDs separated by spaces or commas |
| `reason` | string | No | Why (still written to Discord's audit log and the identity ledger) |

#### `/timeout`

Time out a member (mute + no reactions) for a duration.

```
/timeout <member> [duration] [reason]
```

**Access:** Requires **Timeout Members** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | Yes | The member to time out |
| `duration` | string | No | How long — e.g. 30m, 2h, 1d, 1h30m (max 28d). Blank = this server's default. |
| `reason` | string | No | Why (shown in the audit log and the public embed) |

#### `/unban`

Unban a user by their Discord ID.

```
/unban <user_id> [reason]
```

**Access:** Requires **Ban Members** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user_id` | string | Yes | The banned user's raw Discord ID |
| `reason` | string | No | Why they're being unbanned (audit log) |

#### `/unlock`

Unlock a locked channel and restore its previous permissions.

```
/unlock [channel] [reason]
```

**Access:** Requires **Manage Channels** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | No | The channel to unlock (default: this one) *(channel types: text, voice, announcement, stage, forum, 16)* |
| `reason` | string | No | Why (audit log) |

#### `/untimeout`

Remove a member's timeout early.

```
/untimeout <member> [reason]
```

**Access:** Requires **Timeout Members** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | Yes | The timed-out member |
| `reason` | string | No | Why (audit log) |

### Mod log & message archive

<sub>`cogs/mod_log.py`</sub>

#### `/msglog accept-terms`

Manage Server: agree to the retention terms and turn the archive on.

```
/msglog accept-terms [confirm]
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `confirm` | boolean | No | Yes, I've read /msglog terms and I accept on behalf of this server |

#### `/msglog audit`

Server-change ledger: roles, channels, permissions, emoji, AutoMod rules, member roles.

```
/msglog audit [kind] [target_id] [limit]
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `kind` | string | No | Filter by kind (default: everything) *(one of: `roles (create/delete/edit)`, `channels (create/delete/edit)`, `channel permissions`, `member role changes`, `emoji`, `stickers`, `AutoMod rules`)* |
| `target_id` | string | No | Only events about this role/channel/member/emoji ID |
| `limit` | integer | No | How many (1–40, default 25) |

#### `/msglog automod`

Toggle AutoMod block + rule-change logging.

```
/msglog automod <enabled>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | Yes | On = log what AutoMod blocks and any rule changes |

#### `/msglog channels`

Toggle channel-change logging on or off.

```
/msglog channels <enabled>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | Yes | On = log channel create/delete/edit + permission-overwrite changes |

#### `/msglog commands`

Who ran which slash commands — including denied ones.

```
/msglog commands [user_id] [command] [denied] [limit]
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user_id` | string | No | Only this user's commands |
| `command` | string | No | Only this command (e.g. msglog audit) |
| `denied` | boolean | No | Only denied/failed invocations |
| `limit` | integer | No | How many (1–40, default 25) |

#### `/msglog deleted`

A user's recently deleted messages, from the archive.

```
/msglog deleted <user> [limit]
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | Yes | Whose deleted messages |
| `limit` | integer | No | How many (default 10, max 25) *(range 1–25)* |

#### `/msglog disable`

Turn off archiving + logging.

```
/msglog disable
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/msglog enable`

Turn on the message archive + mod-log.

```
/msglog enable [channel]
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | No | Where delete/edit logs go (defaults to the security mod-log) *(channel types: text, announcement)* |

#### `/msglog expressions`

Toggle emoji/sticker create/delete/edit logging on or off.

```
/msglog expressions <enabled>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | Yes | On = log custom emoji + sticker changes, with the image grabbed on deletion |

#### `/msglog forget`

Erase all identity records + stored avatars for a user ID.

```
/msglog forget <user_id>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user_id` | string | Yes | Numeric user ID to erase from the identity ledger |

#### `/msglog history`

Every recorded name, timeout and lifecycle event for a user ID.

```
/msglog history <user_id>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user_id` | string | Yes | Numeric user ID — works for users who already left or deleted their account |

#### `/msglog ignore`

Toggle a channel out of delete/edit LOGGING (still archived).

```
/msglog ignore <channel>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | … *(channel types: text, announcement)* |

#### `/msglog media-channel`

Route deleted-media re-posts to a separate channel (e.g. 18+ staff only).

```
/msglog media-channel [channel]
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | No | Destination for media re-posts; omit to attach media to the log embeds again *(channel types: text, announcement)* |

#### `/msglog names`

Toggle nickname/username/timeout logging on or off.

```
/msglog names <enabled>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | Yes | On = log nickname, username and timeout changes |

#### `/msglog pro`

Logging Pro: what this server has, what it would get.

```
/msglog pro
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/msglog pro-grant`

Operator: grant/extend/revoke Logging Pro for a server (home guild only).

```
/msglog pro-grant <guild_id> <days> [note]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `guild_id` | string | Yes | Server to grant |
| `days` | integer | Yes | Days to add from now (0 = no expiry, -1 = revoke) |
| `note` | string | No | Why (order id, comp, trial…) |

#### `/msglog revoke-terms`

Manage Server: stop storing this server's data and delete what's stored.

```
/msglog revoke-terms [confirm]
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `confirm` | boolean | No | Yes — stop retention and permanently delete this server's archive |

#### `/msglog roles`

Toggle role-change logging on or off.

```
/msglog roles <enabled>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | Yes | On = log member role add/remove + role create/delete/edit |

#### `/msglog status`

Archive totals + configuration.

```
/msglog status
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/msglog terms`

What the archive stores, and how to turn it on or off.

```
/msglog terms
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/msglog voice`

Toggle voice channel join/leave/move logging on or off.

```
/msglog voice <enabled>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `enabled` | boolean | Yes | On = log voice join/leave/switch to the mod-log |

### Conduct record

<sub>`cogs/conduct.py`</sub>

#### `/clear-warning`

Clear one entry. It stays on record as cleared.

```
/clear-warning <entry_id> <reason>
```

**Access:** Requires **Timeout Members** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `entry_id` | integer | Yes | The #id from /warnings |
| `reason` | string | Yes | Why it's being cleared |

#### `/clear-warnings`

Clear a member's whole standing record (Manage Server).

```
/clear-warnings <member> <reason>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | Yes | Whose record |
| `reason` | string | Yes | Why the whole record is being cleared |

#### `/conduct-forget`

Permanently erase a user's record AND evidence files. Cannot be undone.

```
/conduct-forget <user_id> [confirm] [everywhere]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user_id` | string | Yes | Discord user ID |
| `confirm` | boolean | No | Type true to confirm — this really deletes |
| `everywhere` | boolean | No | Erase in every server, not just this one |

#### `/evidence`

Show one record entry in full, with its stored files.

```
/evidence <entry_id>
```

**Access:** Requires **Timeout Members** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `entry_id` | integer | Yes | The #id from /warnings |

#### `/note`

Record positive or neutral conduct — resolutions, good streaks.

```
/note <member> <note> [evidence] [silent]
```

**Access:** Requires **Timeout Members** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | Yes | Who this is about |
| `note` | string | Yes | What happened |
| `evidence` | attachment | No | Screenshot or file |
| `silent` | boolean | No | Record it without DMing them |

#### `/warn`

Warn a member and record it, with optional evidence.

```
/warn <member> <reason> [evidence] [evidence2] [evidence3] [notify]
```

**Access:** Requires **Timeout Members** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | Yes | Who to warn |
| `reason` | string | Yes | What they did — this is the record |
| `evidence` | attachment | No | Screenshot or file backing this up |
| `evidence2` | attachment | No | Another file |
| `evidence3` | attachment | No | Another file |
| `notify` | string | No | How the member is told — leave empty for this server's default *(one of: `DM + ping in this channel`, `DM only`, `Ping in this channel only`, `Silent — record only, don't tell them`)* |

#### `/warnings`

View a member's conduct record — or your own.

```
/warnings [member] [show_cleared]
```

**Access:** Everyone &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `member` | user | No | Whose record (leave empty for your own) |
| `show_cleared` | boolean | No | Include entries that were cleared |

---

## Server setup

### Setup

<sub>`cogs/setup.py`</sub>

#### `/setup loot-channel`

Channel for crate opens, rare drops, and boss kill announcements.

```
/setup loot-channel <channel>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | The channel to post loot drop announcements *(channel types: text, announcement)* |

#### `/setup mod-log`

Channel for mod action logs.

```
/setup mod-log <channel>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | The channel for mod logs *(channel types: text, announcement)* |

#### `/setup rpg-channel`

Channel where RPG fight results are posted.

```
/setup rpg-channel <channel>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | The channel for RPG combat output *(channel types: text, announcement)* |

#### `/setup status-channel`

Channel for bot online/offline notices.

```
/setup status-channel <channel>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | The channel to post bot status updates *(channel types: text, announcement)* |

#### `/setup suggestions-channel`

Channel where /suggest posts land.

```
/setup suggestions-channel <channel>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | The channel for suggestions *(channel types: text, announcement)* |

#### `/setup view`

View current channel configuration for this server.

```
/setup view
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/setup welcome-channel`

Channel for new member welcome messages.

```
/setup welcome-channel <channel>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | The channel for welcome messages *(channel types: text, announcement)* |

### Automation

<sub>`cogs/automation.py`</sub>

#### `/welcome enable`

Turn join roles + welcome messages on or off.

```
/welcome enable <on>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `on` | boolean | Yes | … |

#### `/welcome status`

Show this server's join & welcome settings.

```
/welcome status
```

**Access:** Requires **Administrator**

*No parameters.*

### Reaction roles

<sub>`cogs/role_menu.py`</sub>

#### `/rolemenu addrole`

Add a role button to a panel

```
/rolemenu addrole <panel> <role> [label] [emoji]
```

**Access:** Requires **Manage Roles** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `panel` | integer | Yes | panel number |
| `role` | role | Yes | role to hand out |
| `label` | string | No | button text (defaults to role name) |
| `emoji` | string | No | any emoji — standard, or a custom one from a server I'm in |

#### `/rolemenu bootstrap`

Recreate the full MEE6 reaction-roles set as panels in a channel

```
/rolemenu bootstrap <channel>
```

**Access:** Requires **Manage Roles** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | channel to post the panels in (e.g. #reaction-roles) *(channel types: text, announcement)* |

#### `/rolemenu create`

Create an empty role panel for a channel

```
/rolemenu create <channel> <title> [description]
```

**Access:** Requires **Manage Roles** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | where the panel is posted *(channel types: text, announcement)* |
| `title` | string | Yes | panel heading |
| `description` | string | No | optional blurb |

#### `/rolemenu delete`

Delete a panel (and its message)

```
/rolemenu delete <panel>
```

**Access:** Requires **Manage Roles** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `panel` | integer | Yes | … |

#### `/rolemenu list`

List this server's role panels

```
/rolemenu list
```

**Access:** Requires **Manage Roles** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/rolemenu removerole`

Remove a role button from a panel

```
/rolemenu removerole <panel> <role>
```

**Access:** Requires **Manage Roles** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `panel` | integer | Yes | … |
| `role` | role | Yes | … |

#### `/rolemenu set-emoji`

Change (or clear) the emoji on a role button

```
/rolemenu set-emoji <panel> <role> [emoji] [label]
```

**Access:** Requires **Manage Roles** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `panel` | integer | Yes | panel number |
| `role` | role | Yes | which button |
| `emoji` | string | No | any emoji, or leave blank to remove it |
| `label` | string | No | optional: also change the button text |

#### `/rolemenu template`

Post a ready-made role panel — creates any roles you don't have yet

```
/rolemenu template <template> <channel>
```

**Access:** Requires **Manage Roles** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `template` | string | Yes | Which set to build *(one of: `🏷 Pronouns`, `🎂 Age`, `🌍 Region`, `🔔 Notifications`, `🎨 Name colour`, `🎨 Name colour — full palette`, `🏳🌈 Sexuality`, `🎮 Platform`, `💬 DM preference`)* |
| `channel` | channel | Yes | Where to post the panel *(channel types: text, announcement)* |

### Emojis

<sub>`cogs/emojis.py`</sub>

#### `/backup_emojis`

Download all server emojis and save them to the emojis/ folder on the bot host.

```
/backup_emojis
```

**Access:** Requires **Manage Expressions**

*No parameters.*

#### `/steal-emoji`

Copy custom emojis into this server — paste them (or one emoji ID) and I'll grab them.

```
/steal-emoji <emoji> [name]
```

**Access:** Requires **Manage Expressions**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `emoji` | string | Yes | Paste the emoji(s) to steal (from any server), a raw emoji ID, or a CDN emoji link |
| `name` | string | No | Rename it (only when stealing a single emoji) |

### Pi counting

<sub>`cogs/pi_count.py`</sub>

#### `/picount disable`

Stop enforcing the pi channel

```
/picount disable
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/picount recount`

Rescan the pi channel from scratch and delete invalid messages

```
/picount recount
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/picount set-channel`

Enable pi-count enforcement in a channel (rescans its history)

```
/picount set-channel <channel>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | … *(channel types: text, announcement)* |

#### `/picount status`

Pi-count status — digits so far, top contributors

```
/picount status
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

### Suggestions

<sub>`cogs/suggestions.py`</sub>

#### `/suggest`

Submit a suggestion for the server.

```
/suggest <suggestion>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `suggestion` | string | Yes | Your suggestion — be as detailed as possible |

### Server info

<sub>`cogs/server_info.py`</sub>

#### `/server-info`

Server overview — members (humans vs bots), channels, roles, features.

```
/server-info
```

**Access:** Everyone &nbsp;·&nbsp; Server only

*No parameters.*

---

## Members & invites

### Invites

<sub>`cogs/invites.py`</sub>

#### `/invite`

Get your personal tracked invite link.

```
/invite
```

**Access:** Everyone &nbsp;·&nbsp; Server only

*No parameters.*

#### `/invite-intel`

Full join dossier: invite source + device/verdict fused by uid (mod).

```
/invite-intel [user] [user_id]
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | No | Member to inspect |
| `user_id` | string | No | ...or a raw Discord ID |

#### `/invite-lockdown`

Phase 2: block native invites + purge untracked links (dry-run unless confirm).

```
/invite-lockdown [confirm]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `confirm` | boolean | No | Actually apply it. Without this it only reports what WOULD change. |

#### `/invite-stats`

Top inviters and join sources (admin).

```
/invite-stats
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/invite-unlock`

Reverse the lockdown: let members create native invites again (dry-run unless confirm).

```
/invite-unlock [confirm]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `confirm` | boolean | No | Actually apply it. Without this it only reports what WOULD change. |

#### `/tracked-invite`

Mint a labeled invite for a public source (Disboard, Reddit, a site).

```
/tracked-invite <label>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `label` | string | Yes | A source label, e.g. 'disboard', 'reddit', 'website' |

### Level roles

<sub>`cogs/level_roles.py`</sub>

#### `/levelroles import-mee6`

Import XP, levels & role rewards from MEE6's leaderboard API

```
/levelroles import-mee6 [preview] [create_missing]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `preview` | boolean | No | Show what would be imported without writing anything |
| `create_missing` | boolean | No | Recreate reward roles MEE6 references that no longer exist |

#### `/levelroles list`

Show the level → role reward map

```
/levelroles list
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/levelroles remove`

Remove the reward role for a level

```
/levelroles remove <level>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `level` | integer | Yes | … |

#### `/levelroles set`

Set the reward role for a level

```
/levelroles set <level> <role>
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `level` | integer | Yes | level threshold (e.g. 10) |
| `role` | role | Yes | role to award at that level |

#### `/levelroles sync`

Sweep all members: give each their highest Level N+ role, strip the rest

```
/levelroles sync
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

### Economy & levels

<sub>`cogs/economy.py`</sub>

#### `/backfill-chat-levels`

[Admin] Sync all chat levels to RPG XP multipliers.

```
/backfill-chat-levels
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/balance`

Check your Peepo Bucks and level.

```
/balance [user]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | No | Check another user's balance (optional) |

#### `/chat-levels`

Top 10 members by chat level, Peepo Bucks, and Regular Bucks.

```
/chat-levels [scope] [sort]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `scope` | string | No | global = all servers, local = this server only (default) *(one of: `local (this server)`, `global (all servers)`)* |
| `sort` | string | No | rank by experience (default) or Peepo Bucks *(one of: `experience level`, `peepo bucks`)* |

#### `/check-perms`

[Admin] Show and fix channels the bot can't read.

```
/check-perms [fix]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `fix` | boolean | No | Show a picker to fix channel permissions |

#### `/notifications`

Toggle level-up notifications on or off.

```
/notifications
```

**Access:** Everyone

*No parameters.*

#### `/rank`

Server rank, level, total XP, and XP needed for the next level.

```
/rank [user]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | No | Check another member's rank (optional) |

#### `/redeem`

Redeem a store item with your Peepo Bucks.

```
/redeem <item>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `item` | string | Yes | Item ID to redeem (e.g. nitro, nitro_basic, robux_800) |

#### `/rpg-leaderboard`

Top 10 Torvex RPG players by level, coins, and kills.

```
/rpg-leaderboard [scope]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `scope` | string | No | global = all servers, local = this server only (default) *(one of: `local (this server)`, `global (all servers)`)* |

#### `/server-notifications`

[Admin] Where level-up notifications go in this server — or turn them off.

```
/server-notifications [mode] [channel]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `mode` | string | No | Where announcements go. Leave blank to see the current setting. *(one of: `In the channel they were talking in`, `In one specific channel`, `Direct message to the member`, `Don't announce level-ups`)* |
| `channel` | channel | No | The channel to send them to (only used with “In one specific channel”). *(channel types: text, announcement)* |

#### `/store`

Browse the Peepo Bucks store.

```
/store
```

**Access:** Everyone

*No parameters.*

### Activity graphs

<sub>`cogs/activity.py`</sub>

#### `/activity channel`

A channel's messages per day.

```
/activity channel <channel> [days]
```

**Access:** Everyone &nbsp;·&nbsp; **Cooldown:** 10s &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `channel` | channel | Yes | Which channel *(channel types: text, announcement)* |
| `days` | integer | No | Window in days (default 30) *(range 2–3650)* |

#### `/activity growth`

Member growth — current humans vs bots by join date.

```
/activity growth
```

**Access:** Everyone &nbsp;·&nbsp; **Cooldown:** 10s &nbsp;·&nbsp; Server only

*No parameters.*

#### `/activity heatmap`

When is the server active? Hour × weekday.

```
/activity heatmap [days]
```

**Access:** Everyone &nbsp;·&nbsp; **Cooldown:** 10s &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `days` | integer | No | Window in days (default 30) *(range 2–3650)* |

#### `/activity leaderboard`

Most active members.

```
/activity leaderboard [days] [top]
```

**Access:** Everyone &nbsp;·&nbsp; **Cooldown:** 10s &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `days` | integer | No | Window in days (default 30) *(range 2–3650)* |
| `top` | integer | No | How many (default 10) *(range 3–20)* |

#### `/activity server`

Messages per day, server-wide.

```
/activity server [days]
```

**Access:** Everyone &nbsp;·&nbsp; **Cooldown:** 10s &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `days` | integer | No | Window in days (default 30) *(range 2–3650)* |

#### `/activity user`

A member's messages per day + top channels.

```
/activity user <user> [days]
```

**Access:** Everyone &nbsp;·&nbsp; **Cooldown:** 10s &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | Yes | Whose activity |
| `days` | integer | No | Window in days (default 30) *(range 2–3650)* |

#### `/activity voice`

Voice-time leaderboard (tracked since the cog deployed).

```
/activity voice [days] [top]
```

**Access:** Everyone &nbsp;·&nbsp; **Cooldown:** 10s &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `days` | integer | No | Window in days (default 30) *(range 2–3650)* |
| `top` | integer | No | How many (default 10) *(range 3–20)* |

### Stats

<sub>`cogs/stats.py`</sub>

#### `/stats-status`

Activity-tracking status — totals, date range, top channels (admin)

```
/stats-status
```

**Access:** Requires **Administrator** — ⚠️ visibility gate only, overridable in Server Settings → Integrations &nbsp;·&nbsp; Server only

*No parameters.*

---

## Games & fun

### Fun

<sub>`cogs/fun.py`</sub>

#### `/8ball`

Ask the magic 8-ball a question.

```
/8ball <question>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `question` | string | Yes | … |

#### `/connect4`

Challenge someone to Connect 4.

```
/connect4 <opponent>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `opponent` | user | Yes | … |

#### `/roast`

Roast someone. All in good fun.

```
/roast <target>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `target` | user | Yes | … |

#### `/tictactoe`

Challenge someone to tic tac toe.

```
/tictactoe <opponent>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `opponent` | user | Yes | … |

### Games vs the bot

<sub>`cogs/games.py`</sub>

#### `/connect4_bot`

Play Connect 4 against the bot.

```
/connect4_bot <difficulty>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `difficulty` | string | Yes | How hard should the bot play? *(one of: `Easy`, `Medium`, `Hard`)* |

#### `/tictactoe_bot`

Play Tic Tac Toe against the bot.

```
/tictactoe_bot <difficulty>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `difficulty` | string | Yes | How hard should the bot play? *(one of: `Easy`, `Medium`, `Hard`)* |

### RPG

<sub>`cogs/rpg.py`</sub>

#### `/link`

Link your Discord account to your Torvex account.

```
/link <torvex_username>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `torvex_username` | string | Yes | Your Torvex username (case-sensitive) |

#### `/market browse`

See all listings for a specific item, sorted by price.

```
/market browse <item>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `item` | string | Yes | Item to look up *(autocomplete)* |

#### `/market buy`

Buy the cheapest listing for an item.

```
/market buy <item>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `item` | string | Yes | Item to buy *(autocomplete)* |

#### `/market cancel`

Cancel one of your active listings and get the item back.

```
/market cancel <listing_id>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `listing_id` | string | Yes | Your listing (pick from list) *(autocomplete)* |

#### `/market list`

List one of your items for sale.

```
/market list <item> <price> [quantity]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `item` | string | Yes | Item to sell *(autocomplete)* |
| `price` | integer | Yes | Price per unit (coins) |
| `quantity` | integer | No | How many to list (default 1) |

#### `/market listings`

View your active market listings.

```
/market listings
```

**Access:** Everyone

*No parameters.*

#### `/market search`

Browse all items currently listed on the market.

```
/market search [item]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `item` | string | No | Filter by item name (optional) *(autocomplete)* |

#### `/rpg attack`

Attack during combat.

```
/rpg attack
```

**Access:** Everyone

*No parameters.*

#### `/rpg boss`

Challenge a boss encounter. Massive HP, massive rewards.

```
/rpg boss [name]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `name` | string | No | Boss name (leave blank to see all bosses) *(autocomplete)* |

#### `/rpg buy`

Buy an item from the shop.

```
/rpg buy <item> [quantity]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `item` | string | Yes | Item name *(autocomplete)* |
| `quantity` | integer | No | How many to buy (default 1) |

#### `/rpg chop`

Chop wood. Higher Woodcutting level unlocks better logs. 30s cooldown.

```
/rpg chop
```

**Access:** Everyone

*No parameters.*

#### `/rpg cook`

Cook a raw fish. Higher Cooking level reduces burn chance.

```
/rpg cook <fish>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `fish` | string | Yes | Raw fish to cook — start typing to see options *(autocomplete)* |

#### `/rpg craft`

Craft an item.

```
/rpg craft <item>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `item` | string | Yes | Item to craft *(autocomplete)* |

#### `/rpg defend`

Defend during combat (halves incoming damage).

```
/rpg defend
```

**Access:** Everyone

*No parameters.*

#### `/rpg equip`

Equip an item.

```
/rpg equip <item>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `item` | string | Yes | Item name *(autocomplete)* |

#### `/rpg fight`

Start a fight with a monster, or challenge a player to PvP.

```
/rpg fight [monster] [opponent]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `monster` | string | No | Monster name — start typing to get suggestions (leave blank for random) *(autocomplete)* |
| `opponent` | user | No | Challenge a player to PvP instead of fighting a monster |

#### `/rpg fish`

Go fishing. Higher Fishing level unlocks better fish. 30s cooldown.

```
/rpg fish
```

**Access:** Everyone

*No parameters.*

#### `/rpg flee`

Attempt to flee from combat.

```
/rpg flee
```

**Access:** Everyone

*No parameters.*

#### `/rpg help`

How to play the Torvex RPG.

```
/rpg help
```

**Access:** Everyone

*No parameters.*

#### `/rpg inventory`

View your inventory.

```
/rpg inventory
```

**Access:** Everyone

*No parameters.*

#### `/rpg item`

Use a consumable item in combat (potions, food, etc.).

```
/rpg item <item>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `item` | string | Yes | Item to use (food restores HP, potions restore HP/MP) *(autocomplete)* |

#### `/rpg magic`

Cast a spell. Support spells can target another player.

```
/rpg magic [spell] [target]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `spell` | string | No | Spell to cast (type to filter by name) *(autocomplete)* |
| `target` | user | No | Player to heal/buff (optional, defaults to yourself) |

#### `/rpg mine`

Mine for ore. Higher Mining level unlocks better ores. 30s cooldown.

```
/rpg mine
```

**Access:** Everyone

*No parameters.*

#### `/rpg recipes`

View available crafting recipes.

```
/rpg recipes
```

**Access:** Everyone

*No parameters.*

#### `/rpg sell`

Sell an item back to the shop for 45% of its value.

```
/rpg sell <item> [quantity]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `item` | string | Yes | Item to sell *(autocomplete)* |
| `quantity` | integer | No | How many to sell (default 1) |

#### `/rpg shop`

Browse the item shop.

```
/rpg shop [category] [subcategory]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `category` | string | No | Main category *(one of: `All`, `Potions`, `Food`, `Weapons`, `Armor`)* |
| `subcategory` | string | No | Subcategory (optional) *(autocomplete)* |

#### `/rpg stats`

View your (or another player's) character stats.

```
/rpg stats [user]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | No | Another member to view (optional) |

#### `/rpg unequip`

Unequip an item slot.

```
/rpg unequip <slot>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `slot` | string | Yes | Slot name (e.g. MainHand, Head, Chest) |

#### `/unlink`

Unlink your Discord account from Torvex.

```
/unlink
```

**Access:** Everyone

*No parameters.*

### PvP

<sub>`cogs/pvp.py`</sub>

#### `/challenge`

Challenge another player to a PvP battle.

```
/challenge <opponent>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `opponent` | user | Yes | The player you want to fight |

### Wordle

<sub>`cogs/wordle.py`</sub>

#### `/wordle`

Start a Wordle game in a private thread.

```
/wordle [difficulty]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `difficulty` | string | No | … *(one of: `Easy (8 guesses, common words)`, `Normal (6 guesses)`, `Hard (6 guesses, obscure words)`)* |

### Chess

<sub>`cogs/chess_cog.py`</sub>

#### `/chess`

Play chess — vs bot or challenge a player.

```
/chess [opponent] [difficulty]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `opponent` | user | No | Player to challenge. Leave blank to play vs bot. |
| `difficulty` | string | No | Bot difficulty (easy/medium/hard). Ignored for PvP. *(one of: `Easy`, `Medium`, `Hard`)* |

### Trading

<sub>`cogs/trading.py`</sub>

#### `/trade`

Offer an item/coin trade to another player.

```
/trade <recipient>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `recipient` | user | Yes | The player you want to trade with |

### Gear

<sub>`cogs/gear.py`</sub>

#### `/gear armor`

Browse all armor sets sorted by level.

```
/gear armor
```

**Access:** Everyone

*No parameters.*

#### `/gear elements`

Learn about the elemental system.

```
/gear elements
```

**Access:** Everyone

*No parameters.*

#### `/gear monsters`

Browse monsters by zone.

```
/gear monsters [zone]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `zone` | string | No | Filter by zone (Plains, Forest, Mountains, Dungeon, Volcano, Abyss) *(one of: `Plains`, `Forest`, `Mountains`, `Dungeon`, `Volcano`, `Abyss`)* |

#### `/gear weapons`

Browse all weapons sorted by level.

```
/gear weapons
```

**Access:** Everyone

*No parameters.*

### Gifts

<sub>`cogs/gifts.py`</sub>

#### `/gift coins`

Gift coins to another player.

```
/gift coins <user> <amount>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | Yes | The player to receive the coins |
| `amount` | integer | Yes | How many coins to gift |

### Peepo

<sub>`cogs/peepo.py`</sub>

#### `/peepo add`

[Admin] Add a peepo by name and image URL.

```
/peepo add <name> <url> [rarity]
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `name` | string | Yes | Peepo name (no spaces) |
| `url` | string | Yes | Direct image URL |
| `rarity` | string | No | Rarity tier (default: Common) *(one of: `Common`, `Uncommon`, `Rare`, `Epic`, `Legendary`)* |

#### `/peepo buy`

Buy a peepo from the fixed-price shop with coins.

```
/peepo buy <name>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `name` | string | Yes | Peepo name (start typing for suggestions) *(autocomplete)* |

#### `/peepo collection`

View your peepo collection (or another player's).

```
/peepo collection [user]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `user` | user | No | The user to check (leave blank for yourself) |

#### `/peepo crate`

Open a Peepo Crate for 5,000 coins — chance at any rarity including Legendary!

```
/peepo crate
```

**Access:** Everyone

*No parameters.*

#### `/peepo market browse`

Browse peepos for sale by other players.

```
/peepo market browse
```

**Access:** Everyone

*No parameters.*

#### `/peepo market buy`

Buy a listing from the marketplace.

```
/peepo market buy <listing_id>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `listing_id` | string | Yes | Listing ID (from /peepo market browse) |

#### `/peepo market cancel`

Cancel one of your active listings.

```
/peepo market cancel <listing_id>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `listing_id` | string | Yes | Listing ID to cancel (from /peepo market browse) |

#### `/peepo market list`

List one of your peepos for sale.

```
/peepo market list <peepo_name> <price>
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `peepo_name` | string | Yes | Name of the peepo to list *(autocomplete)* |
| `price` | integer | Yes | Price in coins |

#### `/peepo shop`

Browse the peepo shop — buy with RPG Coins, grouped by rarity.

```
/peepo shop
```

**Access:** Everyone

*No parameters.*

#### `/peepo sync`

[Admin] Sync server emojis to the peepo catalog.

```
/peepo sync
```

**Access:** Requires **Administrator**

*No parameters.*

#### `/peepo trade`

Offer a peepo trade to another player.

```
/peepo trade <recipient> [peepo_name] [coins]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `recipient` | user | Yes | The player to trade with |
| `peepo_name` | string | No | Peepo you're offering (leave blank for coins-only) *(autocomplete)* |
| `coins` | integer | No | Coins you're offering (default 0) |

---

## AI

### AI

<sub>`cogs/ai.py`</sub>

#### `/ai-config energy`

Daily energy per member (energy mode)

```
/ai-config energy <amount>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `amount` | integer | Yes | Energy per member per day (10–2000); 100 ≈ $0.06 of AI *(range 10–2000)* |

#### `/ai-config mode`

energy = daily allowance per member · unlimited = straight drawdown of the pool

```
/ai-config mode <mode>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `mode` | string | Yes | How members spend this server's AI *(one of: `energy — each member gets a daily allowance (recommended)`, `unlimited — no allowance; one member could use the whole pool`)* |

#### `/ai-config paid-cap`

Home only: max bucks-paid asks per member per day once energy is gone

```
/ai-config paid-cap <amount>
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `amount` | integer | Yes | Paid asks per member per day (0–100); 0 = no paid tier *(range 0–100)* |

#### `/ai-config show`

Current AI usage mode and allowance for this server

```
/ai-config show
```

**Access:** Requires **Manage Server** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/ai-credit-grant`

[Operator] Grant prepaid AI credit to a server (negative = correction)

```
/ai-credit-grant <guild_id> [usd] [note]
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `guild_id` | string | Yes | Server to credit |
| `usd` | number | No | Dollars of AI usage (0 = one pack's worth; negative = correction) |
| `note` | string | No | Why — order ref, correction, comp |

#### `/ai-privacy`

Toggle whether your messages can appear as AI context 🔒

```
/ai-privacy
```

**Access:** Everyone

*No parameters.*

#### `/ai-status`

AI spend + usage this month (admin)

```
/ai-status
```

**Access:** Requires **Administrator** &nbsp;·&nbsp; Server only

*No parameters.*

#### `/ai-usage`

Check your AI energy for today ⚡

```
/ai-usage
```

**Access:** Everyone &nbsp;·&nbsp; Server only

*No parameters.*

#### `/ask`

Ask the AI — it knows the server 🤖

```
/ask <question> [quick] [character]
```

**Access:** Everyone &nbsp;·&nbsp; **Cooldown:** 15s &nbsp;·&nbsp; Server only

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `question` | string | Yes | What do you want to know? |
| `quick` | boolean | No | Quick mode — faster and cheaper, good for simple questions |
| `character` | string | No | Who answers — the bot, a wizard, a gremlin, or the butler *(one of: `Peepo (default)`, `Grimbeard the wizard 🧙`, `Grub the gremlin 🦝`, `Reginald the butler 🎩`)* |

---

## Help

### Help

<sub>`cogs/help.py`</sub>

#### `/add-bot`

Add Torvex Forerunner to your server.

```
/add-bot
```

**Access:** Everyone

*No parameters.*

#### `/dashboard`

Open the web dashboard to configure the bot.

```
/dashboard
```

**Access:** Everyone

*No parameters.*

#### `/help`

Show all available commands, or details for one.

```
/help [command]
```

**Access:** Everyone

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `command` | string | No | A command to explain in detail (leave empty for the overview) *(autocomplete)* |

---

## Other

### auto_rules

<sub>`cogs/auto_rules.py`</sub>

#### `/automation enable`

Turn ALL automation rules on or off.

```
/automation enable <on>
```

**Access:** Requires **Administrator**

| Parameter | Type | Required | Description |
|---|---|:--:|---|
| `on` | boolean | Yes | … |

#### `/automation status`

Show this server's automation rules.

```
/automation status
```

**Access:** Requires **Administrator**

*No parameters.*

