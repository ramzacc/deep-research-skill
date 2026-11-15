---
name: deep-research
description: >
  Runs end-to-end deep research on a question: decompose it into subquestions, search
  many sources in parallel, dig deeper per subquestion, then merge everything into a
  cited markdown report, convert it to PDF, and deliver it on Telegram. Use when the
  user says "deep research", "research X thoroughly", "write a report on X",
  "investigate X", "do a literature/competitive review", or asks for a well-sourced,
  multi-source analysis rather than a quick answer.
version: 1.0.0
author: ramzacc
platforms: [linux, macos]
required_environment_variables:
  - name: DEEP_RESEARCH_EXA_KEY
    prompt: Exa API key for search and page fetching
    help: Same value as your Exa API key. Set it with `hermes config set DEEP_RESEARCH_EXA_KEY <key>`.
    required_for: web search and page fetching
metadata:
  hermes:
    tags: [research, report, citations, web, pdf, telegram]
    category: research
    requires_toolsets: [terminal]
---

# Deep research

You produce a cited report that a careful analyst would accept. Work is done in a fixed
work dir by `${HERMES_SKILL_DIR}/scripts/research.py` and `${HERMES_SKILL_DIR}/scripts/md_to_pdf.py`,
and by delegated researchers.

**Outputs**
- `~/research/<slug>.md` — final report with numbered, linked citations and References.
- `~/research/<slug>.pdf` — same report as PDF, sent to the user via a `MEDIA:` line.
- `~/research/.work/<slug>/` — plan, sources, evidence, findings, draft (resume state).

**Hard rules (never break)**
1. Never invent a URL, source, citation id, number, date or finding. Cite only `S#` ids
   that exist in `<WORK>/sources.json`.
2. Every factual claim in the draft carries a citation: `[S12]`, `[S3, S7]`, or `[S3][S7]`.
3. Do not skip steps and do not reorder them. If a command fails, follow **Failure handling**.
4. Do not paste raw search dumps into the report. Synthesize.
5. Replace `<WORK>` below with the exact absolute path printed by `init`. Shell variables
   do not survive between terminal calls — always write full paths.

---

## Step 0 — Scope (10 seconds)

- If the request is missing the subject, scope, or (for comparative questions) the
  comparison set, ask exactly ONE clarifying question and stop. Otherwise proceed.
- If the request is a quick factual lookup that one search answers, say so and answer it
  directly instead of running this pipeline.
- Otherwise reply with one line: `Starting deep research on "<topic>".` Then continue.

## Step 1 — Init

1. Get today's date: `date +%F`.
2. Pick the slug: `<YYYY-MM-DD>-<3-6 lowercase words, hyphens>`; 2-61 chars, matching
   `^[a-z0-9][a-z0-9-]{1,60}$`. Example: `2026-11-15-eu-ai-act-enforcement`.
3. Run and copy the printed `work dir:` path — it is `<WORK>` for every later command:

```
python3 ${HERMES_SKILL_DIR}/scripts/research.py init --slug <SLUG>
```

Do not create directories by hand.

## Step 2 — Plan

Write `<WORK>/plan.json` with the file write tool (not a shell heredoc). Shape, exactly:

```json
{
  "topic": "one-sentence topic",
  "slug": "<SLUG>",
  "subquestions": [
    {
      "id": "q1",
      "question": "one clear question",
      "queries": ["search query", "another phrasing", "query 2026", "site:official-domain query"]
    }
  ]
}
```

Rules:
- 4-8 subquestions, ids `q1`..`q8` in order, unique.
- Each subquestion covers a DIFFERENT angle. Use this menu: background/definitions;
  current state and timeline; key players/companies/institutions; data and numbers;
  evidence and case studies; criticisms, risks and counter-evidence; recent developments
  (last 12 months); outlook and competing forecasts.
- 3-5 queries per subquestion. Make them diverse: (a) plain question phrasing, (b) an
  alternative vocabulary/synonym phrasing, (c) one with the current year, (d) one aimed at
  primary sources (`report filetype:pdf`, `site:gov`, `site:edu`, `annual report`,
  `statistics`, `dataset`), (e) one adversarial phrasing (`criticism`, `risks`,
  `limitations`, `failed`). Never repeat the same query twice.

Filled-in example (3 of 4+ subquestions shown):

```json
{
  "topic": "How the EU AI Act is being enforced in 2025",
  "slug": "2026-11-15-eu-ai-act-enforcement",
  "subquestions": [
    {"id": "q1", "question": "What obligations does the EU AI Act impose, and which apply in 2025?",
     "queries": ["EU AI Act obligations timeline 2025", "AI Act prohibited practices February 2025",
                 "European Commission AI Act guidelines 2025", "EU AI Act regulation text official journal"]},
    {"id": "q2", "question": "Who enforces the act and what have they done so far?",
     "queries": ["EU AI Office enforcement 2025", "national competent authorities AI Act list",
                 "AI Act enforcement actions fines 2025", "site:digital-strategy.ec.europa.eu AI Act enforcement"]},
    {"id": "q3", "question": "What do companies and critics say about compliance costs and gaps?",
     "queries": ["AI Act compliance cost criticism", "industry reaction EU AI Act 2025",
                 "AI Act loopholes civil society report", "AI Act GPAI obligations criticism 2025"]}
  ]
}
```

4. Validate before searching (offline, no API calls):

```
python3 ${HERMES_SKILL_DIR}/scripts/research.py search --work <WORK> --dry-run
```

Expect `plan ok: N subquestions, M queries`. Any `error:` line → fix plan.json and repeat.

## Step 3 — Breadth

```
python3 ${HERMES_SKILL_DIR}/scripts/research.py search --work <WORK>
```

This runs every query in parallel, dedupes URLs, assigns stable `S#` ids, writes
`<WORK>/sources.json`, and writes `<WORK>/evidence/<qid>.md` per subquestion.

Check the printed summary:
- `queries: X ok, Y failed` — note failed queries; they are skipped, not fatal.
- `unique sources:` — if fewer than 15, add 1-2 new queries to the subquestions with the
  fewest sources (edit plan.json with the write tool), re-run the same command. Ids stay stable.
- Any subquestion with `0` sources: rewrite its queries (broaden wording, drop jargon),
  re-run. If it is still 0 after two tries, continue — report the gap later.

## Step 4 — Depth (delegated researchers)

ONE `delegate_task` call with one task per subquestion — never one call per subquestion:

```
delegate_task(tasks=[<task for q1>, <task for q2>, ...])
```

Fill the per-task template below for each subquestion: replace `<WORK>`, `<QID>`,
`<QUESTION>`, `<TOPIC>`, `<N>` (total subquestions). Paths are already absolute because
this skill was expanded at load time (if any `${HERMES_SKILL_DIR}` is still literal,
replace it with the skill's directory before delegating). Do not let researchers delegate
further.

```
goal: |
  Research subquestion <QID>: <QUESTION>
  Part of a larger report on: <TOPIC>

context: |
  You are one of <N> researchers. Work entirely from the local work dir <WORK>.
  Do not ask questions; you cannot delegate further.

  STEP 1 — Read <WORK>/evidence/<QID>.md (a digest of candidate sources with stable S-ids).

  STEP 2 — Optional, at most 4 search calls and 3 fetch calls total:
    python3 ${HERMES_SKILL_DIR}/scripts/research.py search --work <WORK> --subq <QID> --query "your query"
    (repeat --query up to 3 times per call)
    python3 ${HERMES_SKILL_DIR}/scripts/research.py fetch --work <WORK> --id S12 --max-chars 20000
  Only fetch a page when its evidence excerpt is insufficient. Follow-up searches are
  concurrency-safe. Use the new S-ids they print.

  STEP 3 — Answer the subquestion. Use ONLY facts present in the evidence file or in
  pages you fetched. Cite with S-ids that appear in those outputs. If sources conflict,
  record the conflict instead of picking a side silently. If evidence is missing, say so
  in gaps.

  STEP 4 — Write valid JSON to <WORK>/findings/<QID>.json using the file write tool:
  {"id": "<QID>",
   "question": "<QUESTION>",
   "answer_summary": "3-6 sentences answering the subquestion, each claim followed by its S-id citation",
   "findings": [{"claim": "one verifiable fact with a number/date/name", "source_ids": ["S3"], "confidence": "high"}],
   "contradictions": ["source A says X [S4] while source B says Y [S9]"],
   "gaps": ["what could not be established and why"]}

  Rules for the JSON: 3-8 findings; source_ids are S-ids only, never URLs; confidence is
  exactly "high", "medium" or "low"; contradictions and gaps are arrays (may be empty).
  Validate it with:
    python3 -m json.tool <WORK>/findings/<QID>.json > /dev/null && echo ok
  Then reply with exactly that JSON object and no other prose.

output_schema: {
  "type": "object",
  "required": ["id", "question", "answer_summary", "findings", "contradictions", "gaps"],
  "properties": {
    "id": {"type": "string"},
    "question": {"type": "string"},
    "answer_summary": {"type": "string"},
    "findings": {"type": "array", "items": {"type": "object",
      "required": ["claim", "source_ids", "confidence"],
      "properties": {"claim": {"type": "string"},
                     "source_ids": {"type": "array", "items": {"type": "string"}},
                     "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}},
    "contradictions": {"type": "array", "items": {"type": "string"}},
    "gaps": {"type": "array", "items": {"type": "string"}}
  }
}
```

After the call: if the result already contains the researchers' results, continue immediately
to `status`. Otherwise say `N researchers running.` and **END YOUR TURN**; results arrive as a
new message. Do not poll, do not sleep, do not do the work yourself while waiting.

When results arrive (or when you resume):
1. `python3 ${HERMES_SKILL_DIR}/scripts/research.py status --work <WORK>`
2. Every subquestion listed under `findings:` as missing → research it YOURSELF, one at a
   time: read its evidence file, optionally run up to 4 ad-hoc searches and 3 fetches,
   then write `<WORK>/findings/<qid>.json` in the same shape with the write tool.
3. If `delegate_task` is unavailable or errors: do all subquestions yourself, sequentially,
   same shape, same files.

## Step 5 — Synthesize

1. Read every `<WORK>/findings/*.json`. Read `<WORK>/evidence/<qid>.md` where a finding
   needs more detail. Do not read all of sources.json into context.
2. Copy the skeleton, then fill it in with the write tool:

```
cp ${HERMES_SKILL_DIR}/templates/report.md <WORK>/draft.md
```

3. Rules for `draft.md`:
   - Title: concrete and specific. Keep the date + research-question line from the template.
   - `## Executive summary`: 5-10 bullets, each ending in `[S#]`, strongest findings first.
   - 4-8 thematic `##` sections. Organize by THEME, not one section per subquestion; merge
     overlapping subquestions. 1-3 paragraphs each.
   - Every factual paragraph has at least one `[S#]`. No uncited numbers. Put citations
     where the claim is, not only at the end of the section.
   - `## Disagreements & uncertainties`: contradictions from the findings files, cited on
     both sides; also low-confidence claims.
   - `## Methodology`: real counts (subquestions from plan.json, sources from the
     `sources.json` `sources` array length, queries from the search summaries) and today's date.
   - No `## References` section — `finalize` adds it. No HTML comments left in the body.

## Step 6 — Finalize

```
python3 ${HERMES_SKILL_DIR}/scripts/research.py finalize --work <WORK> --draft <WORK>/draft.md --out ~/research/<SLUG>.md
```

Success prints `report: /abs/path`, a citation count and coverage. Fix and repeat on:
- `error: ... unknown source ids` → the listed ids are not in sources.json. Either remove
  the claim or search for the missing evidence
  (`python3 ${HERMES_SKILL_DIR}/scripts/research.py search --work <WORK> --subq <QID> --query "..."`),
  then cite the new id. Never invent a replacement id.
- `error: the draft contains no citations` → rewrite with citations.
- `warning: only ...% of body paragraphs contain a citation` → add citations to the weak
  paragraphs (the report is still written; do not ignore this).

## Step 7 — PDF

```
python3 ${HERMES_SKILL_DIR}/scripts/md_to_pdf.py ~/research/<SLUG>.md
```

Success prints `pdf: /abs/path` and a size. Confirm the path with the file read tool if in
doubt. On the first run it installs a private venv (~1 minute); later runs are fast.

## Step 8 — Deliver

Final reply, nothing else after the MEDIA line:

```
Deep research on "<topic>" is ready.

Key findings:
- <finding with citation number, e.g. [1]>
- <finding>
- <finding>

Report: ~/research/<SLUG>.md (PDF attached)
MEDIA:/absolute/path/to/<SLUG>.pdf
```

The gateway attaches the PDF on Telegram because of the `MEDIA:` line; use the absolute
path printed by step 7, one line, no backticks and no other text after it. If the channel
cannot take files, say so and give the report path instead.

---

## Resuming

The pipeline is resumable at any point. Run:

```
python3 ${HERMES_SKILL_DIR}/scripts/research.py status --work <WORK>
```

Read the `[x]`/`[ ]` lines and `next:`, then continue at that step only. Never redo
completed work; re-running `search` is safe (ids are stable), and `finalize` is repeatable.

## Failure handling

- `DEEP_RESEARCH_EXA_KEY is not set` → tell the user to run
  `hermes config set DEEP_RESEARCH_EXA_KEY <their Exa key>` and re-run. Do not fake results.
- `HTTP 401/403` → the key is invalid or exhausted. Report it; do not retry in a loop.
- `HTTP 429/5xx` → the script already retried 3 times with backoff. Re-run the same
  command once; if it still fails, tell the user which queries failed and continue with
  the sources already on disk.
- A query fails but others succeed → continue; mention the gap in `## Methodology` if it
  affected a subquestion.
- `plan.json is invalid` → fix exactly the printed lines; `--dry-run` until clean.
- Empty results → broaden wording, remove quotes/jargon, add a site: or year variant.
- PDF: `python3-venv`/pip error → relay the script's suggested fix (`apt install
  python3-venv` on Debian/Ubuntu). The markdown report is still valid; deliver it and say
  the PDF failed.
- `delegate_task` error or no results message → do the subquestions yourself sequentially
  (step 4 fallback), then continue.

## Budget defaults

- 4-8 subquestions, 3-5 queries each, 8 results per query, 6000 chars per source.
- Per subquestion researcher: max 4 follow-up search calls (≤3 queries each) and 3 fetches.
- Typical run: 20-40 searches in step 3 plus ≤32 follow-ups. The script prints run and
  cumulative `cost` — include it in `## Methodology` if it is above $1.
- Do not raise `--workers` above 10 (Exa rate limits) and do not raise results per query
  above 10 (noise, not coverage).
