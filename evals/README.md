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

## Setup

```bash
git clone --depth 1 https://github.com/NousResearch/hermes-agent evals/.run/hermes-src
UV_PROJECT_ENVIRONMENT=$PWD/evals/.run/hermes-venv \
  uv sync --project evals/.run/hermes-src --no-dev
# evals/.env: EXA_API_KEY, OPENCODE_GO_API_KEY, OPENCODE_GO_BASE_URL
chmod 600 evals/.env
```

The first `run.py` also builds a shared PDF venv at `evals/.run/pdf-venv` and a
`python3` shim at `evals/.run/bin`. Hermes deliberately strips `EXA_API_KEY` from
the terminal sandbox (and refuses `terminal.env_passthrough` for it), so the shim
exports only that key, read from the mode-600 `evals/.env`, before exec'ing the
system Python. This mirrors a VPS where the key is exported; the key is never
written into the shim.

## Run

```bash
python3 evals/variants.py --check
python3 evals/run.py --batch smoke --topics smoke --configs B --reps 1
python3 evals/run.py --batch full --configs A,B,C,D --reps 2 --parallel 3
```

Each run gets its own `HOME` and `HERMES_HOME`, the variant skill, a 40-minute
timeout and a resumable `stats.json`. Completed runs are skipped; pass `--force`
to redo one.

## Package and score

```bash
python3 evals/package.py --batch full
# fill evals/results/full/blind/RANKING.md (1 = best, optional 1-5 scores)
python3 evals/score.py --batch full
```

`package.py` shuffles the finished reports per topic with a recorded seed and
writes `blind/<topic>/R01.md|pdf` plus `blind/key.json` and `blind/RANKING.md`.
`score.py` joins the ranking with the stats and writes `SUMMARY.md` (stats-only
when no ranking exists yet).

## Configs

| Config | Subquestions | Results/query | Delegation |
|---|---|---|---|
| A | exactly 4 | 5 | yes |
| B | 4-8 as shipped (target 6) | 8 | yes |
| C | exactly 8 | 10 | yes |
| D | 4-8 as shipped (target 6) | 8 | no, sequential self-research |

`evals/variants.py` copies `skills/deep-research/` and patches `SKILL.md` with
anchored exact-string replacements; a missing anchor fails the build loudly.
`DEEP_RESEARCH_NUM_RESULTS` drives results per query at runtime.
