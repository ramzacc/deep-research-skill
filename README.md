# deep-research

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) skill that runs ChatGPT/Gemini-style
deep research end to end: decompose the question, search broadly in parallel, delegate one
researcher per subquestion, merge the findings into a cited markdown report, and send it to you
on Telegram as a document.

The reasoning lives in a rigid checklist (`SKILL.md`); the deterministic parts live in one
stdlib-only Python script, so a small model can follow the pipeline without improvising.

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
| `DEEP_RESEARCH_EXA_KEY` | search + page fetching (the skill calls Exa's REST API directly) |
| `terminal` toolset enabled | runs `research.py` |
| Python 3.10+ | the script is stdlib-only |
| `delegate_task` (optional but recommended) | parallel per-subquestion researchers |

Set the key on the machine that runs Hermes:

```bash
hermes config set DEEP_RESEARCH_EXA_KEY <your-exa-key>
```

Use the same value as `EXA_API_KEY`. `EXA_API_KEY` alone is not enough: Hermes treats it as a
web-provider credential and strips it from the terminal sandbox (and refuses to pass it through
even when a skill declares it). `DEEP_RESEARCH_EXA_KEY` is a normal skill-declared variable, so
Hermes registers it for the terminal after the skill loads. A running gateway picks up the new
value on the next turn; start a new CLI chat after setting it.

Declared skill variables are passed to the terminal sandbox by Hermes; Hermes-managed provider
credentials are not.

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
~/research/.work/<slug>/          plan.json, sources.json, evidence/, findings/, draft.md
```

The work dir is the resume state: if the agent or the gateway restarts, `status` shows exactly
which step to continue from.

## What happens under the hood

1. `research.py init` creates the work dir.
2. The agent writes `plan.json`: 4-8 subquestions, 3-5 diverse queries each.
3. `research.py search` runs every query in parallel, dedupes URLs, assigns stable `S#` ids,
   and writes one evidence digest per subquestion. Searches always use Exa's `lite` type:
   about $0.004 per search, about $0.28 of search cost per report in the evals versus about
   $0.53 with `auto`.
4. One `delegate_task` call spawns one researcher per subquestion. Each may run a few
   follow-up searches/fetches and writes `<work>/findings/<qid>.json`. Results arrive
   asynchronously; the agent resumes from disk with `status`.
5. The agent synthesizes `draft.md` from `templates/report.md`.
6. `research.py finalize` validates every citation, renumbers them `[1]`, `[2]`, ... in order
   of first appearance, turns them into clickable links, and appends `## References`.
7. The agent runs every `post_actions` item from `plan.json` (for example, updating a
   task with the summary and report path).
8. The reply ends with `MEDIA:/abs/path/<slug>.md` and confirms each post-action.

Manual commands (same as the skill uses):

```bash
python3 skills/deep-research/scripts/research.py init --slug 2026-11-15-heat-pumps
python3 skills/deep-research/scripts/research.py search --work ~/research/.work/2026-11-15-heat-pumps --dry-run
python3 skills/deep-research/scripts/research.py search --work ~/research/.work/2026-11-15-heat-pumps
python3 skills/deep-research/scripts/research.py search --work DIR --subq q3 --query "follow-up"
python3 skills/deep-research/scripts/research.py fetch --work DIR --id S12
python3 skills/deep-research/scripts/research.py finalize --work DIR --draft DIR/draft.md --out ~/research/slug.md
python3 skills/deep-research/scripts/research.py status --work DIR
```

`search` and `fetch` exit non-zero with a clear message on failure; a single failed query is
reported and skipped so the run continues. Concurrent ad-hoc searches are safe: sources are
merged under an exclusive `flock` with atomic replacement, so parallel researchers never
duplicate ids or lose sources.

`--num-results` defaults to 8, or to `DEEP_RESEARCH_NUM_RESULTS` when the flag is omitted.
Each work dir also gets `log.jsonl`: one JSON line per `search_batch`, `search_adhoc`, `fetch`
and `finalize` operation (query, new/matched source ids, cost, ok/error), appended under the
same lock. `final.json` records the final `S#` → reference-number mapping and the paragraph
citation coverage.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `DEEP_RESEARCH_EXA_KEY is not set` | run `hermes config set DEEP_RESEARCH_EXA_KEY <key>`, then re-run |
| `HTTP 401/403` | invalid/expired Exa key |
| `HTTP 429` after retries | Exa rate limit; lower `--workers` (default 8) or retry later |
| `plan.json is invalid` | the error lists exact fields; validate with `search --dry-run` |
| `unknown source ids` from `finalize` | the draft cites an id that is not in `sources.json`; remove the claim or search for real evidence — never invent ids |
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

- `DEEP_RESEARCH_MOCK=/path/to/fixture.json` — routes fake Exa `/search` and Exa `/contents`
  responses (supports status codes, per-route response sequences, and artificial `delay`).
- `DEEP_RESEARCH_BACKOFF_BASE=0` — disables retry sleeps in tests.

Production knobs:

- `DEEP_RESEARCH_EXA_KEY` — the Exa key used by the scripts (falls back to `EXA_API_KEY`).
- `DEEP_RESEARCH_NUM_RESULTS` — results per search query (default 8; `--num-results` wins).
- `DEEP_RESEARCH_MAX_CHARS` — page-text characters per source (default 6000; `--max-chars` wins).

Layout:

```
skills/deep-research/SKILL.md              agent-facing checklist
skills/deep-research/scripts/research.py   init/search/fetch/finalize/status
skills/deep-research/templates/report.md   report skeleton
tests/                                     offline pytest suite (mock HTTP, subprocess concurrency)
```
