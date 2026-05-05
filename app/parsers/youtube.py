"""YouTube parser using youtube-transcript-api and yt-dlp for metadata."""

import asyncio
import json
from pathlib import Path

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
)
from youtube_transcript_api.proxies import GenericProxyConfig

from app.models import Resource
from app.parsers.base import ParseError, TransientParseError, ParseResult, write_parsed
from app.parsers.proxy_fetcher import fetch_proxies
from app.utils import extract_video_id


async def parse_youtube(resource: Resource, kb_root: Path) -> ParseResult:
    if not resource.source_url:
        raise ParseError("no source URL")

    video_id = extract_video_id(resource.source_url)
    if not video_id:
        raise ParseError(f"could not extract video ID from {resource.source_url}")

    proxies = await fetch_proxies()
    transcript = await _fetch_transcript_with_proxies(video_id, proxies)

    body = format_transcript(transcript)
    metadata = await fetch_metadata(resource.source_url)
    title = metadata.get("title") or video_id
    duration_s = metadata.get("duration") or 0
    channel = metadata.get("uploader") or ""

    return write_parsed(
        resource, "youtube", title=title, body=body,
        parser_id="youtube-transcript-api",
        kb_root=kb_root,
        extra={
            "video_id": video_id,
            "duration_s": duration_s,
            "channel": channel,
        },
    )


async def _fetch_transcript_with_proxies(video_id: str, proxies: list[str]) -> list[dict]:
    """Fetch transcript, rotating proxies on block errors."""

    async def _attempt(url: str | None) -> list[dict]:
        if url:
            proxy_config = GenericProxyConfig(http_url=url, https_url=url)
            ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config)
        else:
            ytt_api = YouTubeTranscriptApi()

        fetched = await asyncio.to_thread(
            lambda: ytt_api.fetch(video_id, languages=["en", "ru"])
        )
        return fetched.to_raw_data()

    candidates = proxies if proxies else [None]
    last_error: Exception | None = None

    for proxy_url in candidates:
        try:
            return await _attempt(proxy_url)
        except (TranscriptsDisabled, NoTranscriptFound) as e:
            raise ParseError(f"no transcript available: {e}")
        except (RequestBlocked, IpBlocked) as e:
            last_error = e
            continue
        except Exception as e:
            last_error = e
            continue

    if not proxies:
        raise TransientParseError(
            "transcript fetch blocked: no proxies available, and direct fetch also failed"
        )
    raise TransientParseError(
        f"transcript fetch blocked on all {len(proxies)} proxies, last error: {last_error}"
    )


def format_transcript(transcript: list[dict], segment_interval_s: int = 30) -> str:
    paragraphs: list[str] = []
    current: list[str] = []
    current_start = 0.0

    for seg in transcript:
        start = seg.get("start", 0.0)
        text = seg.get("text", "").strip()
        if not text:
            continue

        if not current:
            current_start = start

        if start - current_start > segment_interval_s and current:
            ts = format_timestamp(current_start)
            paragraphs.append(f"[{ts}] {' '.join(current)}")
            current = [text]
            current_start = start
        else:
            current.append(text)

    if current:
        ts = format_timestamp(current_start)
        paragraphs.append(f"[{ts}] {' '.join(current)}")

    return "\n\n".join(paragraphs)


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


async def fetch_metadata(url: str) -> dict:
    async def _run() -> dict:
        try:
            proc = await asyncio.create_subprocess_exec(
                "yt-dlp", "-J", "--no-playlist", "--flat-playlist", url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0 and stdout:
                return json.loads(stdout)
        except Exception:
            pass
        return {}

    return await _run()
