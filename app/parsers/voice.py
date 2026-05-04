"""Voice message parser using faster-whisper."""

import asyncio
from pathlib import Path

from app.models import Resource
from app.parsers.base import ParseError, TransientParseError, ParseResult, write_parsed

_whisper_model = None


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    return _whisper_model


def _transcribe(filepath: str) -> tuple[str, str, float]:
    """Run transcription entirely in a thread — exhausts the generator here
    so CPU-bound inference doesn't block the event loop."""
    model = _get_whisper()
    segments, info = model.transcribe(filepath, vad_filter=True)
    body = "\n".join(s.text.strip() for s in segments if s.text.strip()).strip()
    return body, info.language, info.duration


async def parse_voice(resource: Resource, kb_root: Path) -> ParseResult:
    if not resource.original_file_path:
        raise ParseError("no original file path for voice message")

    full_path = kb_root / resource.original_file_path
    if not full_path.exists():
        raise ParseError(f"voice file not found: {full_path}")

    try:
        body, language, duration = await asyncio.to_thread(
            _transcribe, str(full_path)
        )
    except Exception as e:
        raise TransientParseError(f"whisper transcription failed: {e}")

    if len(body) < 30:
        raise ParseError("voice transcription empty or too short")

    title = body[:60]

    return write_parsed(
        resource, "voice", title=title, body=body,
        parser_id=f"faster-whisper@small/{language}",
        kb_root=kb_root,
        extra={
            "language": language,
            "duration_s": duration,
        },
    )
