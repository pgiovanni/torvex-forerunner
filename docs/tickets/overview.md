# Tickets

## Purpose
Users submit support requests in a designated channel. The bot creates a private ticket channel for each request, visible only to the user and staff.

## Flow
1. User posts in `#general-tickets`
2. Bot creates a private channel under the tickets category (e.g. `ticket-0042`)
3. Staff role is pinged in the new channel with the request content
4. User and staff communicate in the private channel
5. Staff closes with `/close` when resolved

## Channel Naming
- Format: `ticket-{number:04d}` (e.g. `ticket-0001`, `ticket-0042`)
- Number is based on count of existing ticket channels in the category

## Permissions on Ticket Channel
- `@everyone` — no access
- Ticket author — read + send
- Staff role — read + send

## Commands
- `/close` — close ticket (staff only), deletes the channel
- `/adduser @user` — add another user to the ticket
- `/removeuser @user` — remove a user from the ticket

## Planned
- Transcript saved to a log channel before close
- Ticket categories (Bug Report, General, Trust & Safety, etc.)
- `/ticket create` as an alternative to posting in the channel
