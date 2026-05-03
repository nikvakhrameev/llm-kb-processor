"""Tests for parsers using offline fixtures."""

import pytest

from app.enums import ResourceType
from app.models import Resource
from app.parsers.base import ParseError
from app.parsers.md import parse_md
from app.parsers.text import parse_text
from app.parsers.web import parse_web


def make_resource(rid: str, rtype: str, **kwargs) -> Resource:
    return Resource(id=rid, resource_type=rtype, status="received", **kwargs)


# ------------------------------------------------------------------
# Text parser
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_text_parser(tmp_wiki):
    body = "This is a test text message.\n\nIt has multiple paragraphs.\n\nIt is long enough for the 50 char minimum requirement that parsers enforce."
    r = make_resource("00000000-0000-4000-a000-000000000101", ResourceType.TEXT,
                      inline_text=body)
    result = await parse_text(r, tmp_wiki)
    assert result.char_count == len(body)
    assert result.parser_id == "text-inline"
    assert "this-is-a-test-text-message" in result.parsed_path


@pytest.mark.asyncio
async def test_text_parser_too_short(tmp_wiki):
    r = make_resource("00000000-0000-4000-a000-000000000102", ResourceType.TEXT,
                      inline_text="short")
    with pytest.raises(ParseError, match="too short"):
        await parse_text(r, tmp_wiki)


# ------------------------------------------------------------------
# Markdown parser
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_md_parser(tmp_wiki):
    md_content = "# Test Heading\n\nThis is a markdown file with enough content. " * 5
    md_path = tmp_wiki / "raw" / "inbox" / "test.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md_content)

    r = make_resource("00000000-0000-4000-a000-000000000103", ResourceType.MD,
                      original_file_path=str(md_path.relative_to(tmp_wiki)))
    result = await parse_md(r, tmp_wiki)
    assert result.title == "Test Heading"
    assert result.parser_id == "md-passthrough"


@pytest.mark.asyncio
async def test_md_parser_too_short(tmp_wiki):
    md_path = tmp_wiki / "raw" / "inbox" / "empty.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("# Hi")

    r = make_resource("00000000-0000-4000-a000-000000000104", ResourceType.MD,
                      original_file_path=str(md_path.relative_to(tmp_wiki)))
    with pytest.raises(ParseError, match="too short"):
        await parse_md(r, tmp_wiki)


# ------------------------------------------------------------------
# Web parser (offline — trafilatura can process raw HTML strings)
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_web_parser_html_fixture(tmp_wiki, monkeypatch):
    """Test web parser with HTML that trafilatura can process directly."""
    html = """<html><head><title>Test Page</title></head>
    <body><article><p>This is a test article with enough content to pass the minimum
    character requirement that parsers have for web pages.
    """ + "It continues with more text. " * 20 + """
    </p></article></body></html>"""

    # Monkeypatch trafilatura.fetch_url to return our fixture
    import trafilatura
    monkeypatch.setattr(trafilatura, "fetch_url", lambda url, **kw: html)

    r = make_resource("00000000-0000-4000-a000-000000000105", ResourceType.WEB,
                      source_url="https://example.com/test")
    result = await parse_web(r, tmp_wiki)
    assert result.parsed_path.startswith("raw/parsed/web/")
    # Title may be from metadata or fallback to URL
    assert result.title is not None


# ------------------------------------------------------------------
# Dispatch
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_no_parser():
    from app.parsers.dispatch import parse, ParseError
    r = make_resource("00000000-0000-4000-a000-000000000106", "_unknown")
    with pytest.raises(ParseError, match="no parser"):
        await parse(r, None)
