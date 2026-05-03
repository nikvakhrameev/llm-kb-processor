"""Tests for data models."""

from app.models import GateResult, IngestOutcome, ParseResult, Resource


def test_resource_short_id():
    r = Resource(id="0193f7a8-3c8b-7e2a-9f4c-2c9e7d3a1b6e", resource_type="web",
                 status="received")
    assert r.short_id == "0193f7a8"


def test_parse_result_defaults():
    pr = ParseResult(parsed_path="raw/parsed/web/test.md", title="Test",
                     char_count=100, parser_id="test@1.0")
    assert pr.extra == {}


def test_gate_result():
    gr = GateResult(score=78, rationale="Useful content",
                    topics=["llm", "agents"])
    assert gr.score == 78
    assert len(gr.topics) == 2


def test_ingest_outcome():
    outcome = IngestOutcome(
        commit_sha="abc1234",
        pages_created=["wiki/sources/test.md"],
        pages_updated=[],
        log_entry="- test entry",
        summary="Done.",
        warnings=[],
    )
    assert outcome.commit_sha == "abc1234"
