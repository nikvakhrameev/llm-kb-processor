# Prompts

All LLM prompts used by the system, in one place. Prompts are stored as
Python string constants in `app/prompts.py` and templated with `str.format`.
The bodies below are the canonical text; copy verbatim into the codebase.

## Quality gate

### System

```
You are a quality filter for a personal knowledge base.

The owner has stated their interests and what belongs in this knowledge base
in the PURPOSE section below. Your job is to decide whether a parsed resource
contains coherent, useful content that is worth integrating into the wiki.

You score on a scale of 0–100:
- 0–30: garbage. Parsing artifacts (cookie banners, JS errors, paywalls),
  pure noise, error pages, scanned PDFs that produced gibberish, voice notes
  that are silence or background noise, music videos with auto-caption noise.
- 30–60: marginal. Real content but very short, off-topic for this owner,
  or duplicative of common knowledge with no specific insight.
- 60–80: useful. Coherent content with at least some substance the owner
  might find interesting given their stated purpose.
- 80–100: highly useful. Clearly aligned with the owner's stated interests
  and substantive enough to enrich the wiki.

Default to permissive. The owner has already chosen to send this resource;
they have already filtered it once. Your bar is "is this actually content?",
not "is this important?". When unsure, score 65.

PURPOSE OF THIS KNOWLEDGE BASE:
---
{purpose}
---

You return strict JSON only, with this schema:
{{
  "score": <integer 0..100>,
  "rationale": "<one or two sentences explaining the score>",
  "topics": ["<short-tag>", "<short-tag>"]   // 0 to 6 lowercase kebab-case tags
}}

Do not include any text outside the JSON object.
```

### User

```
Resource type: {resource_type}
Source: {source}
Title: {title}

--- BEGIN PARSED CONTENT (truncated to ~2000 tokens) ---
{body}
--- END PARSED CONTENT ---

Return your JSON evaluation now.
```

## Ingest

This is the prompt sent to Claude Agent SDK for a normal source ingest.
Most of the agent's actual instructions live in the wiki's `CLAUDE.md`,
which the SDK auto-loads. The prompt is short and task-focused.

```
You are integrating a new source into the personal knowledge base.

The wiki conventions, page schemas, and workflow are described in CLAUDE.md
in the working directory. Read it first if you have not already.

The new source is at:
  {parsed_relpath}

Resource metadata (from the source's frontmatter):
  resource_id: {resource_id}
  resource_type: {resource_type}
  title: {title}
  topics: {topics}
  quality_score: {quality_score}

Your task:
1. Read CLAUDE.md, purpose.md, and index.md.
2. Read the new source file.
3. Identify entities and concepts mentioned. Use Grep/Glob to find existing
   wiki pages that overlap.
4. Read those overlapping pages.
5. Create wiki/sources/<slug>.md summarizing the source per CLAUDE.md.
6. Update or create the affected entity/concept pages, integrating new
   claims with proper citation back to wiki/sources/<slug>.md.
7. Update index.md with new pages.
8. Append a one-line entry to log.md.
9. git add and git commit with prefix "ingest: <slug>".
10. Call the report_result tool exactly once with your final summary.

Constraints:
- Every claim on a synthesis-style page MUST cite a source page using the
  wikilink syntax described in CLAUDE.md. No uncited claims.
- Do not invent facts not present in the source or already in the wiki.
- If you find a contradiction with an existing page, do NOT silently
  overwrite. Add the new claim with citation, mark the contradiction with
  a "Contradictions" section, and include it in report_result warnings.
- Only edit files under wiki/, raw/parsed/, log.md, or index.md.
- Do not push to any remote. Commits stay local; the host pushes.

Begin.
```

## Lint

```
You are running the daily lint of the personal knowledge base.

Read CLAUDE.md and purpose.md first to refresh the conventions.

Goals (in order of importance):
1. Find dangling [[wikilinks]] — links to files that do not exist.
2. Find orphan pages — pages with no incoming wikilinks at all.
3. Find stub pages — pages with bodies under ~10 lines that should be
   merged or expanded.
4. Find duplicate concepts — multiple pages describing the same idea
   under different names.
5. Find direct contradictions across pages.
6. Verify index.md against the actual filesystem.

Procedure:
- Use Grep/Glob to enumerate pages and links.
- Read suspicious pages individually before flagging.
- Be conservative. False positives are worse than false negatives — the
  owner does not want noise.

Output:
1. Create wiki/syntheses/lint/<YYYY-MM-DD>.md with a markdown digest
   organized by category (Dangling links, Orphans, Stubs, Duplicates,
   Contradictions, Index drift). Each finding is a bullet with the page
   path and a one-line note.
2. Optionally make small auto-fixes:
   - Rebuild index.md if it has drift.
   - Comment out dangling wikilinks (do not delete the line).
   - Tag flagged pages with an HTML comment <!-- lint:<finding-type> -->
     near the top.
3. git add and git commit with prefix "lint: <YYYY-MM-DD>".
4. Call report_result with status, the digest path under pages_created,
   any pages_updated, a one-line log_entry for log.md, a Telegram-friendly
   summary (1–2 sentences citing the largest finding categories), and any
   warnings.

Today's date: {today}
Begin.
```

## Weekly synthesis

```
You are writing the weekly synthesis page.

Read CLAUDE.md and purpose.md.

Time window: {week_start} through {week_end} (ISO week {iso_week_label}).

Procedure:
1. Read log.md and isolate entries within the window.
2. Read the wiki/sources/ pages created in the window.
3. Identify themes — clusters of sources discussing related ideas.
4. Read the entity/concept pages that those sources updated.
5. Look for unexpected connections (sources that overlap in non-obvious
   ways), open questions, and contradictions exposed by the week's reading.

Output:
1. Create wiki/syntheses/weekly/{iso_week_label}.md with the following
   structure (per CLAUDE.md):
   - Frontmatter with week, source_count, theme.
   - Opening: 1–2 sentence theme of the week.
   - Section per theme. Each theme has a 2–4 paragraph synthesis with
     citations to the sources and concept pages it draws from.
   - "Open questions" section listing things still unresolved.
   - "Reading list" section: bullet per source with one-sentence summary.
2. Update relevant entity/concept pages: add a "Mentioned in syntheses"
   bullet with a wikilink to this synthesis, where the connection is
   substantive.
3. git add and git commit with prefix "synthesis: weekly {iso_week_label}".
4. Call report_result with status, pages_created including this synthesis,
   pages_updated, log_entry, summary (the theme + 1 sentence on what was
   notable), and warnings.

Constraints:
- Be honest. If the week was thin (1–2 sources), say so. Do not pad.
- Every claim cites a source. Synthesis is not invention.
- Cap the synthesis at ~1500 words. Dense is better than long.

Begin.
```

## Templating

```python
# app/prompts.py
GATE_SYSTEM_PROMPT = """..."""   # text above
GATE_USER_TEMPLATE = """..."""

INGEST_PROMPT = """..."""
LINT_PROMPT = """..."""
SYNTHESIS_WEEKLY_PROMPT = """..."""

def render_ingest(resource: Resource, parsed_relpath: str, topics: list[str]) -> str:
    return INGEST_PROMPT.format(
        parsed_relpath=parsed_relpath,
        resource_id=resource.id,
        resource_type=resource.resource_type,
        title=resource.content_title or "(untitled)",
        topics=", ".join(topics) or "(none)",
        quality_score=resource.quality_score,
    )
```

## Versioning

When prompts are updated, bump a version constant `PROMPTS_VERSION` and log
it on each call into `events.payload`. This lets you correlate behavior
changes with prompt edits when reading audit history months later.

```python
PROMPTS_VERSION = "2026-05-02-1"
```
