#!/usr/bin/env bash
# Initialize the knowledge base as a separate git repository.
# Usage: ./scripts/init-wiki.sh [path]
# Default path: ../llm-kb-wiki

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WIKI_ROOT="${1:-$SCRIPT_DIR/../../llm-kb-wiki}"
mkdir -p "$WIKI_ROOT"
WIKI_ROOT="$(cd "$WIKI_ROOT" && pwd)"

echo "Initializing knowledge base at: $WIKI_ROOT"

# Create directory structure
mkdir -p "$WIKI_ROOT"/{raw/{inbox,parsed/{web,pdf,youtube,text,voice},rejected},wiki/{entities,concepts,sources,syntheses/{weekly,lint,topics}}}

# Create purpose.md
cat > "$WIKI_ROOT/purpose.md" << 'PURPOSE_EOF'
# Purpose

This is a personal knowledge base for a software engineer interested in
large language models, AI agents, machine learning infrastructure, and
software engineering practices.

## In scope
- Technical articles on LLMs, AI agents, ML systems
- Long-form essays on AI and software engineering
- Conference talks and lectures (YouTube transcripts)
- Research papers (PDFs)
- Voice notes with technical ideas or questions

## Out of scope
- News headlines without lasting value
- Marketing or promotional content
- Personal/work-confidential material

## Depth expected
Pages should be summarized at a level that is useful 6 months from now --
enough to decide whether to revisit the source, and enough to surface
the source via search later. Around 300-800 words per source page.

## Tone
Neutral, factual, citation-heavy. The wiki is a reference, not an opinion blog.
PURPOSE_EOF

# Create CLAUDE.md
cat > "$WIKI_ROOT/CLAUDE.md" << 'CLAUDE_EOF'
# CLAUDE.md — Agent Playbook

## Mission

You maintain this personal knowledge base. Your job is to integrate new sources
into structured, citation-backed wiki pages that compound in value over time.
Every claim you add must cite a source page using wikilink syntax.

## Wiki structure

```
wiki/entities/          One page per person, company, product, tool (Title-Case-Hyphenated.md)
wiki/concepts/          One page per idea, method, theory (lowercase-kebab.md)
wiki/sources/           One page per ingested source (<resource_id_short>-<kebab-title>.md)
wiki/syntheses/weekly/  Weekly synthesis pages (YYYY-Www.md)
wiki/syntheses/lint/    Daily lint digests (YYYY-MM-DD.md)
raw/parsed/             Read-only parsed sources with YAML frontmatter
raw/inbox/              Original attachments, read-only
```

## Page schemas

### Source page (`wiki/sources/<slug>.md`)
```markdown
---
resource_id: <uuid>
resource_type: web|pdf|youtube|text|voice|md
source_url: <url or null>
title: "<title>"
ingested_at: <ISO-8601>
parsed_path: raw/parsed/<type>/<slug>.md
quality_score: <0-100>
topics: [<list>]
---

# <Title>

## TL;DR
One short paragraph. The most compressed possible takeaway.

## Key claims
- Claim 1, in the source's own framing. [[parsed#para1]]
- Claim 2. [[parsed#para3]]

## Notable details
Optional. Anything that does not fit into key claims but is worth keeping.

## Connections
- Mentions [[entities/Name]] in section 3.
- Builds on [[concepts/name]].
- Related to [[sources/other-slug]] (similar methodology).
```

### Entity page (`wiki/entities/<Name>.md`)
```markdown
---
type: entity
created: <ISO-date>
last_updated: <ISO-date>
sources: [<resource_id_short>, ...]
---

# <Name>

## Overview
One paragraph: who/what is this entity.

## Roles and affiliations (or equivalent)
- Fact with citation [[sources/slug]]
- ...

<!-- llm:auto-section -->
## Connections
- Frequently cited alongside [[entities/Other]] in [[concepts/something]].
```

### Concept page (`wiki/concepts/<name>.md`)
```markdown
---
type: concept
created: <ISO-date>
last_updated: <ISO-date>
sources: [<resource_id_short>, ...]
---

# <Display Name>

## Definition
One paragraph defining the concept. Cite the source(s). [[sources/slug]]

## Key claims
- Claim with citation [[sources/slug]]
- ...

## Open questions
- Unresolved question?

<!-- llm:auto-section -->
## Connections
- Related to [[concepts/other]].
```

## Naming conventions

- **Entities**: Title Case with hyphens — `Andrej-Karpathy.md`, `DeepSeek.md`
- **Concepts**: lowercase kebab-case — `mixture-of-experts.md`, `tool-calling.md`
- **Sources**: `<first8chars-of-uuid>-<kebab-title>.md` truncated to 60 chars
- **Syntheses**: `YYYY-Www.md` (weekly), `YYYY-MM-DD.md` (lint)

## Wikilink syntax

- `[[entities/Name]]` — full page
- `[[entities/Name#Section]]` — specific section
- `[[sources/slug#para3]]` — paragraph anchor (1-indexed, ignores frontmatter)
- `[[concepts/name|alias]]` — aliased display text

## When to create vs update

- **Source page**: always create one per ingested source, no exceptions.
- **Entity page**: create if the entity is central to the source OR if it
  has been mentioned in any other prior source. Otherwise, just reference
  via `[[entities/Name]]` and let lint catch orphans later.
- **Concept page**: same rule as entity.
- **Synthesis page**: only created by lint (daily) or synthesis (weekly)
  jobs, not by ingest.

## Ingest workflow

1. Read CLAUDE.md, purpose.md, and index.md.
2. Read the new parsed source file at the path given in the task.
3. Identify entities and concepts mentioned. Use Grep/Glob to find existing
   wiki pages that overlap.
4. Read those overlapping pages.
5. Create `wiki/sources/<slug>.md` summarizing the source per the schema above.
6. Update or create the affected entity/concept pages, integrating new
   claims with proper citation back to the source page.
7. Update `index.md` with new pages.
8. Append a one-line entry to `log.md`:
   `- <ISO-8601> **ingest** [[sources/<slug>]] · created ..., updated ... · commit:<sha>`
9. `git add` and `git commit` with prefix `ingest: <slug>`.
10. Call `report_result` exactly once with your final summary.

## Lint workflow

1. Find dangling `[[wikilinks]]` — links to files that do not exist.
2. Find orphan pages — pages with no incoming wikilinks.
3. Find stub pages — pages with bodies under ~10 lines.
4. Find duplicate concepts — multiple pages describing the same idea.
5. Find direct contradictions across pages.
6. Verify `index.md` against the actual filesystem.

Output a digest at `wiki/syntheses/lint/<YYYY-MM-DD>.md`. Optionally auto-fix:
rebuild index.md, comment out dangling links, tag flagged pages with
`<!-- lint:<finding-type> -->`.

## Synthesis workflow

1. Read `log.md` and isolate entries within the target week.
2. Read the source pages created in the window.
3. Identify themes — clusters of sources discussing related ideas.
4. Look for unexpected connections, open questions, contradictions.

Output at `wiki/syntheses/weekly/<YYYY-Www>.md` with theme of the week,
per-theme synthesis with citations, open questions, and reading list.

## Forbidden actions

- Do NOT edit `purpose.md` (owner-only).
- Do NOT edit content above an `<!-- manual:keep -->` marker.
- Do NOT delete a source page (mark `<!-- llm:superseded by [[...]] -->` instead).
- Do NOT delete claims that have citations (mark them disputed instead).
- Do NOT edit anything under `raw/` (read-only).
- Do NOT push to any remote. Commits stay local.
- Do NOT fabricate facts not present in the source or already in the wiki.
- If you find a contradiction, do NOT silently overwrite. Add the new claim
  with citation, mark the contradiction in a "Contradictions" section, and
  include it in `report_result` warnings.
CLAUDE_EOF

# Create index.md
cat > "$WIKI_ROOT/index.md" << INDEX_EOF
# Index

_Last updated: $(date -u +%Y-%m-%d)_

## Entities (people, companies, products, tools)

<!-- llm:auto-section -->
*(none yet)*

## Concepts (ideas, methods, theories)

<!-- llm:auto-section -->
*(none yet)*

## Sources

<!-- llm:auto-section -->
*(none yet)*

## Syntheses

<!-- llm:auto-section -->
*(none yet)*
INDEX_EOF

# Create log.md
cat > "$WIKI_ROOT/log.md" << 'LOG_EOF'
# Event Log

<!-- Append-only chronological log. One line per event. -->
LOG_EOF

# Initialize git repository
cd "$WIKI_ROOT"
git init
git add -A
git commit -m "manual: initial wiki structure"

echo ""
echo "Knowledge base initialized at: $WIKI_ROOT"
echo "Run 'cd $WIKI_ROOT && git log --oneline' to verify."
