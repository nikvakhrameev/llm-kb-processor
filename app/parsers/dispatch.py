"""Parser dispatch table."""

from app.enums import ResourceType
from app.parsers.base import Parser, ParseError
from app.parsers.md import parse_md
from app.parsers.pdf import parse_pdf
from app.parsers.text import parse_text
from app.parsers.voice import parse_voice
from app.parsers.web import parse_web
from app.parsers.youtube import parse_youtube

PARSERS: dict[ResourceType, Parser] = {
    ResourceType.WEB: parse_web,
    ResourceType.YOUTUBE: parse_youtube,
    ResourceType.PDF: parse_pdf,
    ResourceType.MD: parse_md,
    ResourceType.TEXT: parse_text,
    ResourceType.VOICE: parse_voice,
}


async def parse(resource, kb_root) -> "ParseResult":
    """Dispatch to the appropriate parser based on resource type."""
    parser = PARSERS.get(resource.resource_type)
    if parser is None:
        raise ParseError(f"no parser for resource type: {resource.resource_type}")
    return await parser(resource, kb_root)
