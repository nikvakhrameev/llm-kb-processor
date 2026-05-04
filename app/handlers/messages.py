"""Telegram message handlers — type detection, download, intake.

Each handler detects the resource type, downloads attachments if needed,
inserts a row in SQLite, and replies with a "queued" ack.
"""

from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Router
from aiogram.types import Message

from app.db import Database
from app.enums import ResourceType
from app.settings import settings
from app.utils import extract_urls, is_youtube_url, uuid_str

router = Router()

MIN_TEXT_LEN = 50
UNSUPPORTED_REPLY = (
    "Unsupported message type. I accept: PDF, MD, web URLs, "
    "YouTube URLs, and text (50+ chars)."
)

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "youtu.be", "music.youtube.com",
}


def detect_type(message: Message) -> ResourceType | None:
    """Detect the resource type from an incoming Telegram message.

    Order of checks: document → text with URLs → plain text. First match wins.
    """
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
            if is_youtube_url(url):
                return ResourceType.YOUTUBE
            return ResourceType.WEB
        if len(urls) > 1:
            return None  # ambiguous — ask user to send one URL per message
        if len(message.text.strip()) >= MIN_TEXT_LEN:
            return ResourceType.TEXT
        return None  # too short

    return None  # photo, sticker, video, audio, etc.


async def _download(message: Message, resource_id: str, bot: Bot) -> str | None:
    """Download an attachment. Returns the relative path (from kb_root) or None."""
    if message.document:
        file = await bot.get_file(message.document.file_id)
        original_name = message.document.file_name or ""
        ext = Path(original_name).suffix.lower() or ".bin"
    else:
        return None

    dest_dir = settings.kb_root / "raw" / "inbox"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{resource_id}{ext}"

    await bot.download_file(file.file_path, destination=dest)
    return str(dest.relative_to(settings.kb_root))


async def _insert_and_reply(
    message: Message,
    rtype: ResourceType,
    *,
    resource_id: str | None = None,
    source_url: str | None = None,
    original_file_path: str | None = None,
    inline_text: str | None = None,
) -> None:
    """Insert a resource row and reply with ack."""
    db = Database(settings.state_db)
    db.connect()
    try:
        rid = resource_id or uuid_str()
        db.insert_resource(
            id=rid,
            resource_type=rtype,
            telegram_chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            telegram_user_id=message.from_user.id,
            source_url=source_url,
            original_file_path=original_file_path,
            inline_text=inline_text,
        )
        short = rid[:8]
        await message.reply(f"queued for processing (id `{short}`)", parse_mode="Markdown")
    finally:
        db.close()


# ------------------------------------------------------------------
# Documents (PDF, MD)
# ------------------------------------------------------------------

@router.message(lambda msg: msg.document is not None)
async def on_document(message: Message, bot: Bot) -> None:
    rtype = detect_type(message)
    if rtype is None:
        await message.reply(UNSUPPORTED_REPLY)
        return
    rid = uuid_str()
    try:
        path = await _download(message, rid, bot)
    except Exception as e:
        await message.reply(f"Could not download attachment: {e}")
        return
    await _insert_and_reply(message, rtype, resource_id=rid, original_file_path=path)


# ------------------------------------------------------------------
# Text (URLs, web, text)
# ------------------------------------------------------------------

@router.message(lambda msg: msg.text is not None)
async def on_text(message: Message) -> None:
    rtype = detect_type(message)
    if rtype is None:
        if len(message.text.strip()) < MIN_TEXT_LEN:
            await message.reply(f"Text is too short (min {MIN_TEXT_LEN} chars)")
        elif len(extract_urls(message.text)) > 1:
            await message.reply("Send one link per message please")
        else:
            await message.reply(UNSUPPORTED_REPLY)
        return

    if rtype in (ResourceType.WEB, ResourceType.YOUTUBE):
        urls = extract_urls(message.text)
        await _insert_and_reply(message, rtype, source_url=urls[0])
    elif rtype == ResourceType.TEXT:
        await _insert_and_reply(message, rtype, inline_text=message.text.strip())


# ------------------------------------------------------------------
# Catchall (photos, stickers, videos, etc.)
# ------------------------------------------------------------------

@router.message()
async def on_unsupported(message: Message) -> None:
    await message.reply(UNSUPPORTED_REPLY)
