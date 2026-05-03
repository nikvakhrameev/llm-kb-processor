"""Telegram bot — intake endpoint for the knowledge base.

aiogram 3.x in long-polling mode. The bot receives messages, detects resource
types, downloads attachments, inserts SQLite rows, and replies with ack.
It does NOT call any LLMs or parsers.
"""

from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update

from app.db import Database
from app.handlers.messages import router as messages_router
from app.handlers.start import router as start_router
from app.handlers.status import router as status_router
from app.settings import settings


async def auth_middleware(handler, event: Update, data: dict):
    """Silent drop for non-allowlisted users."""
    if event.message:
        user = event.message.from_user
    elif event.callback_query:
        user = event.callback_query.from_user
    else:
        user = getattr(event, "from_user", None)

    if user is None or user.id not in settings.allowed_user_ids:
        return  # silent drop
    return await handler(event, data)


async def run_bot() -> None:
    """Entry point for the Telegram bot service."""
    if not settings.telegram_bot_token:
        print("[bot] No TELEGRAM_BOT_TOKEN set — exiting")
        return

    if not settings.allowed_user_ids:
        print("[bot] WARNING: ALLOWED_USER_IDS is empty. No users can interact.")

    bot = Bot(token=settings.telegram_bot_token)

    dp = Dispatcher()
    dp.update.middleware.register(auth_middleware)
    dp.include_router(start_router)
    dp.include_router(status_router)
    dp.include_router(messages_router)

    print("[bot] Starting Telegram polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_bot())
