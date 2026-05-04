"""Tests for Telegram bot — type detection and URL extraction."""

from unittest.mock import MagicMock

from app.handlers.messages import detect_type
from app.enums import ResourceType
from app.utils import extract_urls, is_youtube_url


# ------------------------------------------------------------------
# URL extraction
# ------------------------------------------------------------------

def test_extract_single_url():
    urls = extract_urls("Check this out: https://example.com/article")
    assert urls == ["https://example.com/article"]


def test_extract_multiple_urls():
    urls = extract_urls("https://a.com and https://b.com")
    assert len(urls) == 2


def test_extract_no_urls():
    urls = extract_urls("Hello world, no URLs here.")
    assert urls == []


def test_extract_strips_trailing_punctuation():
    urls = extract_urls("See https://example.com/test. It's great.")
    assert urls == ["https://example.com/test"]


# ------------------------------------------------------------------
# YouTube detection
# ------------------------------------------------------------------

def test_is_youtube_standard():
    assert is_youtube_url("https://www.youtube.com/watch?v=abc123") is True


def test_is_youtube_short():
    assert is_youtube_url("https://youtu.be/abc123") is True


def test_is_not_youtube():
    assert is_youtube_url("https://example.com/video") is False


def test_is_youtube_music():
    assert is_youtube_url("https://music.youtube.com/watch?v=abc123") is True


# ------------------------------------------------------------------
# Type detection
# ------------------------------------------------------------------

def make_message(**kwargs) -> MagicMock:
    """Create a mock aiogram Message."""
    msg = MagicMock()
    msg.document = kwargs.get("document")
    msg.text = kwargs.get("text")
    return msg


def make_document(mime_type="", file_name="") -> MagicMock:
    doc = MagicMock()
    doc.mime_type = mime_type
    doc.file_name = file_name
    return doc


def test_detect_pdf_by_mime():
    doc = make_document(mime_type="application/pdf")
    msg = make_message(document=doc)
    assert detect_type(msg) == ResourceType.PDF


def test_detect_pdf_by_extension():
    doc = make_document(mime_type="", file_name="report.PDF")
    msg = make_message(document=doc)
    assert detect_type(msg) == ResourceType.PDF


def test_detect_md():
    doc = make_document(mime_type="", file_name="notes.md")
    msg = make_message(document=doc)
    assert detect_type(msg) == ResourceType.MD


def test_detect_unsupported_document():
    doc = make_document(mime_type="image/png", file_name="photo.png")
    msg = make_message(document=doc)
    assert detect_type(msg) is None


def test_detect_web_url():
    msg = make_message(text="https://example.com/article")
    assert detect_type(msg) == ResourceType.WEB


def test_detect_youtube():
    msg = make_message(text="https://www.youtube.com/watch?v=abc123")
    assert detect_type(msg) == ResourceType.YOUTUBE


def test_detect_plain_text():
    msg = make_message(text="A" * 50 + " enough chars for text detection.")
    assert detect_type(msg) == ResourceType.TEXT


def test_detect_text_too_short():
    msg = make_message(text="short")
    assert detect_type(msg) is None


def test_detect_multiple_urls():
    msg = make_message(text="https://a.com and https://b.com")
    assert detect_type(msg) is None  # ambiguous


def test_detect_photo():
    msg = make_message()  # no doc or text
    # Simulate a photo by having no matching fields
    assert detect_type(msg) is None
