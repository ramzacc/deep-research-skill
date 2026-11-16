# Evals

Runs the real deep-research skill end to end inside an isolated Hermes Agent
(`evals/.run/hermes-venv`, locked dev install of NousResearch/hermes-agent main on
Python 3.14; PyPI 0.19.0 does not send `x-opencode-session`) against
`deepseek-v4.1-flash` on OpenCode Go, then packages the reports blind for a
qualitative ranking.

## Privacy

The repo is public. Real topics live in `evals/topics.local.json` (gitignored,
mode 600). `run.py` uses it when present and falls back to the tracked
`evals/topics.json` (generic smoke topic only). Topic prompts and report
contents live under `evals/results/` (gitignored) and are never copied into
tracked files. Secrets live in `evals/.env` (gitignored, mode 600) and are only
written to per-run `HERMES_HOME/.env` files (mode 600) under `evals/.run/`.
The Exa key is written there as `DEEP_RESEARCH_EXA_KEY` (the skill's declared
variable), not `EXA_API_KEY`: Hermes blocklists the latter from terminal
passthrough.

## Setup

```bash
git clone --depth 1 https://github.com/NousResearch/hermes-agent evals/.run/hermes-src
UV_PROJECT_ENVIRONMENT=$PWD/evals/.run/hermes-venv \
  uv sync --project evals/.run/hermes-src --no-dev
# evals/.env: EXA_API_KEY, OPENCODE_GO_API_KEY, OPENCODE_GO_BASE_URL
chmod 600 evals/.env
```

Each run writes `DEEP_RESEARCH_EXA_KEY` into its `HERMES_HOME/.env`; the skill
declares that variable, so Hermes registers it for the terminal sandbox through
the real `required_environment_variables` passthrough. No `python3` shim and no
`EXA_API_KEY` export.

## Run

```bash
python3 evals/variants.py --check
python3 evals/run.py --batch smoke --topics smoke --configs B --reps 1
python3 evals/run.py --batch full --configs A,B,C,D,E,F --reps 2 --parallel 3 --max-exa-usd 20
```

Each run gets its own `HOME` and `HERMES_HOME`, the variant skill, a 40-minute
timeout and a resumable `stats.json`. Completed runs are skipped; pass `--force`
to redo one. Runs are ordered rep 1 first, topic by topic, with a stable shuffled
config order per topic. `--max-exa-usd X` stops starting new runs once the
batch's finished plus in-flight Exa spend reaches X; in-flight runs finish, and
re-invoking with a higher cap resumes the remainder.

## Package and score

```bash
python3 evals/package.py --batch full --topics topic-a,topic-b
# fill evals/results/full/blind/RANKING.md (1 = best, optional 1-5 scores)
python3 evals/score.py --batch full
```

`package.py` is incremental: it packages whatever is finished, keeps existing
labels for runs already packaged, appends labels for new runs, and only appends
rows/sections to an existing `RANKING.md`. A ranking you have started filling is
never overwritten. `--topics` packages a subset so you can rank topic 1 while the
rest run. `score.py` joins the ranking with the stats and writes `SUMMARY.md`
(stats-only when no ranking exists yet); it also reports the per-config
`covered_all_asks` and `guide_ok` (hrt-peru only) yes-rates.

## Configs

| Config | Subquestions | Results/query | Chars/source | Follow-ups | Delegation |
|---|---|---|---|---|---|
| A | exactly 4 | 5 | 6000 | 4 search / 2-4 fetch | yes |
| B | 4-8 as shipped (target 6) | 8 | 6000 | 4 search / 2-4 fetch | yes |
| C | exactly 8 | 10 | 6000 | 4 search / 2-4 fetch | yes |
| D | 4-8 as shipped (target 6) | 8 | 6000 | 4 search / 2-4 fetch | no, sequential self-research |
| E | 4-8 as shipped (target 6) | 5 | 2000 | 2 search / 2 fetch | yes |
| F | 4-8 as shipped (target 6) | 10 | 6000 | 2 search / 2-6 fetch; exactly 3 queries/subq | yes |

`evals/variants.py` copies `skills/deep-research/` and patches `SKILL.md` with
anchored exact-string replacements; a missing anchor fails the build loudly.
`DEEP_RESEARCH_NUM_RESULTS` and `DEEP_RESEARCH_MAX_CHARS` drive results and page
text per query at runtime.
