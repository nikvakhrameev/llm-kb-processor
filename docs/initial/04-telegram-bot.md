# Telegram Bot

## Mode

aiogram 3.x in **long-polling** mode (no webhook). This avoids needing a
public hostname or TLS certificate on the VPS and keeps the bot working
behind any NAT.

## Authentication and access control

```
ALLOWED_USER_IDS = [123456789, ...]   # from environment, comma-separated
OWNER_CHAT_ID    = 123456789          # for unsolicited notifications (lint, synthesis)
```

Every incoming update goes through an `auth` middleware:

```python
async def auth_middleware(handler, event, data):
    user = event.from_user
    if user is None or user.id not in settings.allowed_user_ids:
        return  # silent drop, no reply
    return await handler(event, data)
```

Silent drop, not an error reply: we do not want strangers to discover that
this is a private bot.

## Type detection

Detection is performed in this order. The first match wins.

```python
def detect_type(message: Message) -> ResourceType | None:
    if message.voice is not None:
        return ResourceType.VOICE

    if message.document is not None:
        mime = message.document.mime_type or ""
        name = (message.document.file_name or "").lower()
        if mime == "application/pdf" or name.endswith(".pdf"):
            return ResourceType.PDF
        if name.endswith(".md") or name.endswith(".markdown"):
            return ResourceType.MD
        return None  # unsupported document type

    if message.text:
        urls = extract_urls(message.text)
        if len(urls) == 1:
            url = urls[0]
            if is_youtube(url):
                return ResourceType.YOUTUBE
            return ResourceType.WEB
        if len(urls) > 1:
            return None  # ambiguous: ask user to send one URL per message
        # plain text, no URLs
        if len(message.text.strip()) >= MIN_TEXT_LEN:  # default 50
            return ResourceType.TEXT
        return None  # too short, probably accidental

    return None  # photo, sticker, video, audio file, etc.
```

### URL extraction

Use a strict-but-tolerant regex on the raw `message.text`. Telegram-formatted
links via `text_link` MessageEntity are handled by reading `message.entities`
and combining with the regex hits, then de-duplicating.

```python
URL_RE = re.compile(r'https?://[^\s)>\]]+', re.IGNORECASE)
```

Strip trailing punctuation `.,;:!?` from each match.

### YouTube detection

```python
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
                 "youtu.be", "music.youtube.com"}

def is_youtube(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host.lower() in YOUTUBE_HOSTS
```

## Handlers

Registered in this order (aiogram dispatches to first matching):

1. `/start`, `/help` — print supported types and current allowlist status.
2. `/status [resource_id]` — print last 10 resources or one by id.
3. Voice messages — handled by `on_voice`.
4. Documents — handled by `on_document`.
5. Plain text — handled by `on_text`.
6. Catchall — handled by `on_unsupported`.

### Common handler structure

Every type handler does the same five things:

```python
async def on_<type>(message: Message, db: DB):
    rtype = detect_type(message)
    if rtype is None:
        await message.reply(UNSUPPORTED_REPLY)
        return

    resource_id = uuid7()  # time-ordered UUID
    original_path = None
    inline_text = None
    source_url = None

    try:
        if rtype in (ResourceType.PDF, ResourceType.MD, ResourceType.VOICE):
            original_path = await download_attachment(message, resource_id)
        elif rtype in (ResourceType.WEB, ResourceType.YOUTUBE):
            source_url = first_url(message.text)
        elif rtype == ResourceType.TEXT:
            inline_text = message.text.strip()
    except DownloadError as e:
        await message.reply(f"❌ Could not download attachment: {e}")
        return

    db.insert_resource(
        id=resource_id,
        resource_type=rtype,
        status="received",
        telegram_chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        telegram_user_id=message.from_user.id,
        source_url=source_url,
        original_file_path=original_path,
        inline_text=inline_text,
    )

    short = resource_id[:8]
    await message.reply(f"📥 Queued for processing (id `{short}`)",
                        parse_mode="Markdown")
```

## Attachment download

Files (PDF, MD, voice) are downloaded using the Telegram Bot API's
`getFile` + the file URL. Maximum file size accepted by the bot is 20 MB
(Telegram Bot API limit; for larger files, the user should send via a link).

Downloaded path: `raw/inbox/<resource_id>.<ext>` where `<ext>` is derived
from the document's `file_name` extension or, for voice, always `.ogg`.

```python
async def download_attachment(message: Message, resource_id: str) -> Path:
    if message.voice:
        file = await bot.get_file(message.voice.file_id)
        ext = ".ogg"
    else:
        file = await bot.get_file(message.document.file_id)
        original_name = message.document.file_name or ""
        ext = Path(original_name).suffix.lower() or ".bin"

    dest = settings.kb_root / "raw" / "inbox" / f"{resource_id}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    await bot.download_file(file.file_path, destination=dest)
    return dest.relative_to(settings.kb_root)
```

The path stored in `original_file_path` is **relative to `kb_root`** so
moving the directory does not break references.

## Reply messages

| Situation                          | Reply text                                                            |
|------------------------------------|-----------------------------------------------------------------------|
| Accepted                           | `📥 Queued for processing (id <short>)`                              |
| Unsupported type                   | `❌ Unsupported. I accept: PDF, MD, web URLs, YouTube URLs, text, voice` |
| Too-short text                     | `❌ Text is too short (min 50 chars)`                                 |
| Multiple URLs in one message       | `❌ Send one link per message please`                                 |
| Download failed                    | `❌ Could not download attachment: <reason>`                          |
| Quality gate rejected (later)      | `🚫 Skipped (low quality, score=<n>): <rationale>`                    |
| Ingest done (later, see `notifier`) | `✅ Ingested. +<a> pages, <b> updates. Topics: <topic-list>`          |
| Ingest failed (later)              | `❌ Ingest failed: <last-error>. Will retry.` then on final fail: `❌ Ingest failed permanently: <reason>` |

All replies use `reply_to_message_id` set to the original message id so the
conversation stays threaded.

## Commands

### `/start` and `/help`

Static text describing what the bot accepts and how it works.

### `/status`

`/status` (no argument) — show last 10 resources from this user, one per
line, with id, type, status, age.

`/status <resource_id_short>` — show one resource: full status, error if any,
notification status, link to wiki source page if `done`.

```python
@router.message(Command("status"))
async def cmd_status(message: Message, command: CommandObject):
    if command.args:
        rid = await db.find_by_short_id(command.args.strip())
        if not rid:
            await message.reply("Not found.")
            return
        await message.reply(format_one_status(rid))
    else:
        rows = await db.recent_for_user(message.from_user.id, limit=10)
        await message.reply(format_status_list(rows))
```

## Concurrency

aiogram's default dispatcher is async and handles concurrent updates. The
SQLite insert is serialized by SQLite's WAL mode; even at burst loads of
several messages per second, this is not a bottleneck.

The bot **does not** wait for processing to complete. Every handler returns
within seconds of receiving the message. The user gets a "queued" reply
immediately and a "done" or "failed" reply later when the workers complete.

## Logging

Each handler logs at INFO level: timestamp, user_id, message_id, detected
type, resource_id, outcome. Errors at ERROR level with stack trace. No
message content is logged (privacy + telegram message text can be large).
