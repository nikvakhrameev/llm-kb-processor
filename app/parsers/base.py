"""Parser protocol and shared utilities.

All parsers follow the same contract: parse(resource) -> ParseResult.
Parsers raise ParseError for terminal failures (no retry),
TransientParseError for temporary failures (retry with backoff).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.models import Resource
from app.utils import make_slug, utc_now_iso


class ParseError(Exception):
    """Non-retriable parsing failure (404, corrupt file, empty content)."""


class TransientParseError(Exception):
    """Retriable parsing failure (rate limit, network blip)."""


@dataclass
class ParseResult:
    parsed_path: str  # relative to kb_root
    title: str
    char_count: int
    parser_id: str
    extra: dict[str, Any]


class Parser(Protocol):
    """Protocol for type-specific parsers."""

    async def parse(self, resource: Resource) -> ParseResult: ...


def write_parsed(
    resource: Resource,
    type_dir: str,
    *,
    title: str,
    body: str,
    parser_id: str,
    kb_root: Path,
    extra: dict[str, Any] | None = None,
) -> ParseResult:
    """Write a parsed markdown file with YAML frontmatter.

    Returns a ParseResult with the relative path and metadata.
    """
    slug = make_slug(resource.id, title)
    rel = f"raw/parsed/{type_dir}/{slug}.md"
    abs_path = kb_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    frontmatter: dict[str, Any] = {
        "resource_id": resource.id,
        "resource_type": type_dir,
        "source_url": resource.source_url,
        "title": title,
        "fetched_at": utc_now_iso(),
        "char_count": len(body),
        "parser": parser_id,
    }
    if extra:
        frontmatter.update({k: v for k, v in extra.items() if v is not None})

    document = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + body
    abs_path.write_text(document, encoding="utf-8")

    return ParseResult(
        parsed_path=rel,
        title=title,
        char_count=len(body),
        parser_id=parser_id,
        extra=extra or {},
    )
