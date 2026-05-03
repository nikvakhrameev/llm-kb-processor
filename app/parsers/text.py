"""Inline text parser."""

from pathlib import Path

from app.models import Resource
from app.parsers.base import ParseError, ParseResult, write_parsed


async def parse_text(resource: Resource, kb_root: Path) -> ParseResult:
    """Use the inline text directly. First line becomes the title."""
    body = resource.inline_text
    if not body or len(body.strip()) < 50:
        raise ParseError("text too short")

    title = body.strip().split("\n")[0][:80]

    return write_parsed(
        resource, "text", title=title, body=body,
        parser_id="text-inline",
        kb_root=kb_root,
        extra={},
    )
