#!/usr/bin/env python3
"""Join the blind ranking with the per-run stats and write SUMMARY.md.

Works with or without a filled RANKING.md: with no ranking it prints the stats
tables only, so it is useful immediately after a batch finishes.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "results"
SCORE_FIELDS = ("accuracy", "depth", "structure", "citations")
LABEL_RE = re.compile(r"^R[0-9]+$")


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
        runs.append(stats)
    if not runs:
        raise SystemExit(f"no stats.json files in batch {batch!r}")
    return runs


def parse_ranking(path: Path) -> dict[str, dict[str, dict]]:
    if not path.is_file():
        return {}
    rankings: dict[str, dict[str, dict]] = {}
    topic: str | None = None
    header: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            topic = heading.group(1).strip()
            rankings.setdefault(topic, {})
            header = None
            continue
        if topic is None or not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or not cells[0]:
            continue
        if cells[0].lower() == "label":
            header = [cell.lower() for cell in cells]
            continue
        if not LABEL_RE.match(cells[0]):
            continue
        if header is None:
            header = ["label", "rank", "accuracy", "depth", "structure", "citations", "note"]
        row = {header[index]: cells[index] for index in range(min(len(header), len(cells)))}
        if row.get("label", cells[0]).lower() != cells[0].lower():
            row["label"] = cells[0]

        def number(name: str) -> int | None:
            value = row.get(name, "")
            try:
                return int(value) if value else None
            except ValueError:
                return None

        def choice(name: str) -> str | None:
            value = str(row.get(name, "")).strip().lower()
            return value if value in ("yes", "partial", "no") else None

        rankings[topic][cells[0]] = {
            "rank": number("rank"),
            **{field: number(field) for field in SCORE_FIELDS},
            "covered_all_asks": choice("covered_all_asks"),
            "guide_ok": choice("guide_ok"),
            "note": row.get("note", ""),
        }
    return rankings


def mean(values: list[float | int | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return statistics.fmean(clean) if clean else None


def yes_rate(judged: list[dict], field: str) -> float | None:
    values = [item.get(field) for item in judged if item.get(field) in ("yes", "partial", "no")]
    if not values:
        return None
    return sum(1 for value in values if value == "yes") / len(values)


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def fmt(value: float | int | None, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return f"{value}{suffix}"
    return f"{value:.{digits}f}{suffix}"


def money(value: float | None) -> str:
    return "—" if value is None else f"${value:.3f}"


def group_by_config(runs: list[dict], judged: dict[str, dict]) -> dict[str, dict]:
    table: dict[str, dict] = {}
    for run in runs:
        config = str(run.get("config") or "?")
        entry = table.setdefault(config, {"runs": [], "judged": []})
        entry["runs"].append(run)
        judgment = judged.get(run["run_id"])
        if judgment:
            entry["judged"].append(judgment)
    return table


def config_rows(table: dict[str, dict]) -> list[str]:
    lines = [
        "| config | runs | ok | mean rank | accuracy | depth | structure | citations | "
        "covered_all_asks | guide_ok | sources found | cited | cited/found | coverage | search $ | LLM $ | total $ | wall s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for config in sorted(table):
        entry = table[config]
        runs = entry["runs"]
        judged = entry["judged"]
        ok = sum(1 for run in runs if run.get("status") == "ok")
        lines.append("| " + " | ".join([
            config,
            str(len(runs)),
            str(ok),
            fmt(mean([item.get("rank") for item in judged]), 2),
            *[fmt(mean([item.get(field) for item in judged]), 2) for field in SCORE_FIELDS],
            pct(yes_rate(judged, "covered_all_asks")),
            pct(yes_rate(judged, "guide_ok")),
            fmt(mean([len(run.get("sources_found") or []) for run in runs]), 1),
            fmt(mean([len(run.get("sources_cited") or []) for run in runs]), 1),
            fmt(mean([run.get("cited_found_ratio") for run in runs]) * 100 if mean([run.get("cited_found_ratio") for run in runs]) is not None else None, 0, "%"),
            fmt(mean([run.get("citation_coverage_pct") for run in runs]), 0, "%"),
            money(mean([run.get("exa_cost_usd") for run in runs])),
            money(mean([run.get("llm_cost_usd") for run in runs])),
            money(mean([run.get("total_cost_usd") for run in runs])),
            fmt(mean([run.get("wall_seconds") for run in runs]), 0),
        ]) + " |")
    return lines


def topic_rows(runs: list[dict], judged: dict[str, dict]) -> list[str]:
    grouped: dict[tuple[str, str], list[dict]] = {}
    for run in runs:
        grouped.setdefault((str(run.get("topic") or "?"), str(run.get("config") or "?")), []).append(run)
    lines = [
        "| topic | config | runs | mean rank | covered_all_asks | guide_ok | mean found | mean cited | mean coverage | mean $ | mean wall s |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (topic, config) in sorted(grouped):
        group = grouped[(topic, config)]
        judgments = [judged[run["run_id"]] for run in group if run["run_id"] in judged]
        ranks = [item.get("rank") for item in judgments]
        lines.append("| " + " | ".join([
            topic, config, str(len(group)), fmt(mean(ranks), 2),
            pct(yes_rate(judgments, "covered_all_asks")),
            pct(yes_rate(judgments, "guide_ok")),
            fmt(mean([len(run.get("sources_found") or []) for run in group]), 1),
            fmt(mean([len(run.get("sources_cited") or []) for run in group]), 1),
            fmt(mean([run.get("citation_coverage_pct") for run in group]), 0, "%"),
            money(mean([run.get("total_cost_usd") for run in group])),
            fmt(mean([run.get("wall_seconds") for run in group]), 0),
        ]) + " |")
    return lines


def status_rows(runs: list[dict], judged: dict[str, dict]) -> list[str]:
    counts: dict[str, dict[str, int]] = {}
    for run in runs:
        config = str(run.get("config") or "?")
        status = str(run.get("status") or "?")
        counts.setdefault(config, {})
        counts[config][status] = counts[config].get(status, 0) + 1
    statuses = sorted({status for per in counts.values() for status in per})
    lines = ["| config | " + " | ".join(statuses) + " |", "|---|" + "---|" * len(statuses)]
    for config in sorted(counts):
        lines.append("| " + " | ".join([config] + [str(counts[config].get(status, 0)) for status in statuses]) + " |")
    return lines


def recommendation(table: dict[str, dict], has_ranking: bool) -> str:
    scored = []
    for config, entry in table.items():
        judged = entry["judged"]
        runs = entry["runs"]
        if not judged:
            continue
        rank_mean = mean([item.get("rank") for item in judged])
        if rank_mean is None:
            continue
        quality = mean([run.get("cited_found_ratio") for run in runs]) or 0.0
        coverage = mean([run.get("citation_coverage_pct") for run in runs]) or 0.0
        cost = mean([run.get("total_cost_usd") for run in runs]) or 0.0
        scored.append((rank_mean, -quality, -coverage, cost, config))
    if not has_ranking or not scored:
        return ("No filled ranking yet: this summary is stats-only. Fill `blind/RANKING.md` and "
                "re-run `score.py` to rank configs by the blind review.")
    scored.sort()
    best = scored[0][4]
    return (f"Best blind rank: **config {best}** (mean rank {scored[0][0]:.2f}). "
            "Check the per-topic table: a config that wins on rank but loses on cost or coverage may "
            "still be the wrong default.")


def appendix(runs: list[dict], judged: dict[str, dict]) -> list[str]:
    lines = ["## Appendix: sources per run", ""]
    for run in sorted(runs, key=lambda item: (str(item.get("topic")), str(item.get("config")), item.get("rep") or 0)):
        found = run.get("sources_found") or []
        cited = run.get("sources_cited") or []
        judgment = judged.get(run["run_id"], {})
        rank = judgment.get("rank")
        covered = judgment.get("covered_all_asks")
        guide = judgment.get("guide_ok")
        extras = []
        if rank:
            extras.append(f"rank {rank}")
        if covered:
            extras.append(f"covered_all_asks {covered}")
        if guide:
            extras.append(f"guide_ok {guide}")
        lines += [
            f"### {run.get('topic')} · config {run.get('config')} · rep {run.get('rep')} · {run.get('status')}"
            + (" · " + " · ".join(extras) if extras else ""),
            "",
            f"<details><summary>found {len(found)} · cited {len(cited)}</summary>",
            "",
        ]
        if found:
            lines.append("**Found**")
            lines += [f"- {src.get('id')} · {src.get('title') or '(untitled)'} · {src.get('url')} · "
                      f"{','.join(src.get('subquestions') or [])}" for src in found]
            lines.append("")
        if cited:
            lines.append("**Cited**")
            lines += [f"- [{src.get('ref')}] ← {src.get('id')} · {src.get('title') or '(untitled)'} · {src.get('url')}"
                      for src in cited]
            lines.append("")
        lines += ["</details>", ""]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a deep-research eval batch.")
    parser.add_argument("--batch", required=True)
    args = parser.parse_args(argv)

    runs = load_runs(args.batch)
    blind = RESULTS / args.batch / "blind"
    key_path = blind / "key.json"
    key = json.loads(key_path.read_text(encoding="utf-8")) if key_path.is_file() else {}
    ranking_path = blind / "RANKING.md"
    rankings = parse_ranking(ranking_path) if ranking_path.is_file() else {}

    judged: dict[str, dict] = {}
    for topic, labels in (key.get("topics") or {}).items():
        for label, entry in labels.items():
            judgment = (rankings.get(topic) or {}).get(label)
            if judgment:
                judged[entry["run"]] = judgment
    has_ranking = any(item.get("rank") or item.get("covered_all_asks") for item in judged.values())

    table = group_by_config(runs, judged)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# Eval summary — {args.batch}",
        "",
        f"Generated {now}. Runs: {len(runs)} · ranked runs: {sum(1 for item in judged.values() if item.get('rank'))}. "
        f"Seed: {key.get('seed', '—')}.",
        "",
        "Lower mean rank is better. Money is the mean per run. Coverage is the finalize paragraph "
        "citation coverage.",
        "",
        "## By config",
        "",
        *config_rows(table),
        "",
        "## Per topic",
        "",
        *topic_rows(runs, judged),
        "",
        "## Status",
        "",
        *status_rows(runs, judged),
        "",
        "## Recommendation",
        "",
        recommendation(table, has_ranking),
        "",
        *appendix(runs, judged),
    ]
    out = RESULTS / args.batch / "SUMMARY.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"summary: {out}")
    if not has_ranking:
        print("note: no filled ranking found; SUMMARY.md is stats-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
