"""/lint and /synthesis commands — manual enqueue of synthetic resources."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.cron.lint import enqueue_lint
from app.cron.synthesis import enqueue_synthesis
from app.db import Database
from app.settings import settings

router = Router()


def _db() -> Database:
    db = Database(settings.state_db)
    db.connect()
    return db


@router.message(Command("lint"))
async def cmd_lint(message: Message) -> None:
    db = _db()
    try:
        rid = await enqueue_lint(db)
        await message.reply(f"Lint job queued `{rid[:8]}`", parse_mode="Markdown")
    finally:
        db.close()


@router.message(Command("synthesis"))
async def cmd_synthesis(message: Message) -> None:
    db = _db()
    try:
        rid = await enqueue_synthesis(db)
        await message.reply(f"Synthesis job queued `{rid[:8]}`", parse_mode="Markdown")
    finally:
        db.close()