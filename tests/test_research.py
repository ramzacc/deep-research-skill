"""Offline tests for the deep-research scripts.

The HTTP layer is replaced by fixtures via the test-only env var
DEEP_RESEARCH_MOCK (see README.md). No network access happens here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import research

SCRIPT = Path(research.__file__).resolve()
SLUG = "2026-11-15-test-topic"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def mock_fixture(tmp_path: Path, routes: list[dict], default: dict | None = None) -> Path:
    fixture = {
        "routes": routes,
        "default": default or {"json": {"results": [], "costDollars": {"total": 0.0}}},
        "default_contents": {"json": {"results": []}},
    }
    path = tmp_path / "mock.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return path


def ok_response(items: list[dict], cost: float = 0.0) -> dict:
    return {"status": 200, "json": {"results": items, "costDollars": {"total": cost}}}


def hit(url: str, title: str = "Title", published: str = "2025-03-04",
        highlights: tuple[str, ...] = ("A highlight.",), text: str = "Body text.") -> dict:
    return {
        "url": url,
        "title": title,
        "publishedDate": published,
        "author": "Author",
        "highlights": list(highlights),
        "text": text,
    }


def run_cli(args: list[str], capsys) -> tuple[int, str, str]:
    try:
        code = research.main(args)
    except SystemExit as exc:  # die()/argparse
        code = exc.code if isinstance(exc.code, int) else 2
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def make_work(tmp_path: Path, plan: dict | None = None) -> Path:
    work = tmp_path / "work"
    research.main(["init", "--slug", SLUG, "--work", str(work)])
    if plan is not None:
        (work / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    return work


def plan_of(*subquestions: tuple[str, list[str]]) -> dict:
    return {
        "topic": "Test topic",
        "slug": SLUG,
        "subquestions": [
            {"id": qid, "question": f"Question {qid}?", "queries": queries}
            for qid, queries in subquestions
        ],
    }


def write_registry(work: Path, sources: list[dict], cost: float = 0.0) -> None:
    registry = {
        "slug": SLUG,
        "topic": "Test topic",
        "updated": "2026-11-15T00:00:00+00:00",
        "cost_dollars": cost,
        "sources": sources,
    }
    (work / "sources.json").write_text(json.dumps(registry), encoding="utf-8")


def source(sid: str, url: str, title: str = "Source", published: str = "2025-01-02") -> dict:
    return {
        "id": sid,
        "url": url,
        "title": title,
        "published": published,
        "author": "Author",
        "subquestions": ["q1"],
        "queries": ["q"],
        "highlights": ["A highlight."],
        "text": "Body text.",
    }


def load_sources(work: Path) -> dict:
    return json.loads((work / "sources.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# a. URL normalization / dedupe
# --------------------------------------------------------------------------

def test_normalize_url_variants():
    canonical = "https://example.com/report"
    assert research.normalize_url("https://example.com/report") == canonical
    assert research.normalize_url("https://example.com/report/") == canonical
    assert research.normalize_url("https://WWW.Example.COM/report/") == canonical
    assert research.normalize_url("https://example.com/report?utm_source=x&utm_campaign=y") == canonical
    assert research.normalize_url("https://example.com/report/#fragment") == canonical
    assert research.normalize_url("https://example.com/report?b=2&a=1") == "https://example.com/report?a=1&b=2"
    assert research.normalize_url("https://example.com:443/report") == canonical
    assert research.normalize_url("https://example.com/a?utm_medium=z&keep=1") == "https://example.com/a?keep=1"


def test_dedupe_keeps_one_source_and_both_subquestions(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"endpoint": "search", "contains": "query one",
         "responses": [ok_response([hit("https://example.com/report/")])]},
        {"endpoint": "search", "contains": "query two",
         "responses": [ok_response([hit("https://WWW.Example.com/report/?utm_source=news#top")])]},
    ])
    monkeypatch.setenv(research.MOCK_ENV, str(fixture))
    monkeypatch.setenv(research.BACKOFF_ENV, "0")
    work = make_work(tmp_path, plan_of(("q1", ["query one"]), ("q2", ["query two"])))
    code, out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    registry = load_sources(work)
    assert len(registry["sources"]) == 1
    src = registry["sources"][0]
    assert src["id"] == "S1"
    assert src["subquestions"] == ["q1", "q2"]
    assert sorted(src["queries"]) == ["query one", "query two"]
    assert "unique sources: 1" in out
    # evidence written per subquestion, referencing the same id
    assert "[S1]" in (work / "evidence" / "q1.md").read_text()
    assert "[S1]" in (work / "evidence" / "q2.md").read_text()


# --------------------------------------------------------------------------
# b. stable ids across re-runs and ad-hoc additions
# --------------------------------------------------------------------------

def test_ids_stable_across_reruns_and_adhoc(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"endpoint": "search", "contains": "query one",
         "responses": [ok_response([hit("https://one.example/a")])]},
        {"endpoint": "search", "contains": "query two",
         "responses": [ok_response([hit("https://two.example/b")])]},
        {"endpoint": "search", "contains": "follow up",
         "responses": [ok_response([hit("https://one.example/a"), hit("https://three.example/c")])]},
    ])
    monkeypatch.setenv(research.MOCK_ENV, str(fixture))
    monkeypatch.setenv(research.BACKOFF_ENV, "0")
    work = make_work(tmp_path, plan_of(("q1", ["query one"]), ("q2", ["query two"])))

    assert run_cli(["search", "--work", str(work)], capsys)[0] == 0
    first = {s["url"]: s["id"] for s in load_sources(work)["sources"]}
    assert first == {"https://one.example/a": "S1", "https://two.example/b": "S2"}

    # re-run: same urls keep their ids, nothing duplicated
    assert run_cli(["search", "--work", str(work)], capsys)[0] == 0
    second = load_sources(work)["sources"]
    assert len(second) == 2
    assert {s["url"]: s["id"] for s in second} == first

    # ad-hoc: new url gets the next free id, the matched url keeps S1
    code, out, err = run_cli(
        ["search", "--work", str(work), "--subq", "q1", "--query", "follow up"], capsys
    )
    assert code == 0, err
    third = {s["url"]: s["id"] for s in load_sources(work)["sources"]}
    assert third == {
        "https://one.example/a": "S1",
        "https://two.example/b": "S2",
        "https://three.example/c": "S3",
    }
    assert "[S1]" in out and "[S3]" in out
    evidence = (work / "evidence" / "q1.md").read_text()
    assert "https://three.example/c" in evidence
    # idempotent ad-hoc: no duplicate sections appended
    run_cli(["search", "--work", str(work), "--subq", "q1", "--query", "follow up"], capsys)
    assert evidence == (work / "evidence" / "q1.md").read_text()


# --------------------------------------------------------------------------
# c. concurrency: 6 processes, no lost sources, no duplicate ids
# --------------------------------------------------------------------------

def test_concurrent_adhoc_processes(tmp_path, monkeypatch, capsys):
    routes = []
    for index in range(6):
        routes.append({
            "endpoint": "search",
            "contains": f"parallel query {index}",
            "responses": [{"delay": 0.25, **ok_response([hit(f"https://site{index}.example/page")], cost=0.01)}],
        })
    fixture = mock_fixture(tmp_path, routes)
    work = make_work(tmp_path)
    env = dict(os.environ)
    env[research.MOCK_ENV] = str(fixture)
    env[research.BACKOFF_ENV] = "0"
    env.pop("EXA_API_KEY", None)

    procs = [
        subprocess.Popen(
            [sys.executable, str(SCRIPT), "search", "--work", str(work),
             "--subq", "q1", "--query", f"parallel query {index}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for index in range(6)
    ]
    results = [proc.communicate(timeout=120) for proc in procs]
    for proc, (out, err) in zip(procs, results):
        assert proc.returncode == 0, f"stdout={out}\nstderr={err}"

    registry = load_sources(work)
    assert len(registry["sources"]) == 6
    ids = [src["id"] for src in registry["sources"]]
    assert ids == ["S1", "S2", "S3", "S4", "S5", "S6"]
    assert len(set(ids)) == 6
    urls = {src["url"] for src in registry["sources"]}
    assert urls == {f"https://site{i}.example/page" for i in range(6)}
    assert abs(float(registry["cost_dollars"]) - 0.06) < 1e-9
    assert "[S1]" in (work / "evidence" / "q1.md").read_text()


def test_log_jsonl_under_concurrent_adhoc(tmp_path, monkeypatch, capsys):
    routes = []
    for index in range(6):
        routes.append({
            "endpoint": "search",
            "contains": f"logged query {index}",
            "responses": [{"delay": 0.25, **ok_response([hit(f"https://log{index}.example/page")], cost=0.01)}],
        })
    fixture = mock_fixture(tmp_path, routes)
    work = make_work(tmp_path)
    env = dict(os.environ)
    env[research.MOCK_ENV] = str(fixture)
    env[research.BACKOFF_ENV] = "0"
    env.pop("EXA_API_KEY", None)

    procs = [
        subprocess.Popen(
            [sys.executable, str(SCRIPT), "search", "--work", str(work),
             "--subq", "q1", "--query", f"logged query {index}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for index in range(6)
    ]
    results = [proc.communicate(timeout=120) for proc in procs]
    for proc, (out, err) in zip(procs, results):
        assert proc.returncode == 0, f"stdout={out}\nstderr={err}"

    lines = (work / research.LOG_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    records = [json.loads(line) for line in lines]
    assert {record["op"] for record in records} == {"search_adhoc"}
    assert {record["subq"] for record in records} == {"q1"}
    assert all(record["ok"] is True and record["error"] is None for record in records)
    assert sorted(sid for record in records for sid in record["new_ids"]) == [f"S{i}" for i in range(1, 7)]
    assert all(isinstance(record["ts"], str) and record["ts"] for record in records)
    assert sum(float(record["cost"]) for record in records) == pytest.approx(0.06)


# --------------------------------------------------------------------------
# d. a permanently failing query does not abort the batch
# --------------------------------------------------------------------------

def test_failed_query_does_not_abort_batch(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"endpoint": "search", "contains": "broken query",
         "responses": [{"status": 500, "body": "boom"}]},
        {"endpoint": "search", "contains": "good query",
         "responses": [ok_response([hit("https://good.example/x")], cost=0.02)]},
    ])
    monkeypatch.setenv(research.MOCK_ENV, str(fixture))
    monkeypatch.setenv(research.BACKOFF_ENV, "0")
    work = make_work(tmp_path, plan_of(("q1", ["broken query"]), ("q2", ["good query"])))
    code, out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0
    assert "queries: 1 ok, 1 failed" in out
    assert "query failed" in err
    registry = load_sources(work)
    assert len(registry["sources"]) == 1
    assert registry["sources"][0]["id"] == "S1"
    assert "No sources collected" in (work / "evidence" / "q1.md").read_text()


# --------------------------------------------------------------------------
# e. retry on 429, then success
# --------------------------------------------------------------------------

def test_retry_on_429_then_success(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"endpoint": "search", "contains": "rate limited",
         "responses": [{"status": 429, "body": "slow down"},
                       ok_response([hit("https://retry.example/ok")], cost=0.05)]},
    ])
    monkeypatch.setenv(research.MOCK_ENV, str(fixture))
    monkeypatch.setenv(research.BACKOFF_ENV, "0")
    work = make_work(tmp_path, plan_of(("q1", ["rate limited"])))
    code, out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    assert "retry 1/2" in err
    assert "queries: 1 ok, 0 failed" in out
    assert len(load_sources(work)["sources"]) == 1


# --------------------------------------------------------------------------
# f. plan validation
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ({"topic": "t", "slug": SLUG, "subquestions": [
            {"id": "bad", "question": "q?", "queries": ["x"]}]},
         "must match ^q[0-9]+$"),
        ({"topic": "t", "slug": SLUG, "subquestions": [
            {"id": f"q{i}", "question": "q?", "queries": ["x"]} for i in range(1, 10)]},
         "1-8 items"),
        ({"topic": "t", "slug": SLUG, "subquestions": [
            {"id": "q1", "question": "q?", "queries": []}]},
         "1-6 items"),
        ({"topic": "t", "slug": SLUG, "subquestions": [
            {"id": "q1", "question": "q?", "queries": ["x"]},
            {"id": "q1", "question": "q?", "queries": ["y"]}]},
         "duplicate subquestion id"),
        ({"topic": "t", "slug": SLUG, "subquestions": [
            {"id": "q1", "question": "srsly?", "queries": ["  "]}]},
         "non-empty string"),
    ],
)
def test_plan_validation_errors(tmp_path, monkeypatch, capsys, plan, expected):
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))
    work = make_work(tmp_path, plan)
    code, _out, err = run_cli(["search", "--work", str(work), "--dry-run"], capsys)
    assert code == 2
    assert "plan.json" in err
    assert expected in err


def test_plan_dry_run_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))
    work = make_work(tmp_path, plan_of(("q1", ["a", "b"]), ("q2", ["c"])))
    code, out, err = run_cli(["search", "--work", str(work), "--dry-run"], capsys)
    assert code == 0, err
    assert "plan ok: 2 subquestions, 3 queries" in out


def test_num_results_env_default_and_cli_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))
    monkeypatch.setenv(research.BACKOFF_ENV, "0")
    monkeypatch.delenv(research.NUM_RESULTS_ENV, raising=False)
    seen: list[tuple[str, int]] = []

    def fake(query, num_results, _api_key):
        seen.append((query, num_results))
        return [], 0.0

    monkeypatch.setattr(research, "_run_query", fake)
    work = make_work(tmp_path, plan_of(("q1", ["a", "b"])))

    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    assert sorted(seen) == [("a", 8), ("b", 8)]
    assert research.default_num_results() == 8

    seen.clear()
    monkeypatch.setenv(research.NUM_RESULTS_ENV, "10")
    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    assert sorted(seen) == [("a", 10), ("b", 10)]
    assert research.default_num_results() == 10

    seen.clear()
    code, _out, err = run_cli(["search", "--work", str(work), "--num-results", "3"], capsys)
    assert code == 0, err
    assert sorted(seen) == [("a", 3), ("b", 3)]

    monkeypatch.setenv(research.NUM_RESULTS_ENV, "not-a-number")
    assert research.default_num_results() == 8
    monkeypatch.setenv(research.NUM_RESULTS_ENV, "0")
    assert research.default_num_results() == 8


# --------------------------------------------------------------------------
# g. finalize
# --------------------------------------------------------------------------

def make_finalize_work(tmp_path: Path) -> Path:
    work = make_work(tmp_path, plan_of(("q1", ["a"]), ("q2", ["b"])))
    write_registry(work, [
        source("S1", "https://one.example/a", "First title", "2025-01-02"),
        source("S2", "https://two.example/b", "Second title", "2025-02-03"),
        source("S3", "https://three.example/c", "Third title", "2025-03-04"),
        source("S4", "https://four.example/d", "Fourth title", "2024-12-31"),
        source("S5", "https://five.example/e", "Unused title", "2024-11-30"),
    ])
    return work


def test_finalize_renumbers_links_and_references(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text(
        "# Report title\n\n"
        "First claim about the market [S3].\n\n"
        "Second paragraph with two sources [S1, S2].\n\n"
        "Third paragraph with adjacent citations [S2][S4].\n",
        encoding="utf-8",
    )
    out = tmp_path / "final.md"
    code, stdout, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(out)], capsys
    )
    assert code == 0, err
    text = out.read_text(encoding="utf-8")
    # first appearance order: S3 -> 1, S1 -> 2, S2 -> 3, S4 -> 4
    assert "[[1]](https://three.example/c)" in text
    assert "[[2]](https://one.example/a), [[3]](https://two.example/b)" in text
    assert "[[3]](https://two.example/b)[[4]](https://four.example/d)" in text
    final_info = json.loads((work / "final.json").read_text(encoding="utf-8"))
    assert final_info["citations"] == {"S3": 1, "S1": 2, "S2": 3, "S4": 4}
    assert final_info["coverage"]["cited_paragraphs"] == 3
    assert final_info["coverage"]["paragraphs"] >= 3
    events = [json.loads(line) for line in
              (work / research.LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert events[-1]["op"] == "finalize"
    assert events[-1]["ok"] is True
    assert events[-1]["citations"] == 4
    assert events[-1]["report"] == str(out.resolve())
    assert "[S3]" not in text and "[S1, S2]" not in text
    assert "## References" in text
    refs = text.split("## References", 1)[1]
    assert "1. Third title — three.example, 2025-03-04. https://three.example/c" in refs
    assert "2. First title — one.example, 2025-01-02. https://one.example/a" in refs
    assert "3. Second title — two.example, 2025-02-03. https://two.example/b" in refs
    assert "4. Fourth title — four.example, 2024-12-31. https://four.example/d" in refs
    assert "Unused title" not in refs
    assert f"report: {out}" in stdout
    # final.json records the output for status/resume
    assert json.loads((work / "final.json").read_text())["out"] == str(out)


def test_finalize_unknown_id_exits_1(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text(
        "# Title\n\nGood claim [S1].\n\nBad claim [S99].\n\nAnother bad one [S42, S1].\n",
        encoding="utf-8",
    )
    code, _out, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(tmp_path / "f.md")],
        capsys,
    )
    assert code == 1
    assert "S99 on line 5" in err
    assert "S42 on line 7" in err
    assert not (tmp_path / "f.md").exists()


def test_finalize_without_citations_exits_1(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text("# Title\n\nNo citations at all here.\n", encoding="utf-8")
    code, _out, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(tmp_path / "f.md")],
        capsys,
    )
    assert code == 1
    assert "contains no citations" in err


def test_finalize_warns_on_low_citation_coverage(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text(
        "# Title\n\nCited one [S1].\n\nUncited two.\n\nUncited three.\n\nUncited four.\n",
        encoding="utf-8",
    )
    out = tmp_path / "f.md"
    code, _stdout, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(out)], capsys
    )
    assert code == 0
    assert "warning: only 1/4 body paragraphs" in err
    assert out.exists()


def test_finalize_replaces_existing_references_section(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text(
        "# Title\n\nClaim [S1].\n\n## References\n\n1. Stale entry. https://stale.example\n",
        encoding="utf-8",
    )
    out = tmp_path / "f.md"
    code, _stdout, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(out)], capsys
    )
    assert code == 0, err
    text = out.read_text(encoding="utf-8")
    assert text.count("## References") == 1
    assert "stale.example" not in text


# --------------------------------------------------------------------------
# h. status on partial state
# --------------------------------------------------------------------------

def test_status_partial_state(tmp_path, capsys):
    work = make_work(tmp_path, plan_of(("q1", ["a"]), ("q2", ["b"])))
    write_registry(work, [source("S1", "https://one.example/a")])
    (work / "evidence" / "q1.md").write_text("# q1\n", encoding="utf-8")
    (work / "findings" / "q1.json").write_text("{}", encoding="utf-8")

    code, out, err = run_cli(["status", "--work", str(work)], capsys)
    assert code == 0, err
    assert "plan:      [x] 2 subquestions" in out
    assert "sources:   [x] 1 unique" in out
    assert "evidence:  [ ] 1/2 missing: q2" in out
    assert "findings:  [ ] 1/2 missing: q2" in out
    assert "draft:     [ ]" in out
    assert "next: produce findings for q2" in out


# --------------------------------------------------------------------------
# extras: fetch stores full text, missing key is a clear error
# --------------------------------------------------------------------------

def test_fetch_stores_full_text(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"endpoint": "contents", "contains": "one.example",
         "responses": [{"status": 200, "json": {"results": [{
             "url": "https://one.example/a", "title": "Fetched",
             "text": "FULL PAGE " * 50,
         }]}}]},
    ])
    monkeypatch.setenv(research.MOCK_ENV, str(fixture))
    work = make_work(tmp_path)
    write_registry(work, [source("S1", "https://one.example/a")])
    code, out, err = run_cli(["fetch", "--work", str(work), "--id", "S1", "--max-chars", "5000"], capsys)
    assert code == 0, err
    assert "FULL PAGE" in out
    assert load_sources(work)["sources"][0]["text"].startswith("FULL PAGE")
    events = [json.loads(line) for line in
              (work / research.LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert events[-1]["op"] == "fetch"
    assert events[-1]["fetched_id"] == "S1"
    assert events[-1]["ok"] is True

    code, _out, err = run_cli(["fetch", "--work", str(work), "--id", "S77"], capsys)
    assert code == 1
    assert "unknown source id S77" in err


def test_missing_exa_key_is_actionable(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(research.MOCK_ENV, raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    work = make_work(tmp_path, plan_of(("q1", ["a"])))
    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 2
    assert "EXA_API_KEY is not set" in err
    assert "~/.hermes/.env" in err


def test_invalid_slug_rejected(tmp_path, capsys):
    code, _out, err = run_cli(["init", "--slug", "Bad Slug!"], capsys)
    assert code == 2
    assert "invalid slug" in err
