#!/usr/bin/env python3
"""Package finished eval reports blind, so the user can rank them qualitatively.

For each topic, the `status: ok` runs are shuffled with a recorded seed and
copied to `evals/results/<batch>/blind/<topic>/R01.md|pdf`. The mapping lives in
`blind/key.json`; `blind/RANKING.md` is the form the user fills. No stats are
copied into the blind folder.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "results"


def load_runs(batch: str) -> list[dict]:
    runs_dir = RESULTS / batch / "runs"
    if not runs_dir.is_dir():
        raise SystemExit(f"no runs for batch {batch!r}: {runs_dir}")
    runs = []
    for run_dir in sorted(path for path in runs_dir.iterdir() if path.is_dir()):
        stats_path = run_dir / "stats.json"
        if not stats_path.is_file():
            continue
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        runs.append({"dir": run_dir, "stats": stats})
    return runs


def ranking_template(batch: str, labels_by_topic: dict[str, list[str]]) -> str:
    lines = [
        f"# Blind ranking — {batch}",
        "",
        "Rank each topic's reports. **1 = best.** Do not open `key.json` until you are done.",
        "Optional: score each report 1-5 for accuracy, depth, structure and citations.",
        "Keep the label column untouched; put your ranking and scores in the empty cells.",
        "When finished, run `python3 evals/score.py --batch " + batch + "`.",
        "",
    ]
    for topic, labels in labels_by_topic.items():
        lines += [
            f"## {topic}",
            "",
            "| label | rank | accuracy | depth | structure | citations | note |",
            "|---|---|---|---|---|---|---|",
        ]
        lines += [f"| {label} |  |  |  |  |  |  |" for label in labels]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Blind-package finished deep-research eval reports.")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--seed", default=None, help="shuffle seed (default: the batch name)")
    parser.add_argument("--force", action="store_true", help="replace an existing blind folder")
    args = parser.parse_args(argv)

    runs = load_runs(args.batch)
    usable = []
    for run in runs:
        stats = run["stats"]
        if stats.get("status") != "ok":
            continue
        report_md = stats.get("report_md")
        if not report_md:
            continue
        md_path = run["dir"] / report_md
        if not md_path.is_file():
            continue
        usable.append({**run, "md": md_path,
                       "pdf": (run["dir"] / stats["report_pdf"]) if stats.get("report_pdf") else None})
    if not usable:
        raise SystemExit(f"no status=ok runs with a report in batch {args.batch!r}")

    blind = RESULTS / args.batch / "blind"
    if blind.exists():
        if not args.force:
            raise SystemExit(f"{blind} already exists; pass --force to replace it (this drops any filled ranking)")
        shutil.rmtree(blind)
    blind.mkdir(parents=True)

    seed = args.seed or args.batch
    by_topic: dict[str, list[dict]] = {}
    for run in usable:
        by_topic.setdefault(str(run["stats"]["topic"]), []).append(run)

    key = {
        "batch": args.batch,
        "seed": seed,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "topics": {},
    }
    labels_by_topic: dict[str, list[str]] = {}
    for topic in sorted(by_topic):
        candidates = sorted(by_topic[topic], key=lambda run: run["stats"]["run_id"])
        random.Random(f"{seed}:{topic}").shuffle(candidates)
        topic_dir = blind / topic
        topic_dir.mkdir()
        labels: list[str] = []
        entries = {}
        for index, run in enumerate(candidates, 1):
            label = f"R{index:02d}"
            labels.append(label)
            shutil.copy2(run["md"], topic_dir / f"{label}.md")
            if run["pdf"] and run["pdf"].is_file():
                shutil.copy2(run["pdf"], topic_dir / f"{label}.pdf")
            entries[label] = {
                "run": run["stats"]["run_id"],
                "config": run["stats"].get("config"),
                "rep": run["stats"].get("rep"),
                "report_md": f"{topic}/{label}.md",
            }
        key["topics"][topic] = entries
        labels_by_topic[topic] = labels

    (blind / "key.json").write_text(json.dumps(key, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (blind / "RANKING.md").write_text(ranking_template(args.batch, labels_by_topic), encoding="utf-8")
    print(f"blind: {blind}")
    for topic, labels in labels_by_topic.items():
        print(f"  {topic}: {len(labels)} report(s) {labels[0]}..{labels[-1]}")
    print(f"next: fill {blind / 'RANKING.md'}, then: python3 evals/score.py --batch {args.batch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
