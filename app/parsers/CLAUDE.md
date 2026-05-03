# CLAUDE.md — app/parsers/

## Purpose

Each parser converts a raw input into a normalized markdown file in
`raw/parsed/<type>/<slug>.md` with YAML frontmatter and a clean body.

## Files

| File          | Parser                    | Dependencies                |
|---------------|---------------------------|-----------------------------|
| `base.py`     | Protocol, `write_parsed()`| `pyyaml`                    |
| `web.py`      | trafilatura               | `trafilatura`               |
| `youtube.py`  | youtube-transcript-api    | `youtube-transcript-api`, `yt-dlp` subprocess |
| `pdf.py`      | pymupdf4llm               | `pymupdf4llm`, `fitz`       |
| `md.py`       | passthrough               | none                        |
| `text.py`     | inline text               | none                        |
| `voice.py`    | faster-whisper            | `faster_whisper`            |
| `dispatch.py` | PARSERS dict + `parse()`  | all parsers above           |

## Contract

Every parser follows this protocol:

```python
async def parse_<type>(resource: Resource, kb_root: Path) -> ParseResult: ...
```

- **Input**: `Resource` dataclass (has `source_url`, `original_file_path`, `inline_text`) + `kb_root` (Path to wiki repo)
- **Output**: `ParseResult(parsed_path, title, char_count, parser_id, extra)`
- **Errors**: `ParseError` (terminal, no retry) vs `TransientParseError` (retriable)
- **File output**: via `write_parsed()` — writes markdown with YAML frontmatter to `raw/parsed/<type>/<slug>.md`

## Key details

- All parsers are async but wrap sync libraries via `asyncio.to_thread()`
- `voice.py` uses a **lazy singleton** for the Whisper model (load once, reuse)
- `youtube.py` transcripts grouped into ~30s timestamped paragraphs (`[HH:MM:SS] text`)
- `web.py` uses `favor_precision=True` for cleaner output, `include_images=False`
- `pdf.py` extracts title from PDF metadata via `fitz` (pymupdf)
- Minimum content lengths: web/pdf=200 chars, text/md=50 chars, voice=30 chars
- Parsed files use `<first8>-<kebab-title>.md` slugs, truncated to 60 chars

## Adding a new parser

1. Create `parse_<type>.py` following the protocol
2. Add to `PARSERS` dict in `dispatch.py`
3. Add the `ResourceType` enum value in `app/enums.py` (if new)
4. Add tests with offline fixtures in `tests/test_parsers.py`
