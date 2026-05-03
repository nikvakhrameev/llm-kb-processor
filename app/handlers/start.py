"""/start and /help commands."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()

HELP_TEXT = """Personal Knowledge Base bot.

I accept and process:
• Web URLs — articles, blog posts, documentation
• YouTube links — transcripts are extracted
• PDF documents — attached as files
• Markdown files — .md or .markdown
• Plain text — messages of 50+ characters
• Voice messages — transcribed to text

Supported commands:
/start — show this message
/status — view recent resources
/status <id> — view a specific resource

Send a single URL or file per message. Processing is asynchronous — you'll get a notification when it's done."""  # noqa: E501


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.reply(HELP_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.reply(HELP_TEXT)
