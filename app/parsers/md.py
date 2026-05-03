"""Markdown passthrough parser."""

from pathlib import Path

from app.models import Resource
from app.parsers.base import ParseError, ParseResult, write_parsed


async def parse_md(resource: Resource, kb_root: Path) -> ParseResult:
    """Read a markdown file and extract the first heading as title."""
    if not resource.original_file_path:
        raise ParseError("no original file path for markdown")

    full_path = kb_root / resource.original_file_path
    if not full_path.exists():
        raise ParseError(f"markdown file not found: {full_path}")

    body = full_path.read_text(encoding="utf-8")
    if len(body.strip()) < 50:
        raise ParseError("MD file is empty or too short")

    title = _first_heading(body) or full_path.stem

    return write_parsed(
        resource, "md", title=title, body=body,
        parser_id="md-passthrough",
        kb_root=kb_root,
        extra={"source_filename": full_path.name},
    )


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and len(stripped) > 2:
            return stripped[2:].strip()
    return None
