"""Offline tests for the deep-research scripts.

The HTTP layer is replaced by fixtures via the test-only env var
DEEP_RESEARCH_MOCK (see README.md). No network access happens here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import research

SCRIPT = Path(research.__file__).resolve()
SLUG = "2026-11-15-test-topic"
KEY = "test-deepseek-key"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def mock_fixture(tmp_path: Path, routes: list[dict], default: dict | None = None) -> Path:
    fixture = {
        "routes": routes,
        "default_deepseek": default or {"json": {"results": []}},
    }
    path = tmp_path / "mock.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return path


def search_ok(items: list[dict]) -> dict:
    return {"status": 200, "json": {"results": items}}


def fetch_ok(content: str, title: str = "Fetched page") -> dict:
    return {"status": 200, "json": {"title": title, "content": content}}


def hit(url: str, title: str = "Title", published: str = "2025-03-04",
        summary: str = "A summary.", content: str = "Body text.",
        authority: int | None = None, rerank: float | None = None) -> dict:
    item = {
        "url": url,
        "title": title,
        "date": published,
        "summary": summary,
        "content": content,
    }
    if authority is not None:
        item["authority"] = authority
    if rerank is not None:
        item["rerank_score"] = rerank
    return item


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
        "asks": ["Cover every subquestion"],
        "user_context": "",
        "identify": [],
        "subquestions": [
            {"id": qid, "question": f"Question {qid}?", "queries": queries, "covers": [0]}
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


def source(sid: str, url: str, title: str = "Source", published: str = "2025-01-02",
           authority: int | None = None, rerank: float | None = None,
           summary: str = "A summary.", text: str = "Body text.") -> dict:
    entry = {
        "id": sid,
        "url": url,
        "title": title,
        "published": published,
        "subquestions": ["q1"],
        "queries": ["q"],
        "summary": summary,
        "text": text,
    }
    if authority is not None:
        entry["authority"] = authority
    if rerank is not None:
        entry["rerank"] = rerank
    return entry


def load_sources(work: Path) -> dict:
    return json.loads((work / "sources.json").read_text(encoding="utf-8"))


def reset_query_index(work: Path) -> None:
    (work / research.QUERY_INDEX).unlink(missing_ok=True)


def use_sdk(monkeypatch, fixture: Path) -> None:
    monkeypatch.setenv(research.MOCK_ENV, str(fixture))
    monkeypatch.setenv(research.BACKOFF_ENV, "0")
    monkeypatch.setenv(research.DEEPSEEK_KEY_ENV, KEY)


def capture_http(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    original = research.http_post_json

    def spy(url, payload, timeout=45.0, headers=None):
        calls.append({"url": url, "payload": payload, "headers": headers})
        return original(url, payload, timeout, headers)

    monkeypatch.setattr(research, "http_post_json", spy)
    return calls


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
        {"contains": "query one", "responses": [search_ok([hit("https://example.com/report/")])]},
        {"contains": "query two",
         "responses": [search_ok([hit("https://WWW.Example.com/report/?utm_source=news#top")])]},
    ])
    use_sdk(monkeypatch, fixture)
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
        {"contains": "query one", "responses": [search_ok([hit("https://one.example/a")])]},
        {"contains": "query two", "responses": [search_ok([hit("https://two.example/b")])]},
        {"contains": "follow up",
         "responses": [search_ok([hit("https://one.example/a"), hit("https://three.example/c")])]},
    ])
    use_sdk(monkeypatch, fixture)
    work = make_work(tmp_path, plan_of(("q1", ["query one"]), ("q2", ["query two"])))

    assert run_cli(["search", "--work", str(work)], capsys)[0] == 0
    first = {s["url"]: s["id"] for s in load_sources(work)["sources"]}
    assert first == {"https://one.example/a": "S1", "https://two.example/b": "S2"}

    # re-run: same urls keep their ids, nothing duplicated
    assert run_cli(["search", "--work", str(work)], capsys)[0] == 0
    second = load_sources(work)["sources"]
    assert len(second) == 2
    assert {s["url"]: s["id"] for s in second} == first
    # queries were skipped as duplicates, so the total did not grow
    assert load_sources(work)["cost_dollars"] == pytest.approx(research.DEEPSEEK_USD_PER_REQUEST * 2)

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

def subprocess_env(fixture: Path) -> dict:
    env = dict(os.environ)
    env[research.MOCK_ENV] = str(fixture)
    env[research.BACKOFF_ENV] = "0"
    env[research.DEEPSEEK_KEY_ENV] = KEY
    return env


def test_concurrent_adhoc_processes(tmp_path, capsys):
    routes = []
    for index in range(6):
        routes.append({
            "contains": f"parallel query {index}",
            "responses": [{"delay": 0.25,
                           **search_ok([hit(f"https://site{index}.example/page")])}],
        })
    fixture = mock_fixture(tmp_path, routes)
    work = make_work(tmp_path)
    env = subprocess_env(fixture)

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
    assert registry["cost_dollars"] == pytest.approx(6 * research.DEEPSEEK_USD_PER_REQUEST)
    assert "[S1]" in (work / "evidence" / "q1.md").read_text()
    # every source got its full page cached
    pages = sorted(path.name for path in (work / "pages").glob("S*.md"))
    assert pages == [f"S{i}.md" for i in range(1, 7)]


def test_log_jsonl_under_concurrent_adhoc(tmp_path, capsys):
    routes = []
    for index in range(6):
        routes.append({
            "contains": f"logged query {index}",
            "responses": [{"delay": 0.25, **search_ok([hit(f"https://log{index}.example/page")])}],
        })
    fixture = mock_fixture(tmp_path, routes)
    work = make_work(tmp_path)
    env = subprocess_env(fixture)

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
    assert sum(float(record["cost"]) for record in records) == pytest.approx(
        6 * research.DEEPSEEK_USD_PER_REQUEST
    )


# --------------------------------------------------------------------------
# d. a permanently failing query does not abort the batch
# --------------------------------------------------------------------------

def test_failed_query_does_not_abort_batch(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"contains": "broken query", "responses": [{"status": 500, "body": "boom"}]},
        {"contains": "good query", "responses": [search_ok([hit("https://good.example/x")])]},
    ])
    use_sdk(monkeypatch, fixture)
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
        {"contains": "rate limited",
         "responses": [{"status": 429, "body": "slow down"},
                       search_ok([hit("https://retry.example/ok")])]},
    ])
    use_sdk(monkeypatch, fixture)
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
        ({"topic": "t", "slug": SLUG, "asks": ["a"], "user_context": "", "subquestions": [
            {"id": "bad", "question": "q?", "queries": ["x"], "covers": [0]}]},
         "must match ^q[0-9]+$"),
        ({"topic": "t", "slug": SLUG, "asks": ["a"], "user_context": "", "subquestions": [
            {"id": f"q{i}", "question": "q?", "queries": ["x"], "covers": [0]} for i in range(1, 10)]},
         "1-8 items"),
        ({"topic": "t", "slug": SLUG, "asks": ["a"], "user_context": "", "subquestions": [
            {"id": "q1", "question": "q?", "queries": [], "covers": [0]}]},
         "1-6 items"),
        ({"topic": "t", "slug": SLUG, "asks": ["a"], "user_context": "", "subquestions": [
            {"id": "q1", "question": "q?", "queries": ["x"], "covers": [0]},
            {"id": "q1", "question": "q?", "queries": ["y"], "covers": [0]}]},
         "duplicate subquestion id"),
        ({"topic": "t", "slug": SLUG, "asks": ["a"], "user_context": "", "subquestions": [
            {"id": "q1", "question": "srsly?", "queries": ["  "], "covers": [0]}]},
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


def test_plan_post_actions_validation(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))
    plan = plan_of(("q1", ["a"]))
    plan["post_actions"] = ["update my Todoist task with the results"]
    work = make_work(tmp_path, plan)
    code, _out, err = run_cli(["search", "--work", str(work), "--dry-run"], capsys)
    assert code == 0, err

    plan["post_actions"] = "update my task"
    (work / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    code, _out, err = run_cli(["search", "--work", str(work), "--dry-run"], capsys)
    assert code == 2
    assert "post_actions must be a list" in err

    plan["post_actions"] = [""]
    (work / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    code, _out, err = run_cli(["search", "--work", str(work), "--dry-run"], capsys)
    assert code == 2
    assert "post_actions[0]" in err


def test_plan_requires_ask_coverage(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))
    plan = plan_of(("q1", ["a"]), ("q2", ["b"]))
    plan["asks"] = ["first", "second"]
    plan["subquestions"][0]["covers"] = [0]
    plan["subquestions"][1]["covers"] = [0]
    work = make_work(tmp_path, plan)
    code, _out, err = run_cli(["search", "--work", str(work), "--dry-run"], capsys)
    assert code == 2
    assert "uncovered: asks[1]" in err


def test_plan_rejects_invalid_ask_index(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))
    plan = plan_of(("q1", ["a"]))
    plan["asks"] = ["first"]
    plan["subquestions"][0]["covers"] = [3]
    work = make_work(tmp_path, plan)
    code, _out, err = run_cli(["search", "--work", str(work), "--dry-run"], capsys)
    assert code == 2
    assert "not a valid ask index" in err


def test_identify_requires_q1_identification_question(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))
    plan = plan_of(("q1", ["a"]))
    plan["identify"] = ["the guide"]
    plan["subquestions"][0]["question"] = "What is the guide?"
    work = make_work(tmp_path, plan)
    code, _out, err = run_cli(["search", "--work", str(work), "--dry-run"], capsys)
    assert code == 2
    assert "identify is set" in err

    plan["subquestions"][0]["question"] = (
        "Identify exactly which the guide is meant: its canonical name, current URL and authorship history"
    )
    (work / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    code, _out, err = run_cli(["search", "--work", str(work), "--dry-run"], capsys)
    assert code == 0, err


def test_normalize_query_ignores_case_whitespace_and_punctuation():
    assert research.normalize_query("  What is X?  ") == "what is x"
    assert research.normalize_query("what   is   x") == "what is x"
    assert research.normalize_query("WHAT, IS: X!") == "what is x"


def test_duplicate_queries_cost_nothing(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"contains": "Same query?", "responses": [search_ok([hit("https://one.example/a")])]},
    ])
    use_sdk(monkeypatch, fixture)
    work = make_work(tmp_path, plan_of(("q1", ["Same query?"]), ("q2", ["same   query"])))
    code, out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    assert "skipped duplicate: same   query" in out
    assert "queries: 1 ok, 0 failed, 1 skipped duplicate(s)" in out
    registry = load_sources(work)
    assert registry["cost_dollars"] == pytest.approx(research.DEEPSEEK_USD_PER_REQUEST)
    events = [json.loads(line) for line in
              (work / research.LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1
    assert events[0]["op"] == "search_batch"


def test_adhoc_duplicate_after_batch_is_skipped(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"contains": "same query", "responses": [search_ok([hit("https://one.example/a")])]},
    ])
    use_sdk(monkeypatch, fixture)
    work = make_work(tmp_path, plan_of(("q1", ["same query"])))
    assert run_cli(["search", "--work", str(work)], capsys)[0] == 0
    code, out, err = run_cli(
        ["search", "--work", str(work), "--subq", "q1", "--query", "SAME query!"], capsys
    )
    assert code == 0, err
    assert "skipped duplicate: SAME query!" in out
    assert load_sources(work)["cost_dollars"] == pytest.approx(research.DEEPSEEK_USD_PER_REQUEST)


def test_max_chars_controls_stored_source_text(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(research.MAX_CHARS_ENV, raising=False)
    content = "A" * 8000
    fixture = mock_fixture(tmp_path, [
        {"contains": "a long page", "responses": [search_ok([hit("https://long.example/x", content=content)])]},
    ])
    use_sdk(monkeypatch, fixture)
    work = make_work(tmp_path, plan_of(("q1", ["a long page"])))

    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    assert research.default_max_chars() == 6000
    assert len(load_sources(work)["sources"][0]["text"]) == 6000

    monkeypatch.setenv(research.MAX_CHARS_ENV, "2000")
    assert research.default_max_chars() == 2000
    other = tmp_path / "work2"
    research.main(["init", "--slug", SLUG, "--work", str(other)])
    (other / "plan.json").write_text(
        json.dumps(plan_of(("q1", ["a long page"]))), encoding="utf-8"
    )
    code, _out, err = run_cli(["search", "--work", str(other)], capsys)
    assert code == 0, err
    assert len(load_sources(other)["sources"][0]["text"]) == 2000

    monkeypatch.setenv(research.MAX_CHARS_ENV, "not-a-number")
    assert research.default_max_chars() == 6000
    monkeypatch.setenv(research.MAX_CHARS_ENV, "0")
    assert research.default_max_chars() == 6000


def test_plan_dry_run_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))
    work = make_work(tmp_path, plan_of(("q1", ["a", "b"]), ("q2", ["c"])))
    code, out, err = run_cli(["search", "--work", str(work), "--dry-run"], capsys)
    assert code == 0, err
    assert "plan ok: 2 subquestions, 3 queries" in out


def test_num_results_env_default_and_cli_override(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(research.NUM_RESULTS_ENV, raising=False)
    fixture = mock_fixture(tmp_path, [])
    use_sdk(monkeypatch, fixture)
    seen: list[tuple[str, int]] = []

    def fake(query, num_results, _api_key, ori_question=""):
        seen.append((query, num_results))
        return [], 0.0

    monkeypatch.setattr(research, "deepseek_search", fake)
    work = make_work(tmp_path, plan_of(("q1", ["a", "b"])))

    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    assert sorted(seen) == [("a", 20), ("b", 20)]
    assert research.default_num_results() == 20

    seen.clear()
    reset_query_index(work)
    monkeypatch.setenv(research.NUM_RESULTS_ENV, "10")
    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    assert sorted(seen) == [("a", 10), ("b", 10)]
    assert research.default_num_results() == 10

    seen.clear()
    reset_query_index(work)
    code, _out, err = run_cli(["search", "--work", str(work), "--num-results", "3"], capsys)
    assert code == 0, err
    assert sorted(seen) == [("a", 3), ("b", 3)]

    monkeypatch.setenv(research.NUM_RESULTS_ENV, "not-a-number")
    assert research.default_num_results() == 20
    monkeypatch.setenv(research.NUM_RESULTS_ENV, "0")
    assert research.default_num_results() == 20


def test_missing_key_is_actionable(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(research.MOCK_ENV, raising=False)
    monkeypatch.delenv(research.DEEPSEEK_KEY_ENV, raising=False)
    work = make_work(tmp_path, plan_of(("q1", ["a"])))
    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 2
    assert "DEEP_RESEARCH_DEEPSEEK_KEY is not set" in err
    assert "hermes config set DEEP_RESEARCH_DEEPSEEK_KEY" in err


def test_invalid_slug_rejected(tmp_path, capsys):
    code, _out, err = run_cli(["init", "--slug", "Bad Slug!"], capsys)
    assert code == 2
    assert "invalid slug" in err


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
        "Third paragraph with adjacent citations [S2][S4].\n\n"
        "## Resumen\n\n## Qué hacer\n\n## No verificado\n",
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
        "# Title\n\nCited one [S1].\n\nUncited two.\n\nUncited three.\n\nUncited four.\n\n"
        "## Resumen\n\n## Qué hacer\n\n## No verificado\n",
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
        "# Title\n\nClaim [S1].\n\n## Resumen\n\n## Qué hacer\n\n## No verificado\n\n"
        "## References\n\n1. Stale entry. https://stale.example\n",
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


def test_finalize_spanish_references_heading(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text(
        "# Título\n\n## Resumen\n\nDato [S1].\n\n## Qué hacer\n\n## No verificado\n",
        encoding="utf-8",
    )
    out = tmp_path / "final.md"
    code, _stdout, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(out),
         "--lang", "es"], capsys,
    )
    assert code == 0, err
    text = out.read_text(encoding="utf-8")
    assert "## Referencias" in text
    assert "## References" not in text
    assert json.loads((work / "final.json").read_text())["language"] == "es"


def test_finalize_auto_detects_spanish(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text(
        "# Título\n\n## Resumen\n\nLa tasa de la que hablamos es de 5 % y el plazo es amplio [S1].\n\n"
        "## Qué hacer\n\n## No verificado\n",
        encoding="utf-8",
    )
    out = tmp_path / "final.md"
    code, _out, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(out)], capsys,
    )
    assert code == 0, err
    assert "## Referencias" in out.read_text(encoding="utf-8")
    assert json.loads((work / "final.json").read_text())["language"] == "es"


def test_report_template_has_required_sections():
    template = (SCRIPT.parent.parent / "templates" / "report.md").read_text(encoding="utf-8")
    for heading in ("## Summary", "## Disagreements & uncertainties", "## Not verified",
                    "## What to do", "## Assumptions", "## Methodology"):
        assert heading in template


def test_finalize_missing_required_sections_exits_1(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text("# Title\n\nClaim [S1].\n", encoding="utf-8")
    code, _out, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(tmp_path / "f.md")],
        capsys,
    )
    assert code == 1
    assert "missing required sections" in err
    assert "Resumen/Summary" in err
    assert "Qué hacer/What to do" in err
    assert "No verificado/Not verified" in err


def test_finalize_warns_on_long_report(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    body = " ".join(["word"] * 9005)
    draft.write_text(
        "# Title\n\n## Resumen\n\n" + body + " [S1].\n\n## Qué hacer\n\n## No verificado\n",
        encoding="utf-8",
    )
    code, _out, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(tmp_path / "f.md")],
        capsys,
    )
    assert code == 0
    assert "warning: report is" in err


def test_finalize_allows_medium_long_report(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    body = " ".join(["word"] * 5005)
    draft.write_text(
        "# Title\n\n## Resumen\n\n" + body + " [S1].\n\n## Qué hacer\n\n## No verificado\n",
        encoding="utf-8",
    )
    code, _out, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(tmp_path / "f.md")],
        capsys,
    )
    assert code == 0
    assert "warning: report is" not in err


def test_finalize_warns_on_past_deadline(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text(
        "# Title\n\n## Resumen\n\nThe application deadline is 2025-01-01 [S1].\n\n"
        "## Qué hacer\n\n## No verificado\n",
        encoding="utf-8",
    )
    code, _out, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(tmp_path / "f.md")],
        capsys,
    )
    assert code == 0
    assert "mentions a deadline date in the past" in err
    assert "2025-01-01" in err


def test_finalize_warns_on_weak_only_citations(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    write_registry(work, [
        source("S1", "https://www.facebook.com/group/post", "Facebook post",
               authority=0, summary="A user says the pack costs S/ 9.90."),
        source("S2", "https://gob.pe/tarifas", "Official tariffs", authority=4,
               summary="Official tariff: S/ 0.72 per kWh."),
    ])
    draft = work / "draft.md"
    draft.write_text(
        "# Title\n\n"
        "## Summary\n\n"
        "- The pack costs S/ 9.90 [S1].\n"
        "- The official tariff is S/ 0.72 per kWh [S2].\n\n"
        "## What to do\n\n"
        "1. Buy the pack at S/ 9.90 [S1, S2].\n\n"
        "## Not verified\n",
        encoding="utf-8",
    )
    out = tmp_path / "final.md"
    code, _stdout, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(out)], capsys
    )
    assert code == 0, err
    assert "cite only social/forum or low-authority sources" in err
    assert "S1:" in err
    assert "S1, S2" not in err
    assert out.exists()


def test_validate_finding_shape():
    good = {
        "id": "q1",
        "question": "Question?",
        "answer_summary": "Summary [S1].",
        "findings": [
            {"claim": "A fact.", "source_ids": ["S1"], "confidence": "high",
             "verified": True, "date_status": "open"}
        ],
        "contradictions": [],
        "gaps": [],
    }
    assert research.validate_finding(good) == []
    bad = json.loads(json.dumps(good))
    bad["findings"][0].pop("verified")
    bad["findings"][0]["date_status"] = "someday"
    bad["findings"][0]["source_ids"] = ["https://not-an-id"]
    errors = research.validate_finding(bad)
    assert any("verified" in error for error in errors)
    assert any("date_status" in error for error in errors)
    assert any("non-S-id" in error for error in errors)


def test_validate_finding_accepts_as_of_and_unit():
    finding = {
        "id": "q1",
        "question": "Question?",
        "answer_summary": "Summary [S1].",
        "findings": [
            {"claim": "Tariff is S/ 0.7202 per kWh.", "source_ids": ["S1"],
             "confidence": "high", "verified": True, "as_of": "2026-11",
             "unit": "S/ per kWh, incl. IGV"},
        ],
        "contradictions": [],
        "gaps": [],
    }
    assert research.validate_finding(finding) == []

    finding["findings"][0]["as_of"] = "2026-11-16"
    assert research.validate_finding(finding) == []
    finding["findings"][0]["as_of"] = "unknown"
    assert research.validate_finding(finding) == []

    finding["findings"][0]["as_of"] = "2026"
    errors = research.validate_finding(finding)
    assert any("as_of" in error for error in errors)

    finding["findings"][0]["as_of"] = "2026-11"
    finding["findings"][0]["unit"] = "  "
    errors = research.validate_finding(finding)
    assert any("unit" in error for error in errors)


# --------------------------------------------------------------------------
# h. status on partial state
# --------------------------------------------------------------------------

def test_status_prints_post_actions(tmp_path, capsys):
    plan = plan_of(("q1", ["a"]))
    plan["post_actions"] = ["update my Todoist task with the results", "write ~/done.txt"]
    work = make_work(tmp_path, plan)
    code, out, err = run_cli(["status", "--work", str(work)], capsys)
    assert code == 0, err
    assert "post_actions: 2" in out
    assert "  - update my Todoist task with the results" in out
    assert "  - write ~/done.txt" in out


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
# i. DeepSeek search: payload, ori_question, pages, tags, ranking, passages
# --------------------------------------------------------------------------

def test_search_payload_uses_count_and_ori_question(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"contains": "query one", "responses": [search_ok([hit("https://one.example/a")])]},
    ])
    use_sdk(monkeypatch, fixture)
    calls = capture_http(monkeypatch)
    work = make_work(tmp_path, plan_of(("q1", ["query one"])))

    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    search_calls = [call for call in calls if call["url"] == research.DEEPSEEK_SEARCH_URL]
    assert len(search_calls) == 1
    assert search_calls[0]["payload"] == {
        "query": "query one",
        "count": 20,
        "need_content": True,
        "ori_question": "Question q1?",
    }
    assert search_calls[0]["headers"] == {"Authorization": f"Bearer {KEY}"}


def test_ori_question_is_truncated_at_a_word_boundary(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"contains": "query one", "responses": [search_ok([])]},
    ])
    use_sdk(monkeypatch, fixture)
    calls = capture_http(monkeypatch)
    plan = plan_of(("q1", ["query one"]))
    plan["subquestions"][0]["question"] = " ".join(["abcdefghij"] * 20)
    work = make_work(tmp_path, plan)

    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    ori = calls[0]["payload"]["ori_question"]
    assert 0 < len(ori) <= research.DEEPSEEK_MAX_QUERY_CHARS
    assert all(len(word) == 10 for word in ori.split(" "))
    assert not ori.endswith(" ")
    assert "at most 128" in err


def test_fragment_stripping_dedupes_and_cost(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"contains": "site:gob.pe",
         "responses": [search_ok([
             hit("https://shared.example/a/#1", title="Shared", summary="Summary one.",
                 content="Full content one.", published="2025-03-04T10:00:00Z",
                 authority=3, rerank=2.5),
             hit("https://second.example/b#1#1", title="Second"),
         ])]},
        {"contains": "query two",
         "responses": [search_ok([
             hit("https://shared.example/a#cite_note-1#1", title="Shared again"),
             hit("https://third.example/c", title="Third"),
         ])]},
    ])
    use_sdk(monkeypatch, fixture)
    work = make_work(tmp_path, plan_of(("q1", ["site:gob.pe tariff"]), ("q2", ["query two"])))

    code, out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err

    registry = load_sources(work)
    by_url = {src["url"]: src for src in registry["sources"]}
    assert set(by_url) == {
        "https://shared.example/a/", "https://second.example/b", "https://third.example/c",
    }
    assert all("#" not in url for url in by_url)
    shared = by_url["https://shared.example/a/"]
    assert shared["id"] == "S1"
    assert shared["subquestions"] == ["q1", "q2"]
    assert shared["summary"] == "Summary one."
    assert shared["text"] == "Full content one."
    assert shared["published"] == "2025-03-04T10:00:00Z"
    assert shared["authority"] == 3
    assert shared["rerank"] == 2.5
    # two searches, both with results: flat per-request cost
    assert registry["cost_dollars"] == pytest.approx(2 * research.DEEPSEEK_USD_PER_REQUEST)
    assert "run cost: $0.0015" in out
    events = [json.loads(line) for line in
              (work / research.LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert {event["provider"] for event in events} == {"deepseek"}
    assert sum(event["cost"] for event in events) == pytest.approx(
        2 * research.DEEPSEEK_USD_PER_REQUEST
    )


def test_empty_results_cost_zero(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"contains": "empty query", "responses": [search_ok([])]},
    ])
    use_sdk(monkeypatch, fixture)
    work = make_work(tmp_path, plan_of(("q1", ["empty query"])))

    code, out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    registry = load_sources(work)
    assert registry["sources"] == []
    assert registry["cost_dollars"] == 0.0
    assert "run cost: $0.0000" in out
    events = [json.loads(line) for line in
              (work / research.LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert events[-1]["ok"] is True
    assert events[-1]["cost"] == 0.0


def test_full_page_cached_and_short_text_stored(tmp_path, monkeypatch, capsys):
    content = "Unique full page marker. " + ("x" * 20000)
    fixture = mock_fixture(tmp_path, [
        {"contains": "big page", "responses": [search_ok([
            hit("https://big.example/report", content=content),
        ])]},
    ])
    use_sdk(monkeypatch, fixture)
    work = make_work(tmp_path, plan_of(("q1", ["big page"])))

    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    page = (work / "pages" / "S1.md").read_text(encoding="utf-8")
    assert page == content[:research.PAGE_MAX_CHARS]
    assert len(page) > 20000
    assert len(load_sources(work)["sources"][0]["text"]) == research.TEXT_MAX_CHARS


def test_query_relevant_passages_replace_the_first_chars(tmp_path, monkeypatch, capsys):
    filler = "Relleno irrelevante sobre nada en particular. " * 450  # ~20k chars
    assert len(filler) > 19000
    content = filler + "La tarifa oficial es S/ 5.699 por mesa instalada." + filler
    assert content.index("5.699") > 14000
    fixture = mock_fixture(tmp_path, [
        {"contains": "tarifa mesa", "responses": [search_ok([
            hit("https://tarifas.example/doc", title="Tarifas", content=content),
        ])]},
    ])
    use_sdk(monkeypatch, fixture)
    work = make_work(tmp_path, plan_of(("q1", ["tarifa mesa"])))

    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 0, err
    evidence = (work / "evidence" / "q1.md").read_text(encoding="utf-8")
    assert "5.699" in evidence
    assert "S/ 5.699" in evidence
    assert len(evidence) < 4000
    # deterministic: rendering the same registry twice is identical
    registry = load_sources(work)
    sources = research.sources_for(registry, "q1")
    first = research.render_evidence("q1", "Question q1?", sources, work)
    second = research.render_evidence("q1", "Question q1?", sources, work)
    assert first == second


def test_select_passages_is_deterministic_and_capped():
    content = "\n\n".join(
        f"Paragraph {index}: " + ("irrelevant words " * 20) for index in range(60)
    ) + "\n\nTariff: S/ 0.7202 per kWh, effective 2026-01.\n\n" + "\n\n".join(
        f"Tail {index}: " + ("more text " * 20) for index in range(60)
    )
    terms = research.query_terms("tariff per kWh")
    first = research.select_passages(content, terms)
    assert research.select_passages(content, terms) == first
    assert "0.7202" in first
    assert len(first) <= research.PASSAGE_CHARS


def test_render_evidence_ranks_sources_and_tags_weak_ones(tmp_path):
    work = make_work(tmp_path)
    sources = []
    for index in range(1, 28):
        url = "https://site%d.example/p" % index
        sources.append(source(f"S{index}", url, f"Title {index}", authority=2, rerank=1.0))
    sources[0] = source("S1", "https://www.facebook.com/group/post", "Facebook post",
                        authority=1, rerank=0.1)
    sources[1] = source("S2", "https://social.example/x", "Social mirror",
                        authority=2, rerank=2.0)
    sources[1]["url"] = "https://m.facebook.com/p/123"
    sources[2] = source("S3", "https://official.example/doc", "Best source",
                        authority=4, rerank=2.9)
    sources[4] = source("S5", "https://auth5.example/doc", "Authority five",
                        authority=4, rerank=1.0)
    sources[5] = source("S6", "https://auth6.example/doc", "Authority six",
                        authority=3, rerank=1.0)
    sources[3] = source("S4", "https://last.example/doc", "Last", authority=0, rerank=0.0)

    evidence = research.render_evidence("q1", "Question q1?", sources, work)
    headers = re.findall(r"^### \[(S\d+)\]", evidence, re.MULTILINE)
    assert len(headers) == research.DIGEST_FULL_LIMIT == 25
    assert headers[0] == "S3"
    assert headers.index("S5") < headers.index("S6")
    assert "S4" not in headers and "S1" not in headers
    # authority + rerank + date are in every full header
    assert "· authority 4 · rerank 2.9" in evidence
    assert "(2025-01-02)" in evidence
    # tags rendered, never filtered
    facebook_header = next(line for line in evidence.splitlines()
                           if line.startswith("### [S2]"))
    assert "social/forum" in facebook_header
    one_liners = [line for line in evidence.splitlines() if line.startswith("[")]
    assert any(line.startswith("[S1]") and "low-authority" in line
               and "social/forum" in line for line in one_liners)
    assert any(line.startswith("[S4]") and "low-authority" in line for line in one_liners)
    assert "## Other candidates" in evidence


def test_digest_marks_undated_and_unknown_authority(tmp_path):
    work = make_work(tmp_path)
    evidence = research.render_evidence(
        "q1", "Question q1?",
        [source("S1", "https://one.example/a", "One", published="")], work,
    )
    assert "### [S1] One (undated)" in evidence
    assert "authority" not in evidence.split("\n")[0]


# --------------------------------------------------------------------------
# j. fetch: cache, --grep, --url
# --------------------------------------------------------------------------

def test_fetch_uses_cache_without_network(tmp_path, monkeypatch, capsys):
    work = make_work(tmp_path)
    write_registry(work, [source("S1", "https://one.example/a")])
    (work / "pages" / "S1.md").write_text("CACHED PAGE TEXT", encoding="utf-8")
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))
    monkeypatch.setenv(research.DEEPSEEK_KEY_ENV, KEY)

    def explode(*_args, **_kwargs):
        raise AssertionError("fetch must not hit the network when a page is cached")

    monkeypatch.setattr(research, "http_post_json", explode)
    code, out, err = run_cli(["fetch", "--work", str(work), "--id", "S1"], capsys)
    assert code == 0, err
    assert "CACHED PAGE TEXT" in out
    assert "cached" in out
    assert "using cached page" in err
    assert load_sources(work)["cost_dollars"] == 0.0


def test_fetch_grep_returns_windows_with_offsets_and_merges_overlaps(tmp_path, capsys):
    work = make_work(tmp_path)
    write_registry(work, [source("S1", "https://one.example/a")])
    text = (
        "x" * 1000
        + "Tarífa oficial: S/ 5.699 por mesa."
        + "y" * 100
        + "tarifa mensual aplicable."
        + "z" * 2000
        + "TARIFA final."
        + "w" * 2000
    )
    (work / "pages" / "S1.md").write_text(text, encoding="utf-8")
    code, out, err = run_cli(
        ["fetch", "--work", str(work), "--id", "S1", "--grep", "tarifa"], capsys
    )
    assert code == 0, err
    assert "--- match 1 @" in out
    # accent/case-insensitive, overlapping windows merged: 2 windows, not 3
    assert "2 window(s) for: tarifa" in out
    assert "S/ 5.699" in out
    assert "TARIFA final." in out
    first = re.search(r"--- match 1 @(\d+)\.\.(\d+) ---", out)
    assert first is not None
    assert int(first.group(1)) < 1000 < int(first.group(2))


def test_fetch_grep_no_match_exits_1(tmp_path, capsys):
    work = make_work(tmp_path)
    write_registry(work, [source("S1", "https://one.example/a")])
    (work / "pages" / "S1.md").write_text("nothing here", encoding="utf-8")
    code, out, err = run_cli(
        ["fetch", "--work", str(work), "--id", "S1", "--grep", "tarifa"], capsys
    )
    assert code == 1
    assert "no matches for: tarifa" in out


def test_fetch_stores_full_page_and_cost(tmp_path, monkeypatch, capsys):
    content = "FULL PAGE " * 2000  # 20k chars
    fixture = mock_fixture(tmp_path, [
        {"contains": "one.example", "responses": [fetch_ok(content)]},
    ])
    use_sdk(monkeypatch, fixture)
    calls = capture_http(monkeypatch)
    work = make_work(tmp_path)
    write_registry(work, [source("S1", "https://one.example/a")])

    code, out, err = run_cli(
        ["fetch", "--work", str(work), "--id", "S1", "--max-chars", "500"], capsys
    )
    assert code == 0, err
    assert [call["url"] for call in calls] == [research.DEEPSEEK_FETCH_URL]
    assert calls[0]["payload"] == {"url": "https://one.example/a"}
    assert calls[0]["headers"] == {"Authorization": f"Bearer {KEY}"}
    assert "FULL PAGE" in out
    # --max-chars limits the print, not the cache
    assert out.count("FULL PAGE") < 2000
    page = (work / "pages" / "S1.md").read_text(encoding="utf-8")
    assert page.startswith("FULL PAGE")
    assert len(page) == len(content)
    registry = load_sources(work)
    assert len(registry["sources"][0]["text"]) == research.TEXT_MAX_CHARS
    assert registry["cost_dollars"] == pytest.approx(research.DEEPSEEK_USD_PER_REQUEST)
    events = [json.loads(line) for line in
              (work / research.LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert events[-1]["op"] == "fetch"
    assert events[-1]["provider"] == "deepseek"
    assert events[-1]["cost"] == pytest.approx(research.DEEPSEEK_USD_PER_REQUEST)


def test_fetch_url_registers_new_source_and_evidence(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"contains": "new.example", "responses": [fetch_ok("Registered page body")]},
    ])
    use_sdk(monkeypatch, fixture)
    work = make_work(tmp_path, plan_of(("q1", ["a"])))
    write_registry(work, [source("S1", "https://one.example/a")])

    code, out, err = run_cli(
        ["fetch", "--work", str(work), "--url", "https://new.example/x", "--subq", "q1",
         "--grep", "Registered"], capsys,
    )
    assert code == 0, err
    registry = load_sources(work)
    assert [src["id"] for src in registry["sources"]] == ["S1", "S2"]
    added = registry["sources"][1]
    assert added["url"] == "https://new.example/x"
    assert added["subquestions"] == ["q1"]
    assert added["title"] == "Fetched page"
    assert (work / "pages" / "S2.md").read_text(encoding="utf-8") == "Registered page body"
    assert "[S2]" in (work / "evidence" / "q1.md").read_text(encoding="utf-8")
    assert "Registered page body" in out
    assert registry["cost_dollars"] == pytest.approx(research.DEEPSEEK_USD_PER_REQUEST)


def test_fetch_url_requires_subq(tmp_path, capsys):
    work = make_work(tmp_path)
    code, _out, err = run_cli(
        ["fetch", "--work", str(work), "--url", "https://new.example/x"], capsys
    )
    assert code == 2
    assert "--url requires --subq" in err


def test_fetch_unknown_id_exits_1(tmp_path, capsys):
    work = make_work(tmp_path)
    write_registry(work, [source("S1", "https://one.example/a")])
    code, _out, err = run_cli(["fetch", "--work", str(work), "--id", "S77"], capsys)
    assert code == 1
    assert "unknown source id S77" in err


def test_fetch_404_exits_nonzero(tmp_path):
    fixture = mock_fixture(tmp_path, [
        {"contains": "gone.example",
         "responses": [{"status": 404, "body": '{"error": {"message": "no content"}}'}]},
    ])
    work = make_work(tmp_path)
    write_registry(work, [source("S1", "https://gone.example/x")])
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "fetch", "--work", str(work), "--id", "S1"],
        capture_output=True, text=True, env=subprocess_env(fixture),
    )
    assert proc.returncode != 0
    assert "HTTP 404" in proc.stderr
    events = [json.loads(line) for line in
              (work / research.LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert events[-1]["ok"] is False
    assert "HTTP 404" in events[-1]["error"]


def test_402_reports_balance_without_retry(tmp_path, monkeypatch, capsys):
    fixture = mock_fixture(tmp_path, [
        {"contains": "billing query",
         "responses": [{"status": 402,
                        "body": '{"error": {"message": "Insufficient Balance"}}'}]},
    ])
    use_sdk(monkeypatch, fixture)
    work = make_work(tmp_path, plan_of(("q1", ["billing query"])))

    code, _out, err = run_cli(["search", "--work", str(work)], capsys)
    assert code == 1
    assert "insufficient balance" in err.lower()
    assert "retry" not in err
    assert load_sources(work)["sources"] == []
    events = [json.loads(line) for line in
              (work / research.LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert events[-1]["ok"] is False
    assert "insufficient balance" in events[-1]["error"].lower()


# --------------------------------------------------------------------------
# k. citation / deadline lint
# --------------------------------------------------------------------------

def test_citation_support_flags_numbers_missing_from_cited_source():
    by_id = {
        "S1": {"id": "S1", "title": "Tarifas", "text": "La tarifa es 0,7202 soles por kWh y la tarjeta cuesta S/ 5.699."},
        "S2": {"id": "S2", "title": "Otra", "text": "Nada relevante aquí, 2019."},
    }
    text = "\n".join([
        "## Resumen",
        "La tarjeta cuesta S/ 5699 en Lima [S1]. La tarifa es 72.02 ctm por kWh [S1].",
        "El paquete paga 46.236 al año [S2].",
        "| Opción | Precio |",
        "| Local | S/ 5,699 [S1] |",
        "| Import | US$ 1.982 [S1] |",
        "Costo mensual estimado de 1.250 soles [S1].",
        "## Supuestos",
        "| Horas | 2.920 [S2] |",
    ])
    warnings = research._citation_support_warnings(text, by_id)
    joined = "\n".join(warnings)
    assert "46236 not in S2" in joined
    assert "1982 not in S1" in joined
    assert "5699" not in joined and "7202" not in joined   # format variants still match
    assert "1250" not in joined                             # marked as an estimate
    assert "2920" not in joined                             # Supuestos section is skipped
    assert warnings[0].startswith("warning: 2 cited")


def test_citation_support_silent_when_supported():
    by_id = {"S1": {"id": "S1", "title": "t", "text": "Deadline 03/12/2026, fee R$ 241,22."}}
    text = "## Qué hacer\nPostula antes del 03/12/2026 y paga R$ 241,22 [S1]."
    assert research._citation_support_warnings(text, by_id) == []


def test_citation_support_reads_the_summary_too():
    by_id = {"S1": {"id": "S1", "title": "t", "text": "", "summary": "Fee: S/ 5.699"}}
    text = "## Resumen\nLa tarjeta cuesta S/ 5699 [S1]."
    assert research._citation_support_warnings(text, by_id) == []


def test_date_lint_ignores_lines_that_say_closed():
    past = "El plazo de inscripción cerró el 10/11/2020 [S1]."
    still_open = "Plazo de inscripción: 10/11/2020 [S1]."
    assert research._date_warnings(past) == []
    assert research._date_warnings(still_open)


# --------------------------------------------------------------------------
# l. recency-aware ranking
# --------------------------------------------------------------------------

TODAY = __import__("datetime").date(2026, 11, 16)


def test_recency_bonus_boundaries():
    assert research.recency_bonus({"published": "2026-09-01"}, TODAY) == 0.6   # 76 days
    assert research.recency_bonus({"published": "2026-01-01"}, TODAY) == 0.3   # 319 days
    assert research.recency_bonus({"published": "2025-11-16"}, TODAY) == 0.3   # 365 days
    assert research.recency_bonus({"published": "2025-11-15"}, TODAY) == 0.0   # 366 days
    assert research.recency_bonus({"published": "2024-11-15"}, TODAY) == -0.4  # > 2 years
    assert research.recency_bonus({"url": "https://x.pe/2026/11/a"}, TODAY) == 0.4
    assert research.recency_bonus({"title": "Tarifa 2025"}, TODAY) == 0.1
    assert research.recency_bonus({"title": "Tarifa 2024"}, TODAY) == 0.0
    assert research.recency_bonus({"title": "Tarifa 2023"}, TODAY) == -0.5
    assert research.recency_bonus({"title": "Sin fecha"}, TODAY) == 0.0


def test_year_hint_extraction():
    assert research.year_hint("https://x.pe/uploads/2021/05/tarifa.pdf", "", 2026) == 2021
    assert research.year_hint("https://x.pe/item/1232021456/report", "", 2026) is None
    assert research.year_hint("https://x.pe/a", "Tarifa 2019", 2026) == 2019
    assert research.year_hint("https://x.pe/2019/a", "Resumen 2021", 2026) == 2021
    assert research.year_hint("https://x.pe/2028/a", "", 2026) is None
    assert research.year_hint("https://x.pe/1989/a", "", 2026) is None
    assert research.year_hint("https://x.pe/a", "no year here", 2026) is None


def test_fresh_news_outranks_stale_high_rerank_pdf(tmp_path):
    work = make_work(tmp_path)
    stale = source("S1", "https://sunat.gob.pe/uploads/2021/tarifa.pdf",
                   "Tarifa electrica 2021", published="", authority=4, rerank=2.07)
    news = source("S2", "https://elcomercio.pe/economia/nueva-tarifa", "Nueva tarifa electrica",
                  published="2026-11-06", authority=2, rerank=1.35)
    evidence = research.render_evidence("q1", "Question q1?", [stale, news], work, TODAY)
    headers = re.findall(r"^### \[(S\d+)\]", evidence, re.MULTILINE)
    assert headers == ["S2", "S1"]
    assert "undated (year hint 2021)" in evidence
    assert "possibly-outdated" in evidence
    assert research.source_score(news, TODAY) > research.source_score(stale, TODAY)


def test_possibly_outdated_tag_rules():
    assert "possibly-outdated" in research.source_quality_tags(
        {"url": "https://x.pe/a", "published": "2021-05-01"}, TODAY)
    assert "possibly-outdated" in research.source_quality_tags(
        {"url": "https://x.pe/2021/tarifa.pdf"}, TODAY)
    assert "possibly-outdated" not in research.source_quality_tags(
        {"url": "https://x.pe/2026/11/tarifa", "published": "2026-11-02"}, TODAY)
    assert "possibly-outdated" not in research.source_quality_tags(
        {"url": "https://x.pe/sin-fecha", "title": "Tarifa"}, TODAY)


def test_search_accepts_today_and_rejects_bad_dates(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))
    work = make_work(tmp_path, plan_of(("q1", ["a"])))
    code, out, _err = run_cli(
        ["search", "--work", str(work), "--today", "2026-11-16", "--dry-run"], capsys
    )
    assert code == 0
    assert "plan ok: 1 subquestions, 1 queries" in out
    code, _out, err = run_cli(
        ["search", "--work", str(work), "--today", "16/11/2026", "--dry-run"], capsys
    )
    assert code == 2
    assert "--today must be YYYY-MM-DD" in err


# --------------------------------------------------------------------------
# m. recent --grep (no network)
# --------------------------------------------------------------------------

def test_recent_filters_all_terms_and_sorts_newest_first(tmp_path, monkeypatch, capsys):
    work = make_work(tmp_path)
    write_registry(work, [
        source("S1", "https://news.pe/nueva-tarifa", "Nueva tarifa electrica",
               published="2026-11-02", authority=4,
               summary="Nueva tarifa electrica residencial.", text="La tarifa sube a S/ 0.72."),
        source("S2", "https://old.pe/uploads/2021/tarifa.pdf", "Tarifa electrica 2021",
               published="", authority=3,
               summary="Tarifa electrica anterior.", text="Tarifa antigua: S/ 0.60."),
        source("S3", "https://sports.pe/nota", "Partido de futbol", published="2026-11-10",
               authority=2, summary="Deportes", text="Nada de tarifas aqui."),
        source("S4", "https://blog.pe/post", "Nota sin fecha", published="", authority=2,
               summary="Sin fecha", text="Comentario sobre la tarifa electrica."),
    ])
    monkeypatch.setenv(research.MOCK_ENV, str(mock_fixture(tmp_path, [])))

    def explode(*_args, **_kwargs):
        raise AssertionError("recent must never hit the network")

    monkeypatch.setattr(research, "http_post_json", explode)
    code, out, err = run_cli(
        ["recent", "--work", str(work), "--grep", "tarifa", "electrica"], capsys
    )
    assert code == 0, err
    lines = [line for line in out.splitlines() if line.startswith("[S")]
    assert [line.split("]")[0] + "]" for line in lines] == ["[S1]", "[S2]", "[S4]"]
    assert "2026-11-02 · authority 4 · Nueva tarifa electrica" in lines[0]
    assert "year hint 2021 · authority 3 · Tarifa electrica 2021" in lines[1]
    assert "undated · authority 2 · Nota sin fecha" in lines[2]
    assert "matched: 3 source(s) for: tarifa, electrica (newest first)" in out

    code, out, _err = run_cli(
        ["recent", "--work", str(work), "--grep", "tarifa", "electrica", "--limit", "1"],
        capsys,
    )
    assert code == 0
    assert out.count("[S") == 1
    assert "... 2 more (raise --limit)" in out

    code, out, _err = run_cli(["recent", "--work", str(work), "--grep", "hidrogeno"], capsys)
    assert code == 0
    assert "matched: 0 source(s)" in out
    assert "run search --subq" in out


# --------------------------------------------------------------------------
# n. finalize rejects bare source ids
# --------------------------------------------------------------------------

def test_finalize_rejects_bare_source_ids_in_a_table(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text(
        "# Title\n\n"
        "| Local | S1, S2 |\n"
        "| Import | S3 |\n\n"
        "Claim with a real citation [S1].\n\n"
        "## Resumen\n\n## Qué hacer\n\n## No verificado\n",
        encoding="utf-8",
    )
    out = tmp_path / "f.md"
    code, _stdout, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(out)], capsys
    )
    assert code == 1
    assert "outside a citation bracket" in err
    assert "line 3: S1, S2" in err
    assert "line 4: S3" in err
    assert "Cite each as [S#]" in err
    assert not out.exists()
    events = [json.loads(line) for line in
              (work / research.LOG_FILE).read_text(encoding="utf-8").splitlines()]
    assert events[-1]["ok"] is False
    assert "bare source ids" in events[-1]["error"]


def test_finalize_ignores_unknown_bare_tokens_and_bracketed_ids(tmp_path, capsys):
    work = make_finalize_work(tmp_path)
    draft = work / "draft.md"
    draft.write_text(
        "# Title\n\n"
        "Model S999 is not a source; the claim is supported [S1].\n\n"
        "## Resumen\n\n## Qué hacer\n\n## No verificado\n",
        encoding="utf-8",
    )
    out = tmp_path / "f.md"
    code, _stdout, err = run_cli(
        ["finalize", "--work", str(work), "--draft", str(draft), "--out", str(out)], capsys
    )
    assert code == 0, err
    assert out.exists()
