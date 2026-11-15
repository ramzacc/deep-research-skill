#!/usr/bin/env python3
"""Build patched copies of the deep-research skill for the eval matrix.

Each config is a **skill variant**: the shipped skill directory is copied and
`SKILL.md` is edited with anchored, exact-string replacements. A missing or
ambiguous anchor is a hard error, so a silent no-op can never enter a run.

Configs
-------
A  4 subquestions exactly, 5 results per query, delegated researchers
B  4-8 subquestions as shipped, 8 results per query, delegated (current default)
C  8 subquestions exactly, 10 results per query, delegated
D  same breadth as B, but Step 4 replaces delegation with sequential self-research
E  budget: B breadth, 5 results/query, 2000 chars/source, 2 search + 2 fetch follow-ups

`num_results` is applied at run time through `DEEP_RESEARCH_NUM_RESULTS`; the
SKILL.md budget line is patched too so the model is never told a different
number from the one the script uses.

Usage
-----
    python3 evals/variants.py --check
    python3 evals/variants.py --build [--out evals/.run/skills]
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = ROOT / "skills" / "deep-research"
DEFAULT_OUT = ROOT / "evals" / ".run" / "skills"

SUBQ_RULE_ANCHOR = "- 4-8 subquestions, ids `q1`..`q8` in order, unique."
BUDGET_ANCHOR = (
    "- 4-8 subquestions, 3-5 queries each, 8 results per query, 6000 chars per source."
)
FOLLOWUP_STEP2_ANCHOR = "  STEP 2 — Optional, at most 4 search calls and 3 fetch calls total:"
FOLLOWUP_BUDGET_ANCHOR = (
    "- Per subquestion researcher: max 4 follow-up search calls (≤3 queries each) and 3 fetches."
)
STEP4_START = "## Step 4 — Depth (delegated researchers)"
STEP5_START = "## Step 5 — Synthesize"

SELF_RESEARCH_STEP4 = """\
## Step 4 — Depth (do it yourself, no subagents)

Do NOT call `delegate_task`. Do not spawn subagents. Research every subquestion YOURSELF,
sequentially, one at a time.

For each subquestion `<QID>` in plan.json, in order q1, q2, ...:

1. Read `<WORK>/evidence/<QID>.md` (a digest of candidate sources with stable S-ids).
2. Optional, at most 4 search calls and 3 fetch calls total for that subquestion:
   `python3 ${HERMES_SKILL_DIR}/scripts/research.py search --work <WORK> --subq <QID> --query "your query"`
   (repeat --query up to 3 times per call)
   `python3 ${HERMES_SKILL_DIR}/scripts/research.py fetch --work <WORK> --id S12 --max-chars 20000`
   Only fetch a page when its evidence excerpt is insufficient. Use the new S-ids they print.
3. Answer the subquestion using ONLY facts present in the evidence file or in pages you
   fetched. Cite with S-ids that appear in those outputs. If sources conflict, record the
   conflict instead of picking a side silently. If evidence is missing, say so in gaps.
4. Write valid JSON to `<WORK>/findings/<QID>.json` using the file write tool:
   {"id": "<QID>",
    "question": "<QUESTION>",
    "answer_summary": "3-6 sentences answering the subquestion, each claim followed by its S-id citation",
    "findings": [{"claim": "one verifiable fact with a number/date/name", "source_ids": ["S3"], "confidence": "high"}],
    "contradictions": ["source A says X [S4] while source B says Y [S9]"],
    "gaps": ["what could not be established and why"]}

   Rules for the JSON: 3-8 findings; source_ids are S-ids only, never URLs; confidence is
   exactly "high", "medium" or "low"; contradictions and gaps are arrays (may be empty).
5. Validate it and move on:
   `python3 -m json.tool <WORK>/findings/<QID>.json > /dev/null && echo ok`

Finish every subquestion before synthesizing. Then continue at Step 5.

"""

# One declarative table: adding config E is one entry.
CONFIGS: dict[str, dict] = {
    "A": {
        "description": "narrow + shallow: 4 subquestions, 5 results/query, delegation",
        "subquestions": {"exact": 4},
        "num_results": 5,
        "delegate": True,
    },
    "B": {
        "description": "shipped default: 4-8 subquestions (target 6), 8 results/query, delegation",
        "subquestions": {"min": 4, "max": 8, "target": 6},
        "num_results": 8,
        "delegate": True,
    },
    "C": {
        "description": "wide + deep: 8 subquestions, 10 results/query, delegation",
        "subquestions": {"exact": 8},
        "num_results": 10,
        "delegate": True,
    },
    "D": {
        "description": "shipped breadth (4-8, target 6), 8 results/query, NO delegation",
        "subquestions": {"min": 4, "max": 8, "target": 6},
        "num_results": 8,
        "delegate": False,
    },
    "E": {
        "description": "budget: 4-8 subquestions (target 6), 5 results/query, "
                       "2000 chars/source, 2 search + 2 fetch follow-ups",
        "subquestions": {"min": 4, "max": 8, "target": 6},
        "num_results": 5,
        "max_chars": 2000,
        "followups": {"searches": 2, "fetches": 2},
        "delegate": True,
    },
}


class VariantError(RuntimeError):
    """A SKILL.md anchor is missing or not unique."""


def replace_exact(text: str, anchor: str, replacement: str, label: str) -> str:
    count = text.count(anchor)
    if count != 1:
        raise VariantError(f"{label}: expected exactly 1 anchor, found {count}: {anchor!r}")
    return text.replace(anchor, replacement)


def replace_block(text: str, start: str, end: str, replacement: str, label: str) -> str:
    for anchor in (start, end):
        count = text.count(anchor)
        if count != 1:
            raise VariantError(f"{label}: expected exactly 1 block anchor, found {count}: {anchor!r}")
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[:begin] + replacement + text[finish:]


def patched_skill_md(config: str) -> str:
    spec = CONFIGS[config]
    text = (SOURCE_SKILL / "SKILL.md").read_text(encoding="utf-8")
    subq = spec["subquestions"]

    if "exact" in subq:
        exact = int(subq["exact"])
        text = replace_exact(
            text,
            SUBQ_RULE_ANCHOR,
            f"- Exactly {exact} subquestions, ids `q1`..`q{exact}` in order, unique.",
            f"{config} subquestion rule",
        )
        budget_subq = f"{exact} subquestions"
    else:
        budget_subq = f"{subq['min']}-{subq['max']} subquestions"

    text = replace_exact(
        text,
        BUDGET_ANCHOR,
        f"- {budget_subq}, 3-5 queries each, {spec['num_results']} results per query, "
        f"{spec.get('max_chars', 6000)} chars per source.",
        f"{config} budget line",
    )

    followups = spec.get("followups")
    if followups:
        searches = int(followups["searches"])
        fetches = int(followups["fetches"])
        text = replace_exact(
            text,
            FOLLOWUP_STEP2_ANCHOR,
            f"  STEP 2 — Optional, at most {searches} search calls and {fetches} fetch calls total:",
            f"{config} follow-up step",
        )
        text = replace_exact(
            text,
            FOLLOWUP_BUDGET_ANCHOR,
            f"- Per subquestion researcher: max {searches} follow-up search calls "
            f"(≤3 queries each) and {fetches} fetches.",
            f"{config} follow-up budget",
        )

    if not spec["delegate"]:
        text = replace_block(text, STEP4_START, STEP5_START, SELF_RESEARCH_STEP4, f"{config} step 4")
    return text


def build_variant(config: str, out_root: Path) -> Path:
    """Copy the shipped skill to <out_root>/<config>/deep-research with a patched SKILL.md."""
    if config not in CONFIGS:
        raise VariantError(f"unknown config {config!r}; choose from {', '.join(CONFIGS)}")
    dest = out_root / config / "deep-research"
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE_SKILL, dest)
    path = dest / "SKILL.md"
    path.write_text(patched_skill_md(config), encoding="utf-8")
    return dest


def build_all(out_root: Path) -> dict[str, Path]:
    return {config: build_variant(config, out_root) for config in CONFIGS}


def check() -> int:
    original = (SOURCE_SKILL / "SKILL.md").read_text(encoding="utf-8").splitlines(keepends=True)
    failed = False
    with tempfile.TemporaryDirectory(prefix="deep-research-variants-") as tmp:
        root = Path(tmp)
        for config in CONFIGS:
            try:
                dest = build_variant(config, root)
            except VariantError as exc:
                print(f"ERROR {config}: {exc}", file=sys.stderr)
                failed = True
                continue
            patched = (dest / "SKILL.md").read_text(encoding="utf-8").splitlines(keepends=True)
            diff = list(difflib.unified_diff(
                original, patched,
                fromfile=f"skills/deep-research/SKILL.md",
                tofile=f"variants/{config}/deep-research/SKILL.md",
                n=2,
            ))
            print(f"=== {config}: {CONFIGS[config]['description']} ===")
            if diff:
                sys.stdout.writelines(diff)
            else:
                print("(no SKILL.md changes)")
            print()
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build patched deep-research skill variants.")
    parser.add_argument("--build", action="store_true", help="write variants under --out")
    parser.add_argument("--check", action="store_true", help="print the SKILL.md diff per config")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output root (default evals/.run/skills)")
    parser.add_argument("--configs", default=",".join(CONFIGS), help="comma-separated config ids")
    args = parser.parse_args(argv)
    if not args.build and not args.check:
        parser.error("choose --build or --check")

    configs = [part.strip().upper() for part in args.configs.split(",") if part.strip()]
    if args.check:
        return check()
    for config in configs:
        try:
            dest = build_variant(config, Path(args.out))
        except VariantError as exc:
            print(f"ERROR {config}: {exc}", file=sys.stderr)
            return 1
        print(f"{config}: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
