"""Data models for resources and pipeline results."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Resource:
    """Mirrors the resources table row."""

    id: str
    resource_type: str
    status: str
    telegram_chat_id: int | None = None
    telegram_message_id: int | None = None
    telegram_user_id: int | None = None
    source_url: str | None = None
    original_file_path: str | None = None
    parsed_text_path: str | None = None
    inline_text: str | None = None
    content_title: str | None = None
    content_hash: str | None = None
    quality_score: int | None = None
    quality_rationale: str | None = None
    quality_topics: str | None = None
    quality_gate_skipped: bool = False
    ingest_commit_sha: str | None = None
    ingest_summary: str | None = None
    ingest_log_entry: str | None = None
    retry_count: int = 0
    next_attempt_at: str | None = None
    error_message: str | None = None
    notification_sent_at: str | None = None
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    @property
    def short_id(self) -> str:
        return self.id[:8]


@dataclass
class ParseResult:
    parsed_path: str  # relative to kb_root
    title: str
    char_count: int
    parser_id: str
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class GateResult:
    score: int
    rationale: str
    topics: list[str]
    raw_response: str = ""


@dataclass
class IngestOutcome:
    commit_sha: str
    pages_created: list[str]
    pages_updated: list[str]
    log_entry: str
    summary: str
    warnings: list[str]
    cost_usd: float = 0.0
    duration_ms: int = 0
