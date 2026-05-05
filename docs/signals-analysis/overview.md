# Discord Signals & Pruning Analysis

## Purpose
Discord emits a rich stream of gateway events. Most bots only listen to messages and member joins. We tap the full signal surface — presence changes, member chunk requests, prune events, voice state changes — and feed them into the moderation pipeline (heat system, alt detection, anti-nuke).

## Gateway Events We Monitor

### Member Lifecycle
| Event | Signal Use |
|-------|-----------|
| `on_member_join` | Account age check, alt score, watchlist lookup |
| `on_member_remove` | Was this a kick? Check audit log. Log it. |
| `on_member_ban` | Log + remove from heat tracking |
| `on_member_unban` | Log, optionally restore heat score |
| `on_member_update` | Role change, nickname change — log if suspicious |

### Presence & Activity
| Event | Signal Use |
|-------|-----------|
| `on_presence_update` | Status changes — not logged by default, used for activity pattern analysis |
| `on_voice_state_update` | Voice join/leave — used for alt behavior pattern (join voice immediately on alt) |

### Message Events
| Event | Signal Use |
|-------|-----------|
| `on_message` | Spam detection, heat contribution |
| `on_message_delete` | Audit log attribution, mod log |
| `on_message_edit` | Log before/after |
| `on_bulk_message_delete` | Flag as potential nuke signal |
| `on_raw_message_delete` | Catches deletes not in cache |
| `on_raw_bulk_message_delete` | Same, uncached |

### Server Structure Events
| Event | Signal Use |
|-------|-----------|
| `on_guild_channel_create` | Anti-nuke counter |
| `on_guild_channel_delete` | Anti-nuke counter |
| `on_guild_role_create` | Anti-nuke counter |
| `on_guild_role_delete` | Anti-nuke counter |
| `on_webhooks_update` | Webhook abuse detection |
| `on_guild_update` | Server settings changed — log it |

### Prune Events
| Event | Signal Use |
|-------|-----------|
| `on_guild_integrations_update` | Integration added/removed — log |
| Member pruning (`guild.prune_members`) | Bot can request prune counts; analyze who would be pruned (inactive members) before executing |

## Pruning Analysis
Discord's member prune removes members inactive for N days with no roles.

Before any prune:
1. Fetch prune count (`guild.estimate_pruned_members(days=30)`)
2. Display breakdown to staff: how many would be removed, roles affected
3. Staff confirms via `/prune dry-run` before executing `/prune execute`
4. Log full prune: count removed, who authorized, timestamp

## Raw Events vs Cached Events
- discord.py caches recent messages/members but cache has limits
- Use `on_raw_*` variants to catch events for uncached objects (older messages, members who left)
- Always prefer raw events for mod-log reliability

## Signal Aggregation
All events feed into a central `SignalBus`:
- Each event emits a typed signal with metadata
- SignalBus routes to: heat system, alt detector, anti-nuke monitor, mod log
- This keeps cogs decoupled — no cog directly calls another

## Intents Required
```python
intents = discord.Intents.default()
intents.message_content = True   # message content
intents.members = True            # member join/leave/update
intents.presences = True          # presence updates (optional, requires verification for large bots)
intents.guild_messages = True
intents.dm_messages = True
```
Note: `presences` intent requires bot verification for servers >100 members.
