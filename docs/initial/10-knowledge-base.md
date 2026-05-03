# Knowledge Base Conventions

This file describes the wiki itself: what files live in it, what they look
like, and how the agent is expected to maintain them. The agent's actual
"playbook" is `knowledge_base/CLAUDE.md`, which the SDK auto-loads. This doc
is the spec; `CLAUDE.md` is the executable version of it.

## Top-level files

### `purpose.md`

A plain-English description of what this knowledge base is for, written by
the owner. Loaded into every gate and ingest call. Fits in roughly 200–500
words.

A good `purpose.md` answers:
- Who is the owner and what are their interests?
- What broad topics belong here?
- What kinds of sources are typical (research, news, tutorials, blogs)?
- What level of depth is expected (notes vs deep summaries)?
- What does NOT belong (e.g. personal todo, work-confidential content)?

Example skeleton:

```markdown
# Purpose

This is a personal knowledge base for <Owner>, a software engineer
interested in <topic1>, <topic2>, and <topic3>.

## In scope
- Technical articles on <area>
- Long-form essays on <area>
- Conference talks and lectures (YouTube transcripts)
- Research papers (PDFs)

## Out of scope
- News headlines without lasting value
- Marketing or promotional content
- Personal/work-confidential material

## Depth expected
Pages should be summarized at a level that is useful 6 months from now —
enough that the owner can decide whether to revisit the source, and enough
to surface the source via search later. Around 300–800 words per source page.

## Tone
Neutral, factual, citation-heavy. The wiki is a reference, not an opinion blog.
```

### `CLAUDE.md`

The agent playbook. Sections:

1. **Mission** — one paragraph mirroring `purpose.md`.
2. **Wiki structure** — the directory layout below.
3. **Page schemas** — frontmatter and section structure for each page type.
4. **Wikilink and citation syntax** — `[[entities/Name]]`, `[[sources/slug#para3]]`.
5. **Naming conventions** — entity Title-Case-Hyphenated, concept lowercase-kebab.
6. **When to create a new page vs update** — entity/concept created when
   mentioned in 2+ sources OR when central to current source.
7. **Ingest workflow** — the 10-step procedure also enumerated in the
   ingest prompt.
8. **Lint workflow** — what daily lint checks for.
9. **Synthesis workflow** — what weekly synthesis produces.
10. **Forbidden actions** — no fabrication, no removing cited claims, no
    overwriting human-edited sections (marked by `<!-- manual:keep -->`).

`CLAUDE.md` is the most important artifact in the system. It is the only
thing standing between Claude and a chaotic wiki. Treat it like code: review
diffs carefully, version it in git, write tests if you can.

### `index.md`

Auto-maintained by the agent. A flat list of all wiki pages grouped by
category. Used by:
- The owner, for navigation in Obsidian.
- The agent, as a quick scan of "what already exists" before deciding to
  create a new page.

Format:

```markdown
# Index

_Last updated: 2026-05-02_

## Entities (people, companies, products, tools)
- [[entities/Andrej-Karpathy]]
- [[entities/Anthropic]]
- ...

## Concepts (ideas, methods, theories)
- [[concepts/mixture-of-experts]]
- [[concepts/rlhf]]
- ...

## Sources
- [[sources/0193f7a8-great-article]] — Great Article on Foo (web, 2026-05-02)
- ...

## Syntheses
- [[syntheses/weekly/2026-W18]]
- ...
```

The agent regenerates `index.md` whenever it adds or removes a page.

### `log.md`

Append-only chronological event log. One line per event. Used by:
- The owner, as a "what happened in the wiki" timeline.
- The agent, especially weekly synthesis, to see what was ingested.

Format (append at end, never edit existing lines):

```markdown
- 2026-05-02T14:32:11Z **ingest** [[sources/0193f7a8-great-article]] · created [[entities/Andrej-Karpathy]], updated [[concepts/llm-agents]] · `commit:abc1234`
- 2026-05-02T19:05:42Z **ingest** [[sources/01940a2b-rlhf-paper]] · updated [[concepts/rlhf]], [[entities/Anthropic]] · `commit:def5678`
- 2026-05-03T02:00:18Z **lint** [[syntheses/lint/2026-05-03]] · 2 orphans, 1 dangling link · `commit:ghi9012`
```

## Page schemas

### Source page (`wiki/sources/<slug>.md`)

The summary of one ingested source. Owned by the agent, never edited by the
owner directly (use the parsed source in `raw/parsed/` instead).

```markdown
---
resource_id: 0193f7a8-3c8b-7e2a-9f4c-2c9e7d3a1b6e
resource_type: web
source_url: https://example.com/great-article
title: "Great Article on Foo"
ingested_at: 2026-05-02T14:32:11Z
parsed_path: raw/parsed/web/0193f7a8-great-article.md
quality_score: 82
topics: [llm-agents, evals]
---

# Great Article on Foo

## TL;DR
One short paragraph. The most compressed possible takeaway.

## Key claims
- Claim 1, in the source's own framing.
- Claim 2.
- Claim 3.

## Notable details
Optional. Anything that does not fit into key claims but is worth keeping.

## Connections
- Mentions [[entities/Andrej-Karpathy]] in section 3.
- Builds on [[concepts/llm-agents]] (defined here in 2024).
- Related to [[sources/01940a2b-rlhf-paper]] (similar methodology).

## Citations
References from this page point back to the parsed source by paragraph
anchor: `[[parsed#para3]]` resolves to the third paragraph of the parsed
file. The agent inserts these inline alongside claims that need backing.
```

### Entity page (`wiki/entities/<Name>.md`)

```markdown
---
type: entity
created: 2026-05-02
last_updated: 2026-05-02
sources: [0193f7a8, 01940a2b]
---

# Andrej Karpathy

## Overview
One paragraph: who/what is this entity.

## Roles and affiliations
- Founding member of OpenAI (2015–2017) [[sources/...]]
- Senior Director of AI at Tesla (2017–2022) [[sources/...]]
- Independent (2024–) [[sources/...]]

## Notable works
- "LLM Wiki" gist (2026) — proposed an LLM-curated knowledge base. [[sources/0193f7a8]]
- ...

## Mentioned in syntheses
- [[syntheses/weekly/2026-W18]]

<!-- llm:auto-section -->
## Connections
- Frequently cited alongside [[entities/Yann-LeCun]] in [[concepts/llm-history]].
```

### Concept page (`wiki/concepts/<name>.md`)

```markdown
---
type: concept
created: 2026-05-02
last_updated: 2026-05-02
sources: [01940a2b, 0193f7a8]
---

# LLM Agents

## Definition
One paragraph defining the concept as the wiki understands it. Cite the
source(s) where the definition was drawn.

## Key claims
- Agents differ from chat assistants in that they take actions in
  environments with tools. [[sources/...]]
- Agentic loops are bounded by max-turns and tool budgets. [[sources/...]]

## Variants and approaches
- ReAct [[sources/...]]
- Tool-use protocols (MCP) [[sources/...]]

## Open questions
- How to evaluate long-horizon agentic behavior?

## Mentioned in syntheses
- [[syntheses/weekly/2026-W18]]
```

### Synthesis page

```markdown
---
type: synthesis
kind: weekly
week: 2026-W18
window_start: 2026-04-27
window_end: 2026-05-03
source_count: 6
themes: ["agentic patterns", "evals"]
---

# Weekly Synthesis · 2026-W18 · Agentic Patterns for LLMs

## Theme of the week
This week's reading clustered around how LLM agents are structured...

## Agentic patterns
3 sources this week described variations of the same pattern. [[sources/...]]
build on [[concepts/llm-agents]] by emphasizing... while [[sources/...]] argues...

## Evals
[[sources/...]] proposed a novel eval framework for agents that complements
[[concepts/evals]].

## Open questions
- ...

## Reading list
- [[sources/0193f7a8]] — Great Article on Foo. Discusses ...
- [[sources/01940a2b]] — RLHF paper. Argues ...
```

## Wikilink syntax

The wiki uses Obsidian-compatible wikilinks: `[[path/Name]]`. Anchors point
to specific sections or paragraphs:

- `[[entities/Andrej-Karpathy]]` — full page
- `[[entities/Andrej-Karpathy#Roles and affiliations]]` — section
- `[[sources/0193f7a8-great-article#para3]]` — paragraph anchor
- `[[concepts/llm-agents|agents]]` — alias text

Paragraph anchors `#paraN` are 1-indexed counts within the body of the
target file, ignoring frontmatter. The agent generates them when it
synthesizes; the owner can ignore them when reading.

## When to create vs update

The agent follows this rule from `CLAUDE.md`:

- **Source page**: always create one per ingested source, no exceptions.
- **Entity page**: create if the entity is central to the source OR if it
  has been mentioned in any other prior source. Otherwise, just reference
  it via `[[entities/Name]]` and let lint catch it later if it stays
  orphaned.
- **Concept page**: same rule as entity.
- **Synthesis page**: only created by lint (daily) or synthesis (weekly)
  jobs, not by ingest.

## Forbidden zones

The agent must never:

- Edit `purpose.md` (owner-only).
- Edit content above an `<!-- manual:keep -->` marker on any page.
- Delete a source page (sources are immutable; superseded sources can be
  marked `<!-- llm:superseded by [[...]] -->` instead).
- Delete claims that have citations (it can mark them disputed).
- Edit anything under `raw/` (read-only for the agent).
- Edit anything outside `knowledge_base/` (enforced by Docker).
- Run `git push`, `git remote`, `git reset`, `git rebase`, `git merge`
  (enforced by Bash hook allowlist).

## Auto-section markers

Sections owned by the agent are tagged with HTML comments:

```html
<!-- llm:auto-section -->
## Connections
...
```

Sections without this marker are owned by the human (or the original agent
that authored them as immutable). The lint job verifies that auto-sections
are still consistent and that human sections are untouched since their
last manual commit.
