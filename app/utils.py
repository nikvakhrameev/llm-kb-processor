"""Utility functions — UUIDs, slugs, token truncation, timestamps."""

import re
import uuid
from datetime import datetime, timezone


def uuid_str() -> str:
    """Generate a UUIDv4 string."""
    return str(uuid.uuid4())


def make_slug(resource_id: str, title: str, max_len: int = 60) -> str:
    """Create a file slug: <first8>-<kebab-title> truncated to max_len."""
    short = resource_id[:8]
    kebab = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    if not kebab:
        kebab = "untitled"
    slug = f"{short}-{kebab}"
    return slug[:max_len]


def utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Roughly truncate text to max_tokens (assuming ~4 chars per token)."""
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def extract_urls(text: str) -> list[str]:
    """Extract URLs from text, stripping trailing punctuation."""
    url_re = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)
    matches = url_re.findall(text)
    return [m.rstrip(".,;:!?") for m in matches]


def is_youtube_url(url: str) -> bool:
    """Check if a URL is a YouTube link."""
    from urllib.parse import urlparse

    youtube_hosts = {
        "youtube.com", "www.youtube.com", "m.youtube.com",
        "youtu.be", "music.youtube.com",
    }
    host = (urlparse(url).hostname or "").lower()
    return host in youtube_hosts


def extract_video_id(url: str) -> str:
    """Extract YouTube video ID from a URL."""
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    if "youtu.be" in (parsed.hostname or ""):
        return parsed.path.lstrip("/")
    if "youtube.com" in (parsed.hostname or ""):
        qs = parse_qs(parsed.query)
        if "v" in qs:
            return qs["v"][0]
    return parsed.path.rsplit("/", 1)[-1]
