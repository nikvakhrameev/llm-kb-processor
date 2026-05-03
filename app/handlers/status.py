"""/status command — view resource state."""

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.db import Database
from app.settings import settings

router = Router()


def _db() -> Database:
    db = Database(settings.state_db)
    db.connect()
    return db


def _format_resource(row) -> str:
    status = row["status"]
    short = row["id"][:8]
    rtype = row["resource_type"]
    title = row["content_title"] or "(untitled)"
    error = row["error_message"] or ""
    created = row["created_at"] or ""

    lines = [
        f"`{short}`  {status.upper()}  {rtype}  {title}",
    ]
    if error:
        lines.append(f"  Error: {error}")
    if status == "done":
        sha = row["ingest_commit_sha"] or ""
        lines.append(f"  Commit: {sha[:7]}")
    lines.append(f"  Created: {created}")
    return "\n".join(lines)


@router.message(Command("status"))
async def cmd_status(message: Message, command: CommandObject) -> None:
    db = _db()
    try:
        if command.args:
            row = db.find_by_short_id(command.args.strip())
            if row is None:
                await message.reply("Not found.")
                return
            await message.reply(_format_resource(row), parse_mode="Markdown")
        else:
            user_id = message.from_user.id
            rows = db.recent_for_user(user_id, limit=10)
            if not rows:
                await message.reply("No resources yet. Send me a URL, file, or text.")
                return
            text = "\n\n".join(_format_resource(r) for r in rows)
            await message.reply(text, parse_mode="Markdown")
    finally:
        db.close()
