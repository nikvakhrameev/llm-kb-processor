"""Quality gate — filters parsed resources via DeepSeek before ingestion.

Uses the OpenAI-compatible endpoint. After 3 retries on infrastructure failure,
defaults to accept (score=65) so no resource is silently lost.
"""

import asyncio
import json
import time

from openai import AsyncOpenAI

from app.models import GateResult
from app.prompts import render_gate_system, render_gate_user
from app.settings import settings
from app.utils import truncate_to_tokens


def get_deepseek_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
    )


async def quality_gate(
    resource_type: str,
    source: str,
    title: str,
    body: str,
    purpose_md: str,
) -> GateResult:
    """Run the quality gate against DeepSeek.

    Sends the first ~2000 tokens of parsed content plus purpose.md context.
    Returns a GateResult with score, rationale, and topic tags.
    """
    client = get_deepseek_client()
    snippet = truncate_to_tokens(body, max_tokens=2000)

    response = await client.chat.completions.create(
        model=settings.deepseek_model,
        messages=[
            {"role": "system", "content": render_gate_system(purpose_md)},
            {"role": "user", "content": render_gate_user(
                resource_type=resource_type, source=source, title=title, body=snippet,
            )},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=400,
    )

    raw = response.choices[0].message.content or "{}"
    return _parse_gate_response(raw)


def _parse_gate_response(raw: str) -> GateResult:
    """Parse the JSON response, clamping and validating."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: return a low-confidence accept
        return GateResult(score=65, rationale="gate: invalid JSON response",
                          topics=[], raw_response=raw)

    score = max(0, min(100, int(data.get("score", 65))))
    rationale = str(data.get("rationale", ""))
    topics = [str(t).lower().replace(" ", "-") for t in data.get("topics", [])][:6]

    return GateResult(score=score, rationale=rationale, topics=topics,
                      raw_response=raw)


async def gate_with_retries(
    resource_type: str,
    source: str,
    title: str,
    body: str,
    purpose_md: str,
    *,
    max_retries: int = 3,
) -> tuple[GateResult, bool]:
    """Run the quality gate with retries and default-accept fallback.

    Returns (GateResult, skipped) where skipped=True means the gate was
    bypassed due to infrastructure failure.
    """
    last_error: Exception | None = None

    for attempt in range(max_retries):
        t0 = time.monotonic()
        try:
            result = await quality_gate(resource_type, source, title, body, purpose_md)
            return result, False
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = 60 * (2 ** attempt)
                await asyncio.sleep(delay)

    # All retries failed — default-accept
    return GateResult(
        score=65,
        rationale=f"gate-skipped: {last_error}",
        topics=[],
        raw_response="",
    ), True
