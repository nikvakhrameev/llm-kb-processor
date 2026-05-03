"""Voice message parser using faster-whisper."""

import asyncio
from pathlib import Path

from app.models import Resource
from app.parsers.base import ParseError, ParseResult, write_parsed

_whisper_model = None


def _get_whisper():
    """Lazy singleton for the Whisper model (small, CPU, int8)."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    return _whisper_model


async def parse_voice(resource: Resource, kb_root: Path) -> ParseResult:
    """Transcribe a voice message using faster-whisper."""
    if not resource.original_file_path:
        raise ParseError("no original file path for voice message")

    full_path = kb_root / resource.original_file_path
    if not full_path.exists():
        raise ParseError(f"voice file not found: {full_path}")

    model = _get_whisper()

    try:
        segments, info = await asyncio.to_thread(
            model.transcribe, str(full_path), vad_filter=True
        )
    except Exception as e:
        raise ParseError(f"whisper transcription failed: {e}")

    body = "\n".join(s.text.strip() for s in segments if s.text.strip()).strip()
    if len(body) < 30:
        raise ParseError("voice transcription empty or too short")

    title = body[:60]

    return write_parsed(
        resource, "voice", title=title, body=body,
        parser_id=f"faster-whisper@small/{info.language}",
        kb_root=kb_root,
        extra={
            "language": info.language,
            "duration_s": info.duration,
        },
    )
