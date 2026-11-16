#!/usr/bin/env python3
"""Package finished eval reports blind, so the user can rank them qualitatively.

Incremental by design: a re-package keeps every existing label for a run,
appends labels for new runs, and only appends rows/sections to an existing
`RANKING.md`. A ranking the user has started filling is never overwritten.

For each topic, `status: ok` runs are shuffled with a recorded seed and copied
to `evals/results/<batch>/blind/<topic>/R01.md`. The mapping lives in
`blind/key.json`. No stats are copied into the blind folder.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "results"
LABEL_RE = re.compile(r"^R([0-9]+)$")
COLUMNS = ["label", "rank", "accuracy", "depth", "structure", "citations",
           "covered_all_asks", "note"]
GUIDE_TOPIC = "hrt-peru"


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


def columns_for(topic: str) -> list[str]:
    columns = list(COLUMNS)
    if topic == GUIDE_TOPIC:
        columns.insert(-1, "guide_ok")
    return columns


def ranking_header(batch: str) -> list[str]:
    return [
        f"# Blind ranking — {batch}",
        "",
        "Rank each topic's reports. **1 = best.** Do not open `key.json` until you are done.",
        "Optional: score each report 1-5 for accuracy, depth, structure and citations.",
        "- `covered_all_asks`: yes/partial/no — did it cover every ask in the prompt?",
        f"- `guide_ok` ({GUIDE_TOPIC} only): yes/partial/no — did it identify the right base guide?",
        "Keep the label column untouched; put your ranking and scores in the empty cells.",
        f"When finished, run `python3 evals/score.py --batch {batch}`.",
        "",
    ]


def ranking_block(topic: str, labels: list[str]) -> list[str]:
    columns = columns_for(topic)
    lines = [f"## {topic}", ""]
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "---|" * len(columns))
    lines += ["| " + " | ".join([label] + [""] * (len(columns) - 1)) + " |" for label in labels]
    lines.append("")
    return lines


def table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def find_topic_section(lines: list[str], topic: str) -> tuple[int, int] | None:
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"## {topic}":
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return start, end


def table_labels_and_insert(lines: list[str], start: int, end: int) -> tuple[set[str], int, int] | None:
    header = None
    for index in range(start, end):
        cells = table_cells(lines[index]) if lines[index].lstrip().startswith("|") else []
        if cells and cells[0].lower() == "label":
            header = index
            break
    if header is None:
        return None
    labels: set[str] = set()
    last = header
    width = len(table_cells(lines[header]))
    for index in range(header + 1, end):
        if not lines[index].lstrip().startswith("|"):
            if lines[index].strip():
                break
            continue
        cells = table_cells(lines[index])
        if cells and LABEL_RE.match(cells[0]):
            labels.add(cells[0])
        last = index
    return labels, last, width


def add_missing_rows(lines: list[str], topic: str, labels: list[str]) -> list[str]:
    section = find_topic_section(lines, topic)
    if section is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ranking_block(topic, labels)
        return lines
    start, end = section
    info = table_labels_and_insert(lines, start, end)
    if info is None:
        insert_at = end
        while insert_at > start and not lines[insert_at - 1].strip():
            insert_at -= 1
        if insert_at < len(lines) and lines[insert_at].strip():
            lines.insert(insert_at, "")
            insert_at += 1
        lines[insert_at:insert_at] = ranking_block(topic, labels)
        return lines
    existing, last, width = info
    missing = [label for label in labels if label not in existing]
    if not missing:
        return lines
    rows = []
    for label in missing:
        cells = [label] + [""] * (width - 1)
        rows.append("| " + " | ".join(cells) + " |")
    lines[last + 1:last + 1] = rows
    return lines


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Blind-package finished deep-research eval reports.")
    parser.add_argument("--batch", required=True)
    parser.add_argument("--seed", default=None, help="shuffle seed for new runs (default: the batch name)")
    parser.add_argument("--topics", default="", help="comma-separated topic ids (default: all finished)")
    parser.add_argument("--force", action="store_true",
                        help="refresh already-packaged report copies; never resets key.json or RANKING.md")
    args = parser.parse_args(argv)

    runs = load_runs(args.batch)
    usable = []
    for run in runs:
        stats = run["stats"]
        if stats.get("status") != "ok" or not stats.get("report_md"):
            continue
        md_path = run["dir"] / stats["report_md"]
        if not md_path.is_file():
            continue
        usable.append({**run, "md": md_path})
    if not usable:
        raise SystemExit(f"no status=ok runs with a report in batch {args.batch!r}")

    requested = [part.strip() for part in args.topics.split(",") if part.strip()]
    if requested:
        selected = set(requested)
        usable = [run for run in usable if str(run["stats"].get("topic")) in selected]
        if not usable:
            raise SystemExit(f"no finished runs for requested topic(s): {', '.join(requested)}")

    blind = RESULTS / args.batch / "blind"
    blind.mkdir(parents=True, exist_ok=True)
    key_path = blind / "key.json"
    key = read_json(key_path, {})
    if not isinstance(key, dict):
        key = {}
    key.setdefault("batch", args.batch)
    key.setdefault("seed", args.seed or args.batch)
    key.setdefault("topics", {})
    key["seed"] = args.seed or key.get("seed") or args.batch
    key["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    seed = key["seed"]
    by_topic: dict[str, list[dict]] = {}
    for run in usable:
        by_topic.setdefault(str(run["stats"]["topic"]), []).append(run)

    ranking_path = blind / "RANKING.md"
    lines = ranking_path.read_text(encoding="utf-8").splitlines() if ranking_path.is_file() else ranking_header(args.batch)

    for topic in sorted(by_topic):
        candidates = sorted(by_topic[topic], key=lambda run: run["stats"]["run_id"])
        existing = key["topics"].get(topic) or {}
        existing_by_run = {entry.get("run"): label for label, entry in existing.items() if entry.get("run")}
        labels = list(existing)
        max_label = max([int(LABEL_RE.match(label).group(1)) for label in labels if LABEL_RE.match(label)] or [0])
        new_runs = [run for run in candidates if run["stats"]["run_id"] not in existing_by_run]
        random.Random(f"{seed}:{topic}:new").shuffle(new_runs)

        topic_dir = blind / topic
        topic_dir.mkdir(exist_ok=True)
        existing_runs = [run for run in candidates if run["stats"]["run_id"] in existing_by_run]
        for run in existing_runs + new_runs:
            rid = run["stats"]["run_id"]
            label = existing_by_run.get(rid)
            if label is None:
                max_label += 1
                label = f"R{max_label:02d}"
                existing[label] = {
                    "run": rid,
                    "config": run["stats"].get("config"),
                    "rep": run["stats"].get("rep"),
                    "report_md": f"{topic}/{label}.md",
                }
                labels.append(label)
            shutil.copy2(run["md"], topic_dir / f"{label}.md")
        key["topics"][topic] = existing
        ordered_labels = sorted(existing, key=lambda label: int(LABEL_RE.match(label).group(1)) if LABEL_RE.match(label) else 0)
        lines = add_missing_rows(lines, topic, ordered_labels)

    key_path.write_text(json.dumps(key, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ranking_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"blind: {blind}")
    for topic in sorted(by_topic):
        labels = sorted(key["topics"][topic], key=lambda label: int(LABEL_RE.match(label).group(1)) if LABEL_RE.match(label) else 0)
        print(f"  {topic}: {len(labels)} report(s) {labels[0]}..{labels[-1]}")
    print(f"next: fill {ranking_path}, then: python3 evals/score.py --batch {args.batch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
