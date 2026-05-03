# CLAUDE.md — app/handlers/

## Purpose

Telegram bot message handlers. Registered on the aiogram Dispatcher in `app/bot.py`.

## Files

| File          | Purpose |
|---------------|---------|
| `start.py`    | `/start` and `/help` — show supported types and usage |
| `status.py`   | `/status` (last 10) and `/status <id>` (detail) |
| `messages.py` | Type detection, attachment download, intake handlers, catchall |

## Handler registration order (first match wins)

1. `/start`, `/help` — Command filters
2. `/status [id]` — Command filter with optional args
3. Voice messages — `lambda msg: msg.voice is not None`
4. Documents — `lambda msg: msg.document is not None`
5. Text messages — `lambda msg: msg.text is not None`
6. Catchall — everything else (photos, stickers, videos, etc.)

## Type detection (`detect_type`)

Checks in order, returns `ResourceType` or `None`:

1. `message.voice` → `VOICE`
2. `message.document` with PDF mime/ext → `PDF`, with MD ext → `MD`, else `None`
3. `message.text` with 1 URL → `YOUTUBE` (if youtube host) or `WEB`
4. `message.text` with multiple URLs → `None` (ambiguous)
5. `message.text` ≥ 50 chars, no URLs → `TEXT`
6. Everything else → `None`

## Intake pattern

Every successful handler does:
1. Detect type
2. Download attachment if needed (to `raw/inbox/<uuid>.<ext>`)
3. Insert `resources` row with `status='received'` + Telegram metadata
4. Reply with "queued (id `<short>`)" ack

The bot never calls LLMs or parsers. It's pure intake.

## Attachment download

- Files up to 20 MB (Telegram Bot API limit)
- Saved to `kb_root / raw / inbox / <resource_id>.<ext>`
- Path stored as **relative** to `kb_root` in `original_file_path`
- Voice messages always `.ogg`

## Auth middleware

`auth_middleware` in `app/bot.py` — silent drop for users not in `ALLOWED_USER_IDS`.
No error reply (don't reveal this is a private bot).
