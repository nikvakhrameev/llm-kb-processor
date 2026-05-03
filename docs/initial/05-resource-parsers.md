# Resource Parsers

Each parser converts a raw input into a normalized markdown file in
`raw/parsed/<type>/<slug>.md` with a YAML frontmatter and a clean body. All
parsers share the same return contract.

## Common contract

```python
@dataclass
class ParseResult:
    parsed_path: Path        # relative to kb_root
    title: str               # best-effort, falls back to filename or URL
    char_count: int
    parser_id: str           # e.g. "trafilatura@2.x"
    extra: dict[str, Any]    # parser-specific (e.g. youtube duration)

class Parser(Protocol):
    async def parse(self, resource: Resource) -> ParseResult: ...
```

Parsers raise `ParseError` on unrecoverable failure. The resource worker
treats `ParseError` as terminal (no retries) — most parsing errors are
permanent (404 page, deleted video, corrupt PDF).

Parsers raise `TransientParseError` on rate limits or network blips. The
resource worker schedules a retry with backoff.

## Parsers by type

### `web` — trafilatura

```python
import trafilatura

async def parse_web(r: Resource) -> ParseResult:
    downloaded = await asyncio.to_thread(trafilatura.fetch_url, r.source_url,
                                          no_ssl=False)
    if not downloaded:
        raise ParseError(f"could not fetch {r.source_url}")

    md = await asyncio.to_thread(
        trafilatura.extract,
        downloaded,
        output_format="markdown",
        with_metadata=True,
        include_links=True,
        include_tables=True,
        include_images=False,   # MVP: text only
        deduplicate=True,
        favor_precision=True,
    )
    if not md or len(md) < 200:
        raise ParseError(f"empty or near-empty extraction (len={len(md or '')})")

    metadata = trafilatura.extract_metadata(downloaded)
    title = metadata.title if metadata else r.source_url

    return write_parsed(r, "web", title=title, body=md, parser_id="trafilatura@2.x",
                        extra={"author": metadata.author if metadata else None,
                               "site": urlparse(r.source_url).hostname})
```

Notes:
- `favor_precision=True` chooses cleanliness over recall (less boilerplate).
- Some sites (Medium, paywalled) fail. Fallback to `readability-lxml` is
  optional; MVP does not include it.
- For sites that return JS-rendered pages, MVP gives up. A v2 enhancement
  could use Playwright via a separate sidecar.

### `youtube` — youtube-transcript-api + yt-dlp

Two-step:

1. Try `youtube-transcript-api` for an existing transcript (auto-generated or
   manual). Languages tried in order: `en`, `ru`, then any.
2. If no transcript, fall back to `yt-dlp` audio download + `faster-whisper`
   transcription. (Optional in MVP; can `raise ParseError` instead.)

```python
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

async def parse_youtube(r: Resource) -> ParseResult:
    video_id = extract_video_id(r.source_url)
    try:
        transcript = await asyncio.to_thread(
            YouTubeTranscriptApi.get_transcript,
            video_id,
            languages=["en", "ru"],
        )
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        raise ParseError(f"no transcript available: {e}")

    body = format_transcript_with_timestamps(transcript)

    metadata = await asyncio.to_thread(fetch_yt_metadata, r.source_url)
    title = metadata.get("title") or video_id
    duration_s = metadata.get("duration") or 0
    channel = metadata.get("uploader") or ""

    return write_parsed(r, "youtube", title=title, body=body,
                        parser_id="youtube-transcript-api",
                        extra={"video_id": video_id, "duration_s": duration_s,
                               "channel": channel})
```

`fetch_yt_metadata` uses `yt-dlp` in info-only mode (`-J` JSON dump) without
downloading the video.

Transcript formatting groups segments into ~30s paragraphs with leading
timestamps to allow the agent to cite specific moments:

```
[00:00:12] In this video we discuss the architecture of...
[00:00:45] The key idea is that...
```

### `pdf` — pymupdf4llm

```python
import pymupdf4llm

async def parse_pdf(r: Resource) -> ParseResult:
    full_path = settings.kb_root / r.original_file_path
    md = await asyncio.to_thread(pymupdf4llm.to_markdown, str(full_path))
    if len(md) < 200:
        raise ParseError(f"PDF parsed to <200 chars (len={len(md)})")

    title = extract_pdf_title(full_path) or full_path.stem

    return write_parsed(r, "pdf", title=title, body=md,
                        parser_id="pymupdf4llm",
                        extra={"source_filename": full_path.name})
```

`pymupdf4llm` produces clean markdown with headings, tables, and lists. It
does not OCR scanned PDFs in MVP — those will yield <200 chars and be
rejected. OCR support (via `ocrmypdf` preprocessing) is a v2 feature.

### `md` — passthrough

```python
async def parse_md(r: Resource) -> ParseResult:
    full_path = settings.kb_root / r.original_file_path
    body = full_path.read_text(encoding="utf-8")
    if len(body) < 50:
        raise ParseError("MD file is empty")
    title = extract_first_heading(body) or full_path.stem
    return write_parsed(r, "md", title=title, body=body,
                        parser_id="md-passthrough",
                        extra={"source_filename": full_path.name})
```

### `text` — inline

```python
async def parse_text(r: Resource) -> ParseResult:
    body = r.inline_text
    if not body or len(body) < 50:
        raise ParseError("text too short")
    title = first_line(body)[:80]
    return write_parsed(r, "text", title=title, body=body,
                        parser_id="text-inline",
                        extra={})
```

### `voice` — faster-whisper

```python
from faster_whisper import WhisperModel

_whisper_model = None

def get_whisper():
    global _whisper_model
    if _whisper_model is None:
        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    return _whisper_model

async def parse_voice(r: Resource) -> ParseResult:
    full_path = settings.kb_root / r.original_file_path
    model = get_whisper()
    segments, info = await asyncio.to_thread(model.transcribe, str(full_path),
                                              vad_filter=True)
    body = "\n".join(s.text.strip() for s in segments).strip()
    if len(body) < 30:
        raise ParseError("voice transcription empty")

    title = body[:60]  # voice notes have no title; use first chars
    return write_parsed(r, "voice", title=title, body=body,
                        parser_id=f"faster-whisper@small/{info.language}",
                        extra={"language": info.language,
                               "duration_s": info.duration})
```

Whisper "small" runs on CPU at roughly 4× real-time on a modern VPS. For a
3-minute voice note that is ~45s of processing, well within retry budgets.

## Output writer

```python
def write_parsed(r: Resource, type_dir: str, *, title: str, body: str,
                 parser_id: str, extra: dict) -> ParseResult:
    slug = make_slug(r.id, title)
    rel = Path("raw") / "parsed" / type_dir / f"{slug}.md"
    abs_path = settings.kb_root / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)

    frontmatter = {
        "resource_id": r.id,
        "resource_type": type_dir,
        "source_url": r.source_url,
        "title": title,
        "fetched_at": utc_now_iso(),
        "char_count": len(body),
        "parser": parser_id,
        **{k: v for k, v in extra.items() if v is not None},
    }
    document = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n" + body
    abs_path.write_text(document, encoding="utf-8")

    return ParseResult(parsed_path=rel, title=title, char_count=len(body),
                       parser_id=parser_id, extra=extra)
```

Slug generation (`make_slug`) takes the resource id's first 8 hex chars,
appends a kebab-cased title, and truncates to 60 chars total.

## Dispatch

```python
PARSERS: dict[ResourceType, Parser] = {
    ResourceType.WEB: parse_web,
    ResourceType.YOUTUBE: parse_youtube,
    ResourceType.PDF: parse_pdf,
    ResourceType.MD: parse_md,
    ResourceType.TEXT: parse_text,
    ResourceType.VOICE: parse_voice,
}

async def parse(r: Resource) -> ParseResult:
    parser = PARSERS.get(r.resource_type)
    if parser is None:
        raise ParseError(f"no parser for {r.resource_type}")
    return await parser(r)
```

## Error mapping

| Exception                     | Worker action                                 |
|-------------------------------|-----------------------------------------------|
| `ParseError`                  | terminal `failed`, no retry                   |
| `TransientParseError`         | retry with backoff up to 3 attempts           |
| Any other exception           | retry with backoff (treated as transient)     |

## Resource cleanup on rejection or failure

When a resource transitions to `rejected` or `failed`, the parsed file (if any)
is moved from `raw/parsed/<type>/` to `raw/rejected/<type>/`. The original file
in `raw/inbox/` stays. This keeps the rejected pile auditable for tuning the
quality gate threshold without piling up in active directories.
