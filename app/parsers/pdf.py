"""PDF parser using pymupdf4llm."""

import asyncio
from pathlib import Path

import pymupdf4llm

from app.models import Resource
from app.parsers.base import ParseError, ParseResult, write_parsed


async def parse_pdf(resource: Resource, kb_root: Path) -> ParseResult:
    """Convert a PDF to clean markdown using pymupdf4llm."""
    if not resource.original_file_path:
        raise ParseError("no original file path for PDF")

    full_path = kb_root / resource.original_file_path
    if not full_path.exists():
        raise ParseError(f"PDF file not found: {full_path}")

    md = await asyncio.to_thread(pymupdf4llm.to_markdown, str(full_path))
    if len(md) < 200:
        raise ParseError(f"PDF parsed to <200 chars (len={len(md)})")

    title = _extract_title(full_path) or full_path.stem

    return write_parsed(
        resource, "pdf", title=title, body=md,
        parser_id="pymupdf4llm",
        kb_root=kb_root,
        extra={"source_filename": full_path.name},
    )


def _extract_title(filepath: Path) -> str | None:
    """Try to extract a title from PDF metadata."""
    import fitz  # pymupdf

    try:
        doc = fitz.open(str(filepath))
        metadata = doc.metadata
        doc.close()
        if metadata and metadata.get("title"):
            return metadata["title"].strip()
    except Exception:
        pass
    return None
