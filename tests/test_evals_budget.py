"""Unit tests for the eval runner's search budget accounting and run ordering."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_runner():
    spec = importlib.util.spec_from_file_location("eval_run", ROOT / "evals" / "run.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_stats(run_dir: Path, search_usd: float) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "stats.json").write_text(
        json.dumps({"exa_cost_usd": search_usd, "status": "ok"}), encoding="utf-8"
    )


def test_batch_exa_spend_includes_finished_and_live_runs(tmp_path, monkeypatch):
    runner = load_runner()
    batch = "budget"
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    write_stats(tmp_path / batch / "runs" / "finished", 0.20)
    live = tmp_path / batch / "runs" / "live"
    work = live / "home" / "research" / ".work" / "slug"
    work.mkdir(parents=True)
    (work / "sources.json").write_text(json.dumps({"cost_dollars": 0.05}), encoding="utf-8")
    # A finished stats file wins over any leftover live sources.json.
    write_stats(tmp_path / batch / "runs" / "done-with-live", 0.07)
    (tmp_path / batch / "runs" / "done-with-live" / "home").mkdir()
    assert runner.batch_exa_spend(batch) == 0.32
    assert runner.run_dir_exa_cost(live) == 0.05


def test_ordered_runs_rep_then_topic_with_stable_config_order():
    runner = load_runner()
    topics = [{"id": "one"}, {"id": "two"}]
    runs = runner.ordered_runs(topics, ["A", "B", "C"], reps=2, batch="order")
    assert [(topic["id"], rep) for topic, _config, rep in runs] == [
        ("one", 1), ("one", 1), ("one", 1),
        ("two", 1), ("two", 1), ("two", 1),
        ("one", 2), ("one", 2), ("one", 2),
        ("two", 2), ("two", 2), ("two", 2),
    ]
    first = [(topic["id"], config) for topic, config, rep in runs if rep == 1]
    second = [(topic["id"], config) for topic, config, rep in runs if rep == 2]
    assert first == second
    assert sorted(config for _topic, config in first if _topic == "one") == ["A", "B", "C"]


def test_media_line_accepts_markdown(tmp_path):
    runner = load_runner()
    report = tmp_path / "report.md"
    report.write_text("# Report\n", encoding="utf-8")
    target, valid = runner.media_line(f"Done.\nMEDIA:{report}\n")
    assert valid is True
    assert target == str(report)
