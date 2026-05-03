# Quality Gate

## Purpose

A cheap LLM filter that decides whether a parsed resource is worth ingesting
into the wiki. The bar is intentionally low: it only filters out **garbage**,
not "low-priority" content. Anything that contains coherent useful text passes.

The user has already decided this resource is interesting (they sent it via
Telegram). The gate exists to catch:

- Pages where parsing produced noise (cookie banners, paywalls, JavaScript
  errors)
- YouTube transcripts that are auto-captions of pure music or silence
- Voice notes that turned out to be background noise
- Very short text snippets accidentally sent

It is **not** there to second-guess the user's interest.

## Provider and model

- **Primary**: DeepSeek V4 Flash via the OpenAI-compatible endpoint
  `https://api.deepseek.com/v1`
- **Fallback** (optional in MVP): OpenRouter with the same prompt
- **Model id**: `deepseek-v4-flash` (non-thinking mode)
- **Client**: `openai` Python SDK with `base_url` override

```python
from openai import AsyncOpenAI

deepseek = AsyncOpenAI(
    api_key=settings.deepseek_api_key,
    base_url="https://api.deepseek.com/v1",
)
```

## Input

The gate receives:

1. The full text of `purpose.md` from the wiki.
2. The first ~2000 tokens of the parsed body (truncate, do not summarize).
3. Lightweight metadata: resource type, source URL or filename, title.

```python
async def quality_gate(r: Resource, parsed_body: str, purpose_md: str) -> GateResult:
    snippet = truncate_to_tokens(parsed_body, max_tokens=2000)
    response = await deepseek.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": GATE_SYSTEM_PROMPT.format(purpose=purpose_md)},
            {"role": "user", "content": GATE_USER_TEMPLATE.format(
                resource_type=r.resource_type,
                source=r.source_url or r.original_file_path or "(inline text)",
                title=r.content_title or "",
                body=snippet,
            )},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=400,
    )
    return parse_gate_response(response.choices[0].message.content)
```

`response_format={"type": "json_object"}` forces JSON output. The prompt
also specifies the schema explicitly to keep the model on-rails.

## Prompt

The full prompt text lives in `09-prompts.md`. Conceptually:

> You are a quality filter for a personal knowledge base. The owner has
> stated their interests in <purpose>. Decide whether this parsed resource
> contains coherent useful content worth integrating.
>
> Score 0–100:
> - 0–30: garbage (parsing artifacts, error pages, irrelevant noise)
> - 30–60: marginal (real content but very short, off-topic, or duplicative)
> - 60–80: useful (relevant or interesting, worth keeping)
> - 80–100: highly useful (clearly aligned with stated interests)
>
> Default is to be permissive. When in doubt, score 65.
>
> Return strict JSON: {"score": int, "rationale": str, "topics": [str]}

## Output

```python
@dataclass
class GateResult:
    score: int                 # 0..100
    rationale: str             # 1-2 sentences for the audit log
    topics: list[str]          # short tags, ≤6, lowercase, kebab-case
    raw_response: str          # for events.payload
```

## Threshold

```python
GATE_ACCEPT_THRESHOLD = 60
```

- `score >= 60` → status=`approved`
- `score < 60` → status=`rejected`, parsed file moved to `raw/rejected/`,
  Telegram reply: `🚫 Skipped (score=<n>): <rationale>`

The rationale is shown to the user so they understand why something was
filtered. This builds trust — and gives them feedback for tuning purpose.md
or for sending an `/override <resource_id>` command (planned for v2).

## Default-accept on infrastructure failure

If three consecutive DeepSeek calls fail (network error, 5xx, rate limit), the
gate is skipped:

```python
async def gate_with_retries(r: Resource, body: str, purpose: str) -> GateResult:
    for attempt in range(3):
        try:
            return await quality_gate(r, body, purpose)
        except (APIError, NetworkError) as e:
            await asyncio.sleep(60 * (2 ** attempt))
            last_error = e
    # all retries failed → default-accept with a synthetic result
    return GateResult(score=65, rationale=f"gate-skipped: {last_error}",
                      topics=[], raw_response="")
```

The resource is marked with `quality_gate_skipped=1` in SQLite so this is
visible in audit and can be filtered in `/status`.

## Caching

Static prompt parts (`GATE_SYSTEM_PROMPT` interpolated with `purpose.md`) are
identical across calls within a deployment. DeepSeek's automatic prompt
caching deduplicates the prefix server-side and bills at 10× lower rate for
cache hits. To maximize hit rate:

- Put the system message first and **never vary it within a deployment**.
- Put the variable parts (snippet, metadata) in the user message.
- Do not interpolate dynamic values (current time, etc.) into the system
  message.

If `purpose.md` changes, the cache is invalidated for new `purpose.md` versions
but old calls still hit the previous cached prefix. Restart not needed.

## Cost estimate

- Snippet: ~2000 tokens.
- System + purpose + user wrapper: ~1500 tokens.
- Total input: ~3500 tokens. Output: ~150 tokens.
- DeepSeek V4 Flash pricing (cache-hit): roughly $0.0001 per call.
- At 50 resources/day: ~$0.15/month for the gate.

The gate is essentially free. Even running it on every resource without any
optimization is fine.

## Special handling per type

The gate runs the same prompt on every type, but the prompt instructs the
model to apply type-specific rules:

- **voice**: very short transcripts (≤30s) are usually low value unless they
  contain a clearly stated question or fact. Score conservatively.
- **text**: tweets and short notes can be valuable. Length is not a strict
  signal here.
- **web**: paywalled snippets and 404 pages should score very low.
- **pdf**: scanned PDFs that produced gibberish (no recognizable words) score 0.
- **youtube**: pure-music videos with auto-caption noise score low.

The prompt encodes these heuristics so we do not need to branch in code.

## Observability

Every gate call writes one `events` row:

```json
{
  "event_type": "gate_result",
  "payload": {
    "score": 78,
    "rationale": "...",
    "topics": ["llm-agents", "evals"],
    "snippet_tokens": 1934,
    "model": "deepseek-v4-flash",
    "duration_ms": 412,
    "skipped": false
  }
}
```

This data feeds future analysis (false-positive rate, threshold tuning,
purpose.md drift over time).
