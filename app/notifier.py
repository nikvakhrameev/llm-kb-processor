"""Notifier — sends terminal status notifications.

In MVP, logs to stdout. When Telegram bot token is configured, sends
real Telegram messages. The interface is intentionally simple so the
upgrade path is just setting an env var.
"""

import asyncio

from app.db import Database
from app.settings import settings

_bot = None


def _get_bot():
    """Lazy-init the aiogram Bot for notifications."""
    global _bot
    if _bot is None and settings.telegram_bot_token:
        from aiogram import Bot
        _bot = Bot(token=settings.telegram_bot_token)
    return _bot


async def notify_terminal(db: Database, resource_id: str) -> None:
    """Notify about a terminal status change. Idempotent via notification_sent_at."""
    row = db.fetchone(
        """SELECT id, status, notification_sent_at, content_title,
                  quality_score, quality_rationale,
                  ingest_summary, error_message,
                  telegram_chat_id, telegram_message_id,
                  resource_type
           FROM resources WHERE id = ?""",
        (resource_id,),
    )
    if row is None or row["notification_sent_at"] is not None:
        return

    status = row["status"]
    title = row["content_title"] or "(untitled)"
    short_id = resource_id[:8]

    if status == "done":
        import json
        try:
            summary_data = json.loads(row["ingest_summary"] or "{}")
        except json.JSONDecodeError:
            summary_data = {}
        created = len(summary_data.get("pages_created", []))
        updated = len(summary_data.get("pages_updated", []))
        warnings = summary_data.get("warnings", [])
        if warnings:
            warn_lines = "\n".join(f"  • {w}" for w in warnings)
            warn_suffix = f"\n  {len(warnings)} warning(s):\n{warn_lines}"
        else:
            warn_suffix = ""
        text = (
            f"Ingested\n"
            f"\"{title}\"\n"
            f"+{created} pages, {updated} updates{warn_suffix}\n"
            f"ID: `{short_id}`"
        )
    elif status == "rejected":
        score = row["quality_score"]
        rationale = row["quality_rationale"] or ""
        text = f"Skipped (low quality, score {score})\n\"{title}\"\n{rationale}"
    elif status == "failed":
        error = row["error_message"] or "unknown error"
        text = f"Ingest failed\n\"{title}\"\n{error}\nID: `{short_id}`"
    else:
        return

    # Log to stdout
    emoji = {"done": "", "rejected": "", "failed": ""}.get(status, "")
    print(f"  {emoji} [{status.upper()}] [{short_id}] {title}")

    # Try Telegram notification
    bot = _get_bot()
    chat_id = row["telegram_chat_id"] or settings.owner_chat_id
    message_id = row["telegram_message_id"]
    try:
        if message_id and chat_id:
            await bot.send_message(
                chat_id=chat_id, text=text,
                reply_to_message_id=message_id,
                parse_mode="Markdown",
            )
        elif chat_id:
            await bot.send_message(
                chat_id=chat_id, text=text, parse_mode="Markdown",
            )
    except Exception as e:
        print(f"[notifier] Telegram send failed: {e}")
        return  # don't set notification_sent_at — will retry

    db.execute(
        """UPDATE resources
           SET notification_sent_at = datetime('now')
         WHERE id = ? AND notification_sent_at IS NULL""",
        (resource_id,),
    )
    db.conn.commit()
