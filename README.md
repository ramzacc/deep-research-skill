# deep-research

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) skill that runs deep research end
to end as an exploration and reporting tool: decompose the question, search broadly in parallel,
delegate one researcher per subquestion, close the remaining gaps, and merge the findings into a
cited markdown report and send it to you on Telegram as a document.

It maps the landscape and reports the evidence: what sources say, how strong that evidence is,
and the trade-offs between the options. It does not make the decision for you: no recommendation,
no ranking of "best", no step-by-step action plan. If you ask "which should I choose", it lays
out what the evidence supports on each side and what would tip the balance.

The reasoning lives in a rigid checklist (`SKILL.md`); the deterministic parts live in one
stdlib-only Python script, so a small model can follow the pipeline without improvising. Search
and page fetching use DeepSeek's Search & Fetch API ($0.00075 per successful request, flat
regardless of result count).

## Install

```bash
hermes skills install ramzacc/deep-research-skill/skills/deep-research
```

Alternative, via a tap:

```bash
hermes skills tap add ramzacc/deep-research-skill
hermes skills install deep-research
```

Update later:

```bash
hermes skills update deep-research
```

## Requirements

| Requirement | Why |
|---|---|
| `DEEP_RESEARCH_DEEPSEEK_KEY` | search + page fetching (the skill calls DeepSeek's Search & Fetch API directly) |
| `terminal` toolset enabled | runs `research.py` |
| Python 3.10+ | the script is stdlib-only |
| `delegate_task` (optional but recommended) | parallel per-subquestion researchers |

Set the key on the machine that runs Hermes:

```bash
hermes config set DEEP_RESEARCH_DEEPSEEK_KEY <your-deepseek-key>
```

`DEEP_RESEARCH_DEEPSEEK_KEY` is a normal skill-declared variable, so Hermes registers it for the
terminal after the skill loads. A running gateway picks up the new value on the next turn; start
a new CLI chat after setting it.

Recommended delegation setting in `~/.hermes/config.yaml` so a full batch runs at once:

```yaml
delegation:
  max_concurrent_children: 8
```

## Usage

Talk to the agent normally:

```
Deep research: how are European utilities pricing industrial heat pumps in 2025?
```

```
Research thoroughly whether small modular reactors are commercially viable by 2030,
and write me a report with sources.
```

The agent will confirm the scope, run the pipeline, and reply with a summary plus a
`MEDIA:` line so the gateway attaches the markdown report on Telegram.

Outputs:

```
~/research/<slug>.md              final report, numbered linked citations + References
~/research/.work/<slug>/          plan.json, sources.json, pages/, evidence/, findings/, draft.md
```

The work dir is the resume state: if the agent or the gateway restarts, `status` shows exactly
which step to continue from.

## What happens under the hood

1. `research.py init` creates the work dir.
2. The agent writes `plan.json`: 4-8 subquestions, 4-6 diverse queries each (plain, synonym,
   official `site:`, local-market `site:.tld` when the topic is local, adversarial).
3. `research.py search` runs every query in parallel, dedupes URLs, assigns stable `S#` ids,
   caches each full page in `<work>/pages/<S#>.md`, and writes one evidence digest per
   subquestion: the best-ranked sources in full (with query-relevant passages) and the rest as
   one-liners.
4. One `delegate_task` call spawns one researcher per subquestion. Each may run follow-up
   searches/fetches, must verify every price/deadline/requirement on the page with
   `fetch --grep`, and writes `<work>/findings/<qid>.json`. Results arrive asynchronously; the
   agent resumes from disk with `status`.
5. The agent runs a gap round: gaps, contradictions and weak value-bearing findings get up to
   2 targeted searches/fetches each, appended to the findings; a staleness audit re-checks every
   value that will appear in the Summary or a comparison table.
6. The agent synthesizes `draft.md` from `templates/report.md`: Summary, 3-7 thematic sections,
   Debates and evidence strength, Implications, Open questions, Not verified, and Assumptions
   only when it computes something.
7. `research.py finalize` requires Summary, Implications and Not verified, validates every
   citation, renumbers them `[1]`, `[2]`, ... in order of first appearance, turns them into
   clickable links, appends `## References`, and warns about numbers missing from their cited
   page, dates that have passed, and Summary/Implications claims supported only by social/forum
   or low-authority sources.
8. The agent runs every `post_actions` item from `plan.json` (for example, updating a
   task with the summary and report path).
9. The reply ends with `MEDIA:/abs/path/<slug>.md` and confirms each post-action.

Manual commands (same as the skill uses):

```bash
python3 skills/deep-research/scripts/research.py init --slug 2026-11-15-heat-pumps
python3 skills/deep-research/scripts/research.py search --work ~/research/.work/2026-11-15-heat-pumps --dry-run
python3 skills/deep-research/scripts/research.py search --work ~/research/.work/2026-11-15-heat-pumps
python3 skills/deep-research/scripts/research.py search --work DIR --subq q3 --query "follow-up"
python3 skills/deep-research/scripts/research.py fetch --work DIR --id S12
python3 skills/deep-research/scripts/research.py fetch --work DIR --id S12 --grep "5.699" --grep "tarifa"
python3 skills/deep-research/scripts/research.py fetch --work DIR --url https://example.com/doc --subq q3
python3 skills/deep-research/scripts/research.py recent --work DIR --grep tarifa kWh
python3 skills/deep-research/scripts/research.py finalize --work DIR --draft DIR/draft.md --out ~/research/slug.md
python3 skills/deep-research/scripts/research.py status --work DIR
```

`search` and `fetch` exit non-zero with a clear message on failure; a single failed query is
reported and skipped so the run continues. Concurrent ad-hoc searches are safe: sources are
merged under an exclusive `flock` with atomic replacement, so parallel researchers never
duplicate ids or lose sources.

`recent --grep TERM [TERM...]` is local (no request): it lists registered sources whose title,
summary or cached page contain every term, newest first by date or year hint, so the agent can
find the newest source for a value before citing it.

Evidence digests rank sources by DeepSeek rerank plus a recency bonus (≤90 days +0.6, ≤365 days
+0.3, >2 years -0.4; undated pages get a year hint from their URL/title), and tag
`possibly-outdated` sources. `search --today YYYY-MM-DD` pins the ranking date (default: today).

`fetch` prints the cached page when one exists (no network, no cost) and otherwise fetches it
once, caching it in `pages/`. `--grep TERM` prints ±400-character windows around every match
with character offsets, so a number can be confirmed on a 50k-character page; it exits 1 with
`no matches` when the term is not there. `fetch --url` registers a URL that search missed as a
new `S#` and appends it to that subquestion's evidence.

`--num-results` defaults to 20, or to `DEEP_RESEARCH_NUM_RESULTS` when the flag is omitted.
Search is billed per request, not per result, so 20 results cost the same as 8.
Each work dir also gets `log.jsonl`: one JSON line per `search_batch`, `search_adhoc`, `fetch`
and `finalize` operation (query, new/matched source ids, cost, ok/error), appended under the
same lock. `final.json` records the final `S#` → reference-number mapping and the paragraph
citation coverage.

`DEEP_RESEARCH_MAX_CHARS` (default 6000) caps the short text kept per source in
`sources.json`; the full page always lives in `pages/<S#>.md`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `DEEP_RESEARCH_DEEPSEEK_KEY is not set` | run `hermes config set DEEP_RESEARCH_DEEPSEEK_KEY <key>`, then re-run |
| `HTTP 401/403` | invalid/expired DeepSeek key |
| `HTTP 402` | insufficient DeepSeek balance; top up the account |
| `HTTP 429/5xx` after retries | provider rate limit; lower `--workers` (default 8) or retry later |
| `plan.json is invalid` | the error lists exact fields; validate with `search --dry-run` |
| `unknown source ids` from `finalize` | the draft cites an id that is not in `sources.json`; remove the claim or search for real evidence — never invent ids |
| `no matches for: ...` from `fetch --grep` | the value is not on that page; do not cite it from that source |
| Research stops mid-run | ask "continue the research"; the agent runs `status` and resumes from the work dir |
| Researchers return no findings / their terminal commands are denied | subagents auto-deny approval prompts by default; set `hermes config set delegation.subagent_auto_approve true` and restart the gateway |
| Hub install misses a file | `SKILL.md` must literally reference every bundled path; both are referenced |

## Development

```bash
pip install pytest
python3 -m pytest -q tests
```

Tests are fully offline. The HTTP layer is replaced by a JSON fixture selected with the
**test-only** environment variables:

- `DEEP_RESEARCH_MOCK=/path/to/fixture.json` — routes fake DeepSeek `/tools/search` and
  `/tools/fetch` responses (`contains` matches the query or URL; supports status codes,
  per-route response sequences, and artificial `delay`), with an optional `default_deepseek`.
- `DEEP_RESEARCH_BACKOFF_BASE=0` — disables retry sleeps in tests.

Production knobs:

- `DEEP_RESEARCH_DEEPSEEK_KEY` — the DeepSeek key used by the scripts (required; missing exits 2).
- `DEEP_RESEARCH_NUM_RESULTS` — results per search query (default 20; `--num-results` wins).
- `DEEP_RESEARCH_MAX_CHARS` — short page-text characters per source in `sources.json`
  (default 6000; `--max-chars` wins).

History: this skill used Exa (and briefly Serper) for search and page text. Exa is no longer
used; DeepSeek Search & Fetch does both.

Layout:

```
skills/deep-research/SKILL.md              agent-facing checklist
skills/deep-research/scripts/research.py   init/search/fetch/finalize/status
skills/deep-research/templates/report.md   report skeleton
tests/                                     offline pytest suite (mock HTTP, subprocess concurrency)
evals/                                     end-to-end eval harness against a real Hermes Agent
```
