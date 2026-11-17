---
name: deep-research
description: >
  Runs end-to-end deep research on a question: decompose it into subquestions, search
  many sources in parallel, dig deeper per subquestion, then merge everything into a
  cited markdown report and deliver it on Telegram. Use when the
  user says "deep research", "research X thoroughly", "write a report on X",
  "investigate X", "do a literature/competitive review", asks in Spanish for
  "investigación profunda", "investiga a fondo", "hazme un informe/reporte sobre", or
  "investiga X", or asks for a well-sourced, multi-source analysis rather than a quick answer.
version: 1.0.0
author: ramzacc
platforms: [linux, macos]
required_environment_variables:
  - name: DEEP_RESEARCH_DEEPSEEK_KEY
    prompt: DeepSeek API key for search and page fetching
    help: Set it with `hermes config set DEEP_RESEARCH_DEEPSEEK_KEY <key>`.
    required_for: web search and page fetching
metadata:
  hermes:
    tags: [research, report, citations, web, telegram]
    category: research
    requires_toolsets: [terminal]
---

# Deep research

You produce a cited report that a careful analyst would accept. Work is done in a fixed
work dir by `${HERMES_SKILL_DIR}/scripts/research.py`, and by delegated researchers.

**Outputs**
- `~/research/<slug>.md` — final report with numbered, linked citations and References,
  delivered to the user as a document via a `MEDIA:` line.
- `~/research/.work/<slug>/` — plan, sources, evidence, findings, draft (resume state).

**Hard rules (never break)**
1. Never invent a URL, source, citation id, number, date or finding. Cite only `S#` ids
   that exist in `<WORK>/sources.json`.
2. Every factual claim in the draft carries a citation: `[S12]`, `[S3, S7]`, or `[S3][S7]`.
3. Do not skip steps and do not reorder them. If a command fails, follow **Failure handling**.
4. Do not paste raw search dumps into the report. Synthesize.
5. Replace `<WORK>` below with the exact absolute path printed by `init`. Shell variables
   do not survive between terminal calls — always write full paths.
6. Health, drug or legal-risk topics: report what sources say about options, availability,
   prices, monitoring and risks. Do NOT give step-by-step instructions for compounding,
   manufacturing or sourcing veterinary/unregulated products. Put a risk warning next to
   any mention of them.
7. A proper name (person, group, lab, program, scholarship, product) may appear in the report
   only if it appears literally in an evidence excerpt or a fetched page. Otherwise leave it
   out, or list it under Not verified.

---

## Step 0 — Scope (10 seconds)

- If the request is missing the subject, scope, or (for comparative questions) the
  comparison set, ask exactly ONE clarifying question and stop. Otherwise proceed.
- If the request is a quick factual lookup that one search answers, say so and answer it
  directly instead of running this pipeline.
- If the user asked for a follow-up action with the results (update a task, send a message,
  write a file), remember its verbatim intent for `post_actions` in plan.json.
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
  "asks": ["every explicit request in the user's prompt, one item each"],
  "user_context": "profile/constraints from the prompt plus what Hermes knows about the user; empty string is allowed",
  "identify": ["specific artifact the prompt refers to, if any"],
  "post_actions": ["follow-up action the user asked for with the results, verbatim intent"],
  "subquestions": [
    {
      "id": "q1",
      "question": "one clear question",
      "queries": ["search query", "another phrasing", "query 2026", "site:official-domain query"],
      "covers": [0]
    }
  ]
}
```

Rules:
- `asks`: one item per explicit request in the user's prompt. Do not invent asks. At least one.
- `user_context`: the user's constraints/goals from the prompt plus anything Hermes knows from
  memory or the conversation. Keep it factual; `""` is valid when there is nothing to add.
- `identify`: optional list of specific artifacts the prompt refers to by reference (a guide,
  report, post or page). Omit it or use `[]` when none. If the prompt says "a guide someone
  wrote on forum X", this is an artifact: the subject is the canonical document, not the forum
  thread that mentions it.
- `post_actions`: optional list, default `[]`. Record every follow-up action the user asked for
  with the results, verbatim intent (for example "update task Y with the results" or
  "write the report path to ~/done.txt"). The post-actions step runs them after finalize; they
  must survive the background-researcher wake, so re-read plan.json (or run `status`) later.
- If `identify` is non-empty, `q1` MUST be exactly:
  `Identify exactly which <artifact> is meant: its canonical name, current URL and authorship
  history`. Use official/primary queries for q1 and prefer the canonical, maintained document
  over the forum thread that mentions it.
- `covers`: indices into `asks` (0-based) that this subquestion answers. Every ask must be
  covered by at least one subquestion; q1 may cover the identification ask. An empty list is
  allowed only for a pure identification subquestion.
- 4-8 subquestions, ids `q1`..`q8` in order, unique.
- Each subquestion covers a DIFFERENT angle. Use this menu: background/definitions;
  current state and timeline; key players/companies/institutions; data and numbers;
  evidence and case studies; criticisms, risks and counter-evidence; recent developments
  (last 12 months); outlook and competing forecasts.
- If the request is a decision (choose, buy, apply, move, start, compare options to act on),
  make the LAST subquestion an execution subquestion: "What does acting on the best options
  require for this user: steps and paperwork, timeline, total cost, main risks and a plan B?"
  It covers the asks about the decision itself. Skip it for purely informational requests.
- 4-6 queries per subquestion, each 128 characters or fewer. Make them diverse: (a) plain
  question phrasing, (b) a synonym or alternate-language phrasing (English plus the user's
  language when the topic is local), (c) one official/primary query using `site:` on the
  relevant government, university or official domain or country TLD (e.g.
  `site:gob.pe tarifa electrica`), (d) when the user is in a specific country and the
  subquestion involves prices, availability, shops, providers or regulation, one local-market
  query with the country TLD (e.g. `site:.pe precio balon gas`), (e) one adversarial phrasing
  (`criticism`, `risks`, `limitations`, `failed`), (f) for any subquestion with prices, fees,
  tariffs, deadlines or availability, one query naming the current month and year in the
  user's language (e.g. `tarifa luz noviembre 2026`, `precio balon gas noviembre 2026`) to
  catch recent news. Never repeat the same query twice.
- Operators that work in DeepSeek search: `site:`, `-site:`, `"exact phrase"`, `lang:`,
  `after:`. Use `after:` only for news-type queries, never together with `lang:` (that
  combination returns nothing), and remember it drops undated official documents. Do not use
  `after:` to get freshness: name the current month and year instead.

Filled-in example (3 of 4+ subquestions shown):

```json
{
  "topic": "How the EU AI Act is being enforced in 2025",
  "slug": "2026-11-15-eu-ai-act-enforcement",
  "asks": [
    "Explain the obligations that apply in 2025",
    "Explain who enforces the act and what they have done",
    "Summarize compliance costs and criticism"
  ],
  "user_context": "Policy analyst; needs a short brief, not legal advice.",
  "identify": [],
  "post_actions": [],
  "subquestions": [
    {"id": "q1", "question": "What obligations does the EU AI Act impose, and which apply in 2025?",
     "queries": ["EU AI Act obligations timeline 2025", "AI Act prohibited practices February 2025",
                 "European Commission AI Act guidelines 2025", "EU AI Act regulation text official journal"],
     "covers": [0]},
    {"id": "q2", "question": "Who enforces the act and what have they done so far?",
     "queries": ["EU AI Office enforcement 2025", "national competent authorities AI Act list",
                 "AI Act enforcement actions fines 2025", "site:digital-strategy.ec.europa.eu AI Act enforcement"],
     "covers": [1]},
    {"id": "q3", "question": "What do companies and critics say about compliance costs and gaps?",
     "queries": ["AI Act compliance cost criticism", "industry reaction EU AI Act 2025",
                 "AI Act loopholes civil society report", "AI Act GPAI obligations criticism 2025"],
     "covers": [2]}
  ]
}
```

4. Validate before searching (offline, no API calls):

```
python3 ${HERMES_SKILL_DIR}/scripts/research.py search --work <WORK> --dry-run
```

Expect `plan ok: N subquestions, M queries`. Any `error:` line → fix plan.json and repeat.
The dry run also checks that every ask is covered, every `covers` index is valid, and the
`identify`/q1 rule is met; it fails with the exact field to fix.

## Step 3 — Breadth

```
python3 ${HERMES_SKILL_DIR}/scripts/research.py search --work <WORK>
```

This runs every query in parallel, dedupes URLs, assigns stable `S#` ids, writes
`<WORK>/sources.json`, and writes `<WORK>/evidence/<qid>.md` per subquestion. Each request
asks for 20 results and sends the subquestion text as `ori_question`; the full page of every
result is cached in `<WORK>/pages/<S#>.md`, so `fetch` usually costs nothing.

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
  User context: <USER_CONTEXT>
  Asks this subquestion covers: <ASK_INDICES: the exact ask text for each index>
  Today: <TODAY, YYYY-MM-DD>

context: |
  You are one of <N> researchers. Work entirely from the local work dir <WORK>.
  Do not ask questions; you cannot delegate further.

  STEP 1 — Read <WORK>/evidence/<QID>.md (a digest of candidate sources with stable S-ids).
  It quotes the passages of each page that best match this subquestion, and lists weaker
  candidates as one-liners you can still fetch. Sources are ranked by DeepSeek rerank plus a
  recency bonus (a 90-day-old page gets +0.6, an old undated PDF can get -0.5), so an official
  2021 PDF does not outrank this month's notice. The tags `social/forum`, `low-authority` and
  `possibly-outdated` mark weak sources: use them as leads, never as the only support for a
  number, and check freshness with `recent --grep` before using a value.

  STEP 2 — Follow-up research: at most 6 search calls. Fetch at least 3 and at most 8 pages
  total (fetch is cheap — cached pages are free):
    python3 ${HERMES_SKILL_DIR}/scripts/research.py search --work <WORK> --subq <QID> --query "your query"
    (repeat --query up to 3 times per call)
    python3 ${HERMES_SKILL_DIR}/scripts/research.py fetch --work <WORK> --id S12
    python3 ${HERMES_SKILL_DIR}/scripts/research.py fetch --work <WORK> --id S12 --grep "5.699" --grep "tarifa"
    python3 ${HERMES_SKILL_DIR}/scripts/research.py fetch --work <WORK> --url https://... --subq <QID>
  If an official page URL that would answer the question already appears in the evidence,
  fetch it instead of searching again. Follow-up searches are concurrency-safe; use the new
  S-ids they print.

  MANDATORY for every price, fee, tariff, deadline, requirement or decision-critical number:
    (a) fetch the cited page with `--grep` for the value and confirm it in the full text.
        `no matches` means the value is NOT on that page: fix the source or drop the claim.
    (b) record the unit and pack size exactly as the page states it — per unit, per box of N,
        per month, currency, incl./excl. tax — in the claim and in the `unit` field.
    (c) freshness check before accepting the value: run
        `python3 ${HERMES_SKILL_DIR}/scripts/research.py recent --work <WORK> --grep "<thing>" "<unit>"`
        (local, no request) and read the newest rows. If a newer source reports a change, fetch
        it (`--grep` the value) and use the newest official value, recording its `as_of` date.
        If nothing newer is registered, run one search naming the current month and year (e.g.
        "tarifa luz noviembre 2026"). An older official PDF is not the current value, however
        high its authority or rerank score.
    (d) if the only source is tagged social/forum or low-authority, corroborate it with a
        second independent source or set `confidence` to "low".
  Prefer authority >= 3 and official/primary pages. A forum post, Q&A page, social media,
  WeChat, LinkedIn or a blog never settles a price, requirement or deadline on its own.

  STEP 3 — Answer the subquestion. Use ONLY facts present in the evidence file or in pages
  you fetched. Cite with S-ids that appear in those outputs. If sources conflict, record the
  conflict instead of picking a side silently. If evidence is missing, say so in gaps.
  For every date, set date_status relative to today: "open", "past" or "unknown". Never
  present a past deadline as actionable; if the sources show the next cycle, give it.
  Proper names: include a person/group/lab/program/scholarship/product only if it appears
  literally in the evidence file or a fetched page. Otherwise omit it or put it in gaps.

  STEP 4 — Write valid JSON to <WORK>/findings/<QID>.json using the file write tool:
  {"id": "<QID>",
   "question": "<QUESTION>",
   "answer_summary": "3-6 sentences answering the subquestion, each claim followed by its S-id citation",
   "findings": [{"claim": "one verifiable fact with a number/date/name", "source_ids": ["S3"],
                 "confidence": "high", "verified": true, "date_status": "open",
                 "as_of": "2026-11", "unit": "S/ per kWh, incl. IGV"}],
   "contradictions": ["source A says X [S4] while source B says Y [S9]"],
   "gaps": ["what could not be established and why"]}

  Rules for the JSON: 3-8 findings; source_ids are S-ids only, never URLs; confidence is
  exactly "high", "medium" or "low"; verified is true only when the claim was confirmed with
  `fetch --grep` in the fetched full text, false otherwise; date_status is required on date
  claims and is exactly "open", "past" or "unknown" (omit it for non-date claims); as_of is
  required on every finding that carries a value (price, fee, quantity, rate, deadline) and is
  "YYYY-MM", "YYYY-MM-DD" or "unknown" — the date the value is valid as of, from the page;
  unit is required when the claim has a price or quantity and repeats the page's wording
  ("S/ 14.90 per box of 12", "US$ 40 per month, excl. tax"); contradictions and gaps are
  arrays (may be empty). Validate it with:
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
      "required": ["claim", "source_ids", "confidence", "verified"],
      "properties": {"claim": {"type": "string"},
                     "source_ids": {"type": "array", "items": {"type": "string"}},
                     "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                     "verified": {"type": "boolean"},
                     "date_status": {"type": "string", "enum": ["open", "past", "unknown"]},
                     "as_of": {"type": "string"},
                     "unit": {"type": "string"}}}},
    "contradictions": {"type": "array", "items": {"type": "string"}},
    "gaps": {"type": "array", "items": {"type": "string"}}
  }
}
```

After the call: if the result already contains the researchers' results, continue immediately
to `status`. Otherwise tell the user `N researchers running.` and **END YOUR TURN**; results
arrive as a new message. Do not poll, do not sleep, do not do the work yourself while waiting.

When results arrive (or when you resume):
1. `python3 ${HERMES_SKILL_DIR}/scripts/research.py status --work <WORK>` (it prints
   `post_actions`; re-read plan.json so they survive the wake).
2. Every subquestion listed under `findings:` as missing → research it YOURSELF, one at a
   time: read its evidence file, optionally run up to 6 ad-hoc searches and fetch 3-8 pages,
   then write `<WORK>/findings/<qid>.json` in the same shape with the write tool.
3. If `delegate_task` is unavailable or errors: do all subquestions yourself, sequentially,
   same shape, same files.

## Step 4b — Gap round

Iterative deepening: run this once, after every findings file exists and before you
synthesize. Do not skip it silently.

1. Read all `<WORK>/findings/*.json` and list:
   - gaps the researchers reported,
   - contradictions between findings,
   - decision-critical findings (prices, fees, tariffs, requirements, eligibility, deadlines)
     with `verified: false` or `confidence: "low"`,
   - asks in `plan.json` whose coverage is thin: no number where a number is needed, one weak
     source, or only social/forum/low-authority support.
2. Pick the top ≤8 items by how much they change the answer; skip cosmetic ones.
3. Staleness audit (mandatory): for every value that will appear in Summary or What to do,
   run `python3 ${HERMES_SKILL_DIR}/scripts/research.py recent --work <WORK> --grep "<value>" "<thing>"`
   and read the newest rows. If a newer source contradicts the finding, fetch it and resolve
   the conflict (update `claim`, `source_ids`, `as_of`) before synthesis. Recency beats
   formality for "the current value": an old official PDF loses to this month's notice.
4. For each item run at most 2 targeted calls YOURSELF:
   `python3 ${HERMES_SKILL_DIR}/scripts/research.py search --work <WORK> --subq <QID> --query "..."`
   and/or `fetch --work <WORK> --id S# --grep "value"` (or `fetch --url ... --subq <QID>`).
5. Append the result to the relevant `<WORK>/findings/<qid>.json`: a new finding, or a
   corrected one (fix `claim`, `source_ids`, `confidence`, `verified`, `as_of`, `unit`).
   Keep the file valid JSON.
6. If nothing qualifies, say "no gap round needed" and go on.

Then synthesize from the updated findings.

## Step 5 — Synthesize

1. Read every `<WORK>/findings/*.json`. Read `<WORK>/evidence/<qid>.md` where a finding
   needs more detail. Do not read all of sources.json into context.
2. Copy the skeleton, then fill it in with the write tool:

```
cp ${HERMES_SKILL_DIR}/templates/report.md <WORK>/draft.md
```

3. Write the title and every heading in the language of the user's request. Use the
   localized names below (English / Spanish).
4. Required structure, in this order:
   - Title (concrete, not "Research Report") + the date and research-question line.
   - `## Summary` / `## Resumen`: at most 6 bullets, strongest first, each ending in `[S#]`.
   - 3-7 thematic `##` sections: the general picture, organized by theme or option. Cover the
     asks in plan.json; do not add topics nobody asked for.
   - `## Disagreements & uncertainties` / `## Contradicciones e incertidumbres`: contradictions
     from the findings files, cited on both sides; also low-confidence claims.
   - `## Not verified` / `## No verificado`: every claim used with `verified: false`, plus the
     reason it could not be confirmed.
   - `## What to do` / `## Qué hacer`: the actionable conclusion. Numbered concrete steps
     tailored to `user_context`, each with its date or deadline (open ones only), plus a short
     shortlist or decision table. Every step has a `[S#]` citation.
   - `## Assumptions` / `## Supuestos`: required whenever the report computes anything. A table
     with each input, value, unit and source id (or "assumption"/"supuesto" and why). Use ONE
     exchange rate, cited, everywhere; cost totals must list every component.
   - `## Methodology` / `## Metodología`: real counts (subquestions, sources, queries), the
     search spend the script printed, and today.
5. Rules for every section: every factual paragraph has at least one `[S#]`; no uncited
   numbers; put the citation where the claim is, not only at the end. Prefer tables over
   prose: whenever you compare options, amounts, requirements, deadlines, steps or risks, put
   them in a markdown table (one row per option, one column per attribute, citations inside
   the cells) and keep the prose around it to 1-3 sentences that say what the table means.
   Do not pad: a typical report is 4,000-7,000 words. Do not leave HTML comments in the body. Do
   not add a References heading — `finalize` adds the localized one. Sources appear ONLY as
   citation brackets (`[S12]`, `[S3, S7]`), in tables and prose alike: never write a bare `S12`
   anywhere, because it is not a citation and `finalize` rejects it.
6. Before finalizing, re-read the whole draft once and fix these six things:
   - **Cite the right source.** A number or proper name next to `[S#]` must appear in S#'s
     text. If it came from another source, cite that one; if you cannot find it, move it to
     Not verified.
   - **Mark your own calculations.** Any number you computed (conversions, totals, monthly
     costs, break-even) gets `≈` in front of it and cites the inputs, e.g. `≈ US$ 1,309 (cálculo:
     S/ 4,500 ÷ 3.438) [S2][S7]`. Recompute every computed row from the Assumptions table; one
     exchange rate everywhere.
   - **One fact, one value.** If the same fact appears with two values (a fee, a requirement,
     an eligibility rule), keep the best-sourced one everywhere, or explain the conflict in
     Disagreements.
   - **Act on current facts only.** Every time-sensitive fact used in What to do (a person's
     current affiliation, a price, a deadline, a requirement, availability) must come from a
     page you fetched in this run and that shows it is current. If not, fetch the current page
     (`fetch` costs almost nothing) or drop the step.
   - **Units and pack sizes match the source.** If the page says "S/ 14.90 per box of 12", the
     report says per box, not per unit; if it says "per month", do not present it as per year.
     Never convert, divide or multiply without `≈` and a cited input. State the currency and
     whether tax is included, exactly as the page does.
   - **Every value in What to do has an as_of date and the newest official source.** Check
     each value against its finding's `as_of`; if a newer official value exists (a newer
     tariff, fee schedule, regulation or price list), use it everywhere the old one appeared
     and note the "as of" date in the step.

## Step 6 — Finalize

```
python3 ${HERMES_SKILL_DIR}/scripts/research.py finalize --work <WORK> --draft <WORK>/draft.md --out ~/research/<SLUG>.md --lang en
```

Use `--lang en` for an English request. Use `--lang es` when the user's request is Spanish.
If unsure, omit `--lang`; the script auto-detects the language.
`finalize` adds `## References` or `## Referencias` in that language.

Success prints `report: /abs/path`, a citation count and coverage. Fix and repeat on:
- `error: ... unknown source ids` → the listed ids are not in sources.json. Either remove
  the claim or search for the missing evidence
  (`python3 ${HERMES_SKILL_DIR}/scripts/research.py search --work <WORK> --subq <QID> --query "..."`),
  then cite the new id. Never invent a replacement id.
- `error: the draft contains no citations` → rewrite with citations.
- `error: the draft mentions source ids outside a citation bracket` → the listed lines have a
  bare `S476` (often a table column). Change each to `[S476]`, or drop the text.
- `error: the draft is missing required sections` → add the listed sections in the report
  language (`Summary`/`Resumen`, `What to do`/`Qué hacer`, `Not verified`/`No verificado`)
  and re-run.
- `warning: report is ... words` → cut padding; a typical report is 4,000-7,000 words.
- `warning: line ... mentions a deadline date in the past` → do not present it as actionable;
  mark it `date_status: "past"`, and give the next cycle only if a source shows one.
- `warning: N cited sentence(s)/cell(s) contain numbers not found in the sources they cite`
  → for each listed line, starting with Summary and What to do: if it is your calculation,
  mark it with `≈` and cite its inputs; if it is a fact, find the source that contains it and
  cite that one, or fetch the cited page to check the current value; if no source has it,
  move it to Not verified. Re-run finalize once; a few remaining warnings are acceptable.
- `warning: only ...% of body paragraphs contain a citation` → add citations to the weak
  paragraphs (the report is still written; do not ignore this).
- `warning: N Summary/What-to-do sentence(s)/cell(s) cite only social/forum or low-authority
  sources` → the decision rests on weak support. Search for an official/primary source and
  fetch it, then cite that; if none exists, mark the claim `confidence: "low"` and say so in
  Disagreements or Not verified. Do not leave a price, requirement or deadline on a forum
  post, Q&A page, social media, WeChat, LinkedIn or blog alone.

## Step 7 — Post-actions

Re-read `<WORK>/plan.json` (or run `status`) and take its `post_actions` list. Run every item
with your available tools. Example: to update a task, put the Summary bullets and the
report path in the task description/comment.
If a tool is missing or fails, do not stop; note it for the final reply.

## Step 8 — Deliver

Final reply, nothing else after the MEDIA line:

```
Deep research on "<topic>" is ready.

Key findings:
- <finding with citation number, e.g. [1]>
- <finding>
- <finding>

Post-actions:
- <action>: done
- <action>: failed — <reason>

Report: ~/research/<SLUG>.md (attached)
MEDIA:/absolute/path/to/<SLUG>.md
```

For every item in `plan.json` `post_actions`, say `done` or `failed — <reason>`; omit the
Post-actions block when the list is empty. The gateway attaches the Markdown report as a document
on Telegram because of the `MEDIA:` line.
Use an absolute path: run `realpath ~/research/<SLUG>.md` and put that path on the `MEDIA:` line,
one line, no backticks and no other text after it. Always include the MEDIA line, even in a
terminal/CLI session: the gateway uses it when a file channel exists, and CLI users can open the
absolute path.

---

## Resuming

The pipeline is resumable at any point. Run:

```
python3 ${HERMES_SKILL_DIR}/scripts/research.py status --work <WORK>
```

Read the `[x]`/`[ ]` lines and `next:`, then continue at that step only. Never redo
completed work; re-running `search` is safe (ids are stable and already-run queries are
skipped as duplicates), and `finalize` is repeatable.

## Failure handling

- `DEEP_RESEARCH_DEEPSEEK_KEY is not set` → tell the user to run
  `hermes config set DEEP_RESEARCH_DEEPSEEK_KEY <their key>` and re-run. Do not fake results.
- `HTTP 401/403` → the key is invalid or exhausted. Report it; do not retry in a loop.
- `HTTP 402` → insufficient DeepSeek balance; tell the user to top up. Do not retry.
- `HTTP 429/5xx` → the script already retried 3 times with backoff. Re-run the same
  command once; if it still fails, tell the user which queries failed and continue with
  the sources already on disk.
- A query fails but others succeed → continue; mention the gap in `## Methodology` if it
  affected a subquestion.
- `plan.json is invalid` → fix exactly the printed lines (asks, covers, identify/q1);
  `--dry-run` until clean.
- `findings shape` warnings from `status` → add `verified` to every finding, `date_status`
  on date claims, `as_of`/`unit` on value claims, and real S-ids; rewrite that file.
- `fetch --grep` prints `no matches for: ...` and exits 1 → the value is not on that page.
  Do not cite it from that source; find the page that states it.
- Empty results → broaden wording, remove quotes/jargon, add a `site:` or country-TLD
  variant (do not add `after:` to official-document queries).
- `delegate_task` error or no results message → do the subquestions yourself sequentially
  (step 4 fallback), then continue.
- A post-action tool is missing or fails → do not stop; report that item as failed in the final
  reply.

## Budget defaults

- 4-8 subquestions, 4-6 queries each (≤128 chars), 20 results per query (search is billed per
  request, so 20 costs the same as 8), 6000 chars per source in `sources.json` plus the full
  page cached in `pages/`.
- Per subquestion researcher: max 6 follow-up search calls (≤3 queries each) and 3-8 fetched
  pages. Cached pages cost nothing, so fetch aggressively. `recent` is local and free: use it
  for every freshness check.
- Typical run: 24-48 searches in step 3 plus ≤48 follow-ups and ≤8 gap-round searches. At
  $0.00075 per request that is about $0.10 per report. The script prints run and cumulative
  `cost` — include it in `## Methodology` when it is above $0.10.
- Do not raise `--workers` above 10 (provider rate limits).
