"""Web page parser using trafilatura."""

import asyncio
from urllib.parse import urlparse

import trafilatura

from app.models import Resource
from app.parsers.base import ParseError, TransientParseError, ParseResult, write_parsed


async def parse_web(resource: Resource, kb_root) -> ParseResult:
    if not resource.source_url:
        raise ParseError("no source URL")

    try:
        downloaded = await asyncio.to_thread(
            trafilatura.fetch_url, resource.source_url, no_ssl=False
        )
    except Exception as e:
        raise TransientParseError(f"network fetch failed: {e}")

    if not downloaded:
        raise ParseError(f"could not fetch {resource.source_url}")

    md = await asyncio.to_thread(
        trafilatura.extract,
        downloaded,
        output_format="markdown",
        with_metadata=True,
        include_links=True,
        include_tables=True,
        include_images=False,
        deduplicate=True,
        favor_precision=True,
    )
    if not md or len(md) < 200:
        raise ParseError(f"empty or near-empty extraction (len={len(md or '')})")

    metadata = trafilatura.extract_metadata(downloaded)
    title = metadata.title if metadata and metadata.title else resource.source_url

    return write_parsed(
        resource, "web", title=title, body=md, parser_id="trafilatura@2.x",
        kb_root=kb_root,
        extra={
            "author": metadata.author if metadata and metadata.author else None,
            "site": urlparse(resource.source_url).hostname,
        },
    )
