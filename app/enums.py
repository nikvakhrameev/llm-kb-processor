"""Enums for resource types and pipeline states."""

from enum import StrEnum


class ResourceType(StrEnum):
    WEB = "web"
    PDF = "pdf"
    MD = "md"
    YOUTUBE = "youtube"
    TEXT = "text"
    VOICE = "voice"
    LINT = "_lint"
    SYNTHESIS_WEEKLY = "_synthesis_weekly"


class ResourceStatus(StrEnum):
    RECEIVED = "received"
    PARSING = "parsing"
    PARSED = "parsed"
    GATING = "gating"
    APPROVED = "approved"
    REJECTED = "rejected"
    INGESTING = "ingesting"
    DONE = "done"
    FAILED = "failed"
