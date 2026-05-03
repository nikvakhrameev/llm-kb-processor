"""Tests for the quality gate logic."""

import pytest

from app.models import GateResult
from app.quality_gate import _parse_gate_response


def test_parse_valid_gate_response():
    raw = '{"score": 78, "rationale": "Good content", "topics": ["llm", "agents"]}'
    result = _parse_gate_response(raw)
    assert result.score == 78
    assert result.rationale == "Good content"
    assert result.topics == ["llm", "agents"]


def test_parse_gate_clamps_score():
    raw = '{"score": 150, "rationale": "x", "topics": []}'
    result = _parse_gate_response(raw)
    assert result.score == 100


def test_parse_gate_clamps_negative():
    raw = '{"score": -10, "rationale": "x", "topics": []}'
    result = _parse_gate_response(raw)
    assert result.score == 0


def test_parse_gate_caps_topics():
    raw = '{"score": 80, "rationale": "x", "topics": ["a", "b", "c", "d", "e", "f", "g"]}'
    result = _parse_gate_response(raw)
    assert len(result.topics) == 6


def test_parse_gate_invalid_json():
    raw = "not json"
    result = _parse_gate_response(raw)
    assert result.score == 65  # fallback
    assert "invalid JSON" in result.rationale


def test_threshold_accept():
    """Gate accepts at >= 60."""
    from app.settings import settings
    threshold = settings.gate_accept_threshold
    assert GateResult(score=60, rationale="", topics=[]).score >= threshold
    assert GateResult(score=59, rationale="", topics=[]).score < threshold
