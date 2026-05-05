# Birthdays

## Purpose
Members register their birthday. The bot announces it in a designated channel at midnight in the member's local timezone.

## Commands
- `/birthday set <month> <day>` — register your birthday (year optional, not stored for privacy)
- `/birthday remove` — remove your birthday
- `/birthday @user` — see when someone's birthday is
- `/birthday upcoming` — list upcoming birthdays in the next 30 days
- `/birthday setChannel #channel` — set the announcement channel (staff only)

## Announcement
- Posted at midnight in the user's registered timezone
- If user has no timezone set, defaults to UTC and notes it
- Format:
```
🎂 Happy Birthday, @pgiovanni!
Hope you have a great day! 🎉
```
- Optional: role-based birthday reward (e.g. temp "Birthday" role for 24h)

## Privacy
- Only month + day stored, never year
- Users can remove their birthday at any time

## Upcoming Birthdays Display
- `/birthday upcoming` shows next 30 days sorted chronologically
- Shows names + dates, no years
- Staff can see full list; members see public view

## Integration
- Times localized via the Timezone system (see timezones/overview.md)
- Future: grant orbs on birthday via Torvex API
