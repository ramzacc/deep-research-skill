#!/usr/bin/env python3
"""Deterministic helper for the deep-research skill.

Subcommands
-----------
init      create the work dir for a slug and print the paths to use
search    batch (plan.json) or ad-hoc (-subq/-query) Exa searches, merged on disk
fetch     full text of one known source id, via /contents
finalize  validate + renumber citations, add References, write the final report
status    show which pipeline steps are done (used to resume)

All state lives in a work dir (default: ~/research/.work/<slug>/):

    plan.json                 written by the agent
    sources.json              source registry (stable S-ids, merged under flock)
    evidence/<qid>.md         per-subquestion digest for subagents
    findings/<qid>.json       written by subagents (or by the agent)
    draft.md                  written by the agent
    final.json                written by finalize (final md path and citation map)

Only the Python standard library is used. Python >= 3.10.

Exit codes: 0 ok, 1 operation failed (bad citations, network error), 2 bad
input (invalid slug/plan/arguments or missing API key).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fcntl
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"
TEXT_MAX_CHARS = 6000
HIGHLIGHTS_PER_SOURCE = 8
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
QID_RE = re.compile(r"^q[0-9]+$")
IDENTIFY_Q1_RE = re.compile(
    r"^Identify exactly which .+ is meant: its canonical name, current URL and "
    r"authorship history\.?$",
    re.DOTALL,
)
CITE_RE = re.compile(r"\[(S[0-9]+(?:\s*,\s*S[0-9]+)*)\]")
CITE_LINK_RE = re.compile(r"\[\[[0-9]+\]\]\(")
MAX_PARAGRAPH_CITATION_WARN = 0.60

# Test-only knobs (documented in README.md). Never set these in production.
MOCK_ENV = "DEEP_RESEARCH_MOCK"
BACKOFF_ENV = "DEEP_RESEARCH_BACKOFF_BASE"

# Results per query. DEEP_RESEARCH_NUM_RESULTS is the default; --num-results wins.
NUM_RESULTS_ENV = "DEEP_RESEARCH_NUM_RESULTS"
DEFAULT_NUM_RESULTS = 8
# Characters of page text per source. DEEP_RESEARCH_MAX_CHARS is the default; --max-chars wins.
MAX_CHARS_ENV = "DEEP_RESEARCH_MAX_CHARS"
DEFAULT_MAX_CHARS = TEXT_MAX_CHARS
LOG_FILE = "log.jsonl"

_MOCK_CACHE: dict[str, tuple[float, dict]] = {}
_MOCK_STATE: dict[str, dict] = {}
_MOCK_LOCK = threading.Lock()


class HttpError(Exception):
    """HTTP failure with an integer status (0 for transport/timeout errors)."""

    def __init__(self, status: int, body: str = "") -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = int(status)
        self.body = body


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def die(message: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    sys.stderr.write(message.rstrip("\n") + "\n")
    raise SystemExit(code)


def note(message: str) -> None:
    """Progress/warning line on stderr; stdout stays machine-greppable."""
    sys.stderr.write(message.rstrip("\n") + "\n")
    sys.stderr.flush()


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_text_atomic(path: Path, text: str) -> None:
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json_atomic(path: Path, data: dict) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path, what: str) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        die(f"error: {what} not found at {path}", 2)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(f"error: {what} at {path} is not valid JSON: {exc}", 2)
    if not isinstance(data, dict):
        die(f"error: {what} at {path} must contain a JSON object", 2)
    return data


def normalize_url(url: str) -> str:
    """Canonical form used for dedupe.

    Lowercase scheme+host, drop 'www.', drop default ports, drop fragments and
    utm_* query params, strip a trailing slash.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    query_pairs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    query = urllib.parse.urlencode(sorted(query_pairs))
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    return host[4:] if host.startswith("www.") else host


def slug_from_work(work: Path) -> str:
    return work.name


def work_root() -> Path:
    return Path.home() / "research" / ".work"


def research_dir() -> Path:
    return Path.home() / "research"


def default_work_dir(slug: str) -> Path:
    return work_root() / slug


def require_work(path_str: str) -> Path:
    work = Path(path_str).expanduser().resolve()
    if not work.is_dir():
        die(f"error: work dir does not exist: {work}\nRun: research.py init --slug <slug>", 2)
    return work


# --------------------------------------------------------------------------
# HTTP layer (mockable; test-only env var)
# --------------------------------------------------------------------------

def _load_mock_fixture() -> dict:
    path = os.environ.get(MOCK_ENV)
    if not path:
        return {}
    p = Path(path).expanduser()
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        die(f"error: {MOCK_ENV} points at a missing file: {p}", 2)
    cached = _MOCK_CACHE.get(str(p))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        fixture = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"error: mock fixture {p} is not valid JSON: {exc}", 2)
    if not isinstance(fixture, dict):
        die(f"error: mock fixture {p} must contain a JSON object", 2)
    _MOCK_CACHE[str(p)] = (mtime, fixture)
    return fixture


def _mock_route(payload: dict, endpoint: str) -> dict | None:
    fixture = _load_mock_fixture()
    routes = fixture.get("routes") or []
    if endpoint == "search":
        haystack = str(payload.get("query", ""))
    else:
        haystack = " ".join(str(u) for u in payload.get("urls", []))
    matches = []
    for idx, route in enumerate(routes):
        if not isinstance(route, dict):
            continue
        if (route.get("endpoint") or "search") != endpoint:
            continue
        needle = route.get("contains")
        if needle and needle not in haystack:
            continue
        matches.append((idx, route))
    if not matches:
        default = fixture.get("default_contents" if endpoint == "contents" else "default")
        return {"responses": [default]} if default else None
    # most specific match wins: longest matched needle, then declaration order
    matches.sort(key=lambda pair: (-len(pair[1].get("contains") or ""), pair[0]))
    return matches[0][1]


def _mock_response(endpoint: str, payload: dict) -> dict:
    route = _mock_route(payload, endpoint)
    if route is None:
        raise HttpError(599, f"no mock route for {endpoint} payload {json.dumps(payload)[:300]}")
    responses = route.get("responses") or [{"json": {}}]
    key = f"{os.environ.get(MOCK_ENV)}::{id(route)}::{endpoint}"
    with _MOCK_LOCK:
        state = _MOCK_STATE.setdefault(key, {"used": 0})
        index = min(state["used"], len(responses) - 1)
        state["used"] += 1
    entry = responses[index] if isinstance(responses[index], dict) else {"json": {}}
    delay = float(entry.get("delay") or 0)
    if delay > 0:
        time.sleep(delay)
    status = int(entry.get("status", 200))
    if status != 200:
        raise HttpError(status, str(entry.get("body", "")))
    return entry.get("json") or {}


def http_post_json(url: str, payload: dict, api_key: str, timeout: float = 45.0) -> dict:
    """POST JSON, return parsed JSON. Raises HttpError on any failure."""
    endpoint = "contents" if url.endswith("/contents") else "search"
    if os.environ.get(MOCK_ENV):
        return _mock_response(endpoint, payload)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "User-Agent": "hermes-deep-research/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HttpError(exc.code, detail) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise HttpError(0, f"network error: {reason}") from exc


def default_num_results() -> int:
    """Results per query: DEEP_RESEARCH_NUM_RESULTS, then 8. The CLI flag wins."""
    raw = os.environ.get(NUM_RESULTS_ENV, "").strip()
    if not raw:
        return DEFAULT_NUM_RESULTS
    try:
        value = int(raw)
    except ValueError:
        note(f"warning: {NUM_RESULTS_ENV}={raw!r} is not an integer; using {DEFAULT_NUM_RESULTS}")
        return DEFAULT_NUM_RESULTS
    if value < 1:
        note(f"warning: {NUM_RESULTS_ENV}={value} is below 1; using {DEFAULT_NUM_RESULTS}")
        return DEFAULT_NUM_RESULTS
    return value


def default_max_chars() -> int:
    """Characters of page text per source: DEEP_RESEARCH_MAX_CHARS, then 6000."""
    raw = os.environ.get(MAX_CHARS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_CHARS
    try:
        value = int(raw)
    except ValueError:
        note(f"warning: {MAX_CHARS_ENV}={raw!r} is not an integer; using {DEFAULT_MAX_CHARS}")
        return DEFAULT_MAX_CHARS
    if value < 1:
        note(f"warning: {MAX_CHARS_ENV}={value} is below 1; using {DEFAULT_MAX_CHARS}")
        return DEFAULT_MAX_CHARS
    return value


def backoff_base() -> float:
    try:
        return max(0.0, float(os.environ.get(BACKOFF_ENV, "1.0")))
    except ValueError:
        return 1.0


def with_retries(fn, attempts: int = 3, label: str = "request"):
    """Run fn(), retrying 429/5xx/transport errors with exponential backoff."""
    last: HttpError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except HttpError as exc:
            last = exc
            retryable = exc.status in (0, 429) or 500 <= exc.status <= 599
            if not retryable or attempt == attempts:
                raise
            delay = backoff_base() * (2 ** (attempt - 1))
            note(f"  retry {attempt}/{attempts - 1} for {label} after HTTP {exc.status}: sleeping {delay:.1f}s")
            time.sleep(delay)
    raise last or HttpError(0, "retry loop produced no result")


def exa_key_required() -> str:
    key = (
        os.environ.get("DEEP_RESEARCH_EXA_KEY", "").strip()
        or os.environ.get("EXA_API_KEY", "").strip()
    )
    if not key and not os.environ.get(MOCK_ENV):
        die(
            "error: DEEP_RESEARCH_EXA_KEY is not set.\n"
            "Set it to your Exa API key (the same value as EXA_API_KEY):\n"
            "  hermes config set DEEP_RESEARCH_EXA_KEY <your key>\n"
            "Then re-run the command. The skill uses Exa's REST API directly.",
            2,
        )
    return key


def exa_search(query: str, num_results: int, max_chars: int, api_key: str) -> tuple[list[dict], float]:
    payload = {
        "query": query,
        "type": "auto",
        "numResults": num_results,
        "contents": {"text": {"maxCharacters": max_chars}, "highlights": True},
    }
    data = http_post_json(EXA_SEARCH_URL, payload, api_key)
    results = data.get("results") or []
    if not isinstance(results, list):
        results = []
    cost = 0.0
    try:
        cost = float((data.get("costDollars") or {}).get("total") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return results, cost


def exa_contents(url: str, max_chars: int, api_key: str) -> tuple[dict, float]:
    payload = {"urls": [url], "text": {"maxCharacters": max_chars}}
    data = http_post_json(EXA_CONTENTS_URL, payload, api_key)
    results = data.get("results") or []
    if not results or not isinstance(results[0], dict):
        raise HttpError(502, f"/contents returned no result for {url}")
    try:
        cost = float((data.get("costDollars") or {}).get("total") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    return results[0], cost


# --------------------------------------------------------------------------
# sources registry
# --------------------------------------------------------------------------

def empty_registry(slug: str, topic: str = "") -> dict:
    return {
        "slug": slug,
        "topic": topic,
        "updated": utcnow(),
        "cost_dollars": 0.0,
        "sources": [],
    }


def load_registry(path: Path, slug: str) -> dict:
    if not path.exists():
        return empty_registry(slug)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"error: {path} is corrupted ({exc}); delete it and re-run search", 1)
    if not isinstance(data, dict):
        die(f"error: {path} must contain a JSON object", 1)
    data.setdefault("slug", slug)
    data.setdefault("topic", "")
    data.setdefault("cost_dollars", 0.0)
    if not isinstance(data.get("sources"), list):
        data["sources"] = []
    return data


@contextlib.contextmanager
def sources_lock(work: Path):
    lock_path = work / ".sources.lock"
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_log(work: Path, record: dict) -> None:
    """Append one JSON line to <work>/log.jsonl.

    Serialized with the source registry's lock so concurrent ad-hoc searches
    never interleave partial lines. Logging is best-effort: an unwritable log
    must not fail a research run.
    """
    line = json.dumps({"ts": utcnow(), **record}, ensure_ascii=False) + "\n"
    try:
        with sources_lock(work):
            with open(work / LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
    except OSError as exc:
        note(f"warning: could not append to {work / LOG_FILE}: {exc}")


def merge_records(registry: dict, records: list[dict], changes: list[dict] | None = None) -> tuple[list[str], list[str]]:
    """Merge search records into the registry in place.

    records: [{"qid": "q1", "query": "...", "results": [exa result, ...]}]
    Returns (new_ids, touched_ids). When *changes* is a list, one
    {"new_ids": [...], "matched_ids": [...]} entry per record is appended.
    """
    by_norm = {normalize_url(src.get("url", "")): src for src in registry["sources"]}
    next_num = 1
    for src in registry["sources"]:
        match = re.match(r"^S([0-9]+)$", str(src.get("id", "")))
        if match:
            next_num = max(next_num, int(match.group(1)) + 1)
    new_ids: list[str] = []
    touched: list[str] = []
    for record in records:
        record_new: list[str] = []
        record_touched: list[str] = []
        qid = record.get("qid") or ""
        query = record.get("query") or ""
        for item in record.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            norm = normalize_url(url)
            src = by_norm.get(norm)
            if src is None:
                src = {
                    "id": f"S{next_num}",
                    "url": url,
                    "title": str(item.get("title") or "").strip(),
                    "published": item.get("publishedDate") or "",
                    "author": item.get("author") or "",
                    "subquestions": [],
                    "queries": [],
                    "highlights": [],
                    "text": "",
                }
                next_num += 1
                registry["sources"].append(src)
                by_norm[norm] = src
                new_ids.append(src["id"])
                record_new.append(src["id"])
            if not src.get("title"):
                src["title"] = str(item.get("title") or "").strip()
            if not src.get("published"):
                src["published"] = item.get("publishedDate") or ""
            if not src.get("author"):
                src["author"] = item.get("author") or ""
            if qid and qid not in src["subquestions"]:
                src["subquestions"].append(qid)
            if query and query not in src["queries"]:
                src["queries"].append(query)
            highlights = item.get("highlights") or []
            if isinstance(highlights, list):
                for hl in highlights:
                    hl = str(hl).strip()
                    if hl and hl not in src["highlights"] and len(src["highlights"]) < HIGHLIGHTS_PER_SOURCE:
                        src["highlights"].append(hl)
            text = str(item.get("text") or "")
            if text and not src.get("text"):
                src["text"] = text[:TEXT_MAX_CHARS]
            touched.append(src["id"])
            record_touched.append(src["id"])
        if changes is not None:
            changes.append({"new_ids": record_new, "matched_ids": record_touched})
    # keep first-appearance order stable even if an id was seen again later
    registry["updated"] = utcnow()
    return new_ids, touched


def source_by_id(registry: dict, sid: str) -> dict | None:
    for src in registry["sources"]:
        if src.get("id") == sid:
            return src
    return None


def sources_for(registry: dict, qid: str) -> list[dict]:
    return [src for src in registry["sources"] if qid in (src.get("subquestions") or [])]


def format_date(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else text[:24]


def source_section(src: dict, max_text_chars: int) -> str:
    day = format_date(src.get("published"))
    when = f" ({day})" if day else ""
    lines = [f"### [{src['id']}] {src.get('title') or '(untitled)'}{when} — {src.get('url')}"]
    for highlight in src.get("highlights") or []:
        lines.append(f"- {highlight}")
    text = re.sub(r"\s+", " ", str(src.get("text") or "")).strip()
    if text:
        lines += ["", text[:max_text_chars]]
    lines.append("")
    return "\n".join(lines)


def render_evidence(qid: str, question: str, sources: list[dict], max_text_chars: int = 1500) -> str:
    lines = [f"# {qid} — {question}", ""]
    if not sources:
        lines += ["No sources collected for this subquestion yet.", ""]
        return "\n".join(lines)
    lines += [
        f"{len(sources)} candidate sources. Cite them as [S#]; ids are stable.",
        "",
    ]
    for src in sources:
        lines.append(source_section(src, max_text_chars))
    return "\n".join(lines)


def append_evidence(path: Path, qid: str, question: str, sources: list[dict], max_text_chars: int = 1500) -> list[dict]:
    """Append source sections not already present. Returns the sources appended."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    fresh = [src for src in sources if f"### [{src['id']}]" not in existing]
    if not fresh:
        return []
    chunk = "\n".join(source_section(src, max_text_chars) for src in fresh)
    if existing.strip():
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n## Added by follow-up searches\n\n" + chunk)
    else:
        header = (
            f"# {qid} — {question}\n\n"
            f"{len(fresh)} candidate sources. Cite them as [S#]; ids are stable.\n\n"
        )
        write_text_atomic(path, header + chunk)
    return fresh


# --------------------------------------------------------------------------
# plan handling
# --------------------------------------------------------------------------

def validate_plan(plan: dict) -> list[dict]:
    errors: list[str] = []
    if not isinstance(plan.get("topic"), str) or not plan.get("topic", "").strip():
        errors.append("plan.json: topic must be a non-empty string")
    if not isinstance(plan.get("slug"), str) or not plan.get("slug", "").strip():
        errors.append("plan.json: slug must be a non-empty string")

    asks = plan.get("asks")
    if not isinstance(asks, list) or not asks:
        errors.append("plan.json: asks must be a non-empty list of strings")
        asks = []
    else:
        for index, ask in enumerate(asks):
            if not isinstance(ask, str) or not ask.strip():
                errors.append(f"plan.json: asks[{index}] must be a non-empty string")

    if not isinstance(plan.get("user_context"), str):
        errors.append("plan.json: user_context must be a string (empty is allowed)")
    identify = plan.get("identify", [])
    if not isinstance(identify, list):
        errors.append("plan.json: identify must be a list of strings when present")
        identify = []
    else:
        for index, artifact in enumerate(identify):
            if not isinstance(artifact, str) or not artifact.strip():
                errors.append(f"plan.json: identify[{index}] must be a non-empty string")

    subquestions = plan.get("subquestions")
    if not isinstance(subquestions, list):
        errors.append("plan.json: subquestions must be a list")
        subquestions = []
    elif not 1 <= len(subquestions) <= 8:
        errors.append(f"plan.json: subquestions must have 1-8 items (found {len(subquestions)})")
    seen: set[str] = set()
    covered: set[int] = set()
    q1_question = ""
    for index, item in enumerate(subquestions):
        where = f"subquestions[{index}]"
        if not isinstance(item, dict):
            errors.append(f"plan.json: {where} must be an object")
            continue
        qid = item.get("id")
        if not isinstance(qid, str) or not QID_RE.match(qid):
            errors.append(f"plan.json: {where}.id must match ^q[0-9]+$ (got {qid!r})")
        elif qid in seen:
            errors.append(f"plan.json: duplicate subquestion id {qid!r}")
        else:
            seen.add(qid)
            if qid == "q1":
                q1_question = str(item.get("question") or "")
        question = item.get("question")
        if not isinstance(question, str) or not question.strip():
            errors.append(f"plan.json: {where}.question must be a non-empty string")
        queries = item.get("queries")
        if not isinstance(queries, list):
            errors.append(f"plan.json: {where}.queries must be a list of strings")
        elif not 1 <= len(queries) <= 6:
            errors.append(f"plan.json: {where}.queries must have 1-6 items (found {len(queries)})")
        else:
            for q_index, query in enumerate(queries):
                if not isinstance(query, str) or not query.strip():
                    errors.append(f"plan.json: {where}.queries[{q_index}] must be a non-empty string")
        covers = item.get("covers")
        if not isinstance(covers, list):
            errors.append(f"plan.json: {where}.covers must be a list of ask indices")
        else:
            for c_index, cover in enumerate(covers):
                if isinstance(cover, bool) or not isinstance(cover, int):
                    errors.append(f"plan.json: {where}.covers[{c_index}] must be an integer")
                elif not 0 <= cover < len(asks):
                    errors.append(
                        f"plan.json: {where}.covers[{c_index}]={cover} is not a valid ask index"
                    )
                else:
                    covered.add(cover)
    uncovered = [index for index in range(len(asks)) if index not in covered]
    if uncovered:
        errors.append(
            "plan.json: every ask must be covered by at least one subquestion; uncovered: "
            + ", ".join(f"asks[{index}]" for index in uncovered)
        )
    if identify and not IDENTIFY_Q1_RE.match(q1_question.strip()):
        errors.append(
            'plan.json: identify is set, so q1.question must be "Identify exactly which '
            '<artifact> is meant: its canonical name, current URL and authorship history"'
        )

    if errors:
        for line in errors:
            note(f"error: {line}")
        die("error: plan.json is invalid; fix the errors above and re-run search", 2)
    return subquestions


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    slug = args.slug.strip()
    if not SLUG_RE.match(slug):
        die(
            f"error: invalid slug {slug!r}: must match {SLUG_RE.pattern} "
            "(lowercase letters, digits, hyphens; 2-61 chars; start alphanumeric).\n"
            "Example: 2026-11-15-eu-ai-act",
            2,
        )
    work = Path(args.work).expanduser().resolve() if args.work else default_work_dir(slug)
    (work / "evidence").mkdir(parents=True, exist_ok=True)
    (work / "findings").mkdir(parents=True, exist_ok=True)
    plan_path = work / "plan.json"
    print(f"work dir: {work}")
    print(f"plan file: {plan_path}")
    print(f"evidence dir: {work / 'evidence'}")
    print(f"findings dir: {work / 'findings'}")
    if plan_path.exists():
        print("note: plan.json already exists; it will be reused (overwrite it to re-plan)")
    if not (work / "sources.json").exists():
        write_json_atomic(work / "sources.json", empty_registry(slug))
        print(f"sources file: {work / 'sources.json'}")
    return 0


def _run_query(query: str, num_results: int, max_chars: int, api_key: str) -> tuple[list[dict], float]:
    return exa_search(query, num_results, max_chars, api_key)


def _execute_queries(pairs: list[tuple[str, str]], args: argparse.Namespace, api_key: str) -> tuple[list[dict], int]:
    """Run (qid, query) pairs in parallel. Returns (records, failures)."""
    records: list[dict] = []
    failures = 0
    workers = max(1, min(args.workers, len(pairs) or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(with_retries, lambda q=query: _run_query(q, args.num_results, args.max_chars, api_key), 3, query): (qid, query)
            for qid, query in pairs
        }
        results: dict[tuple[str, str], dict] = {}
        for future in concurrent.futures.as_completed(future_map):
            qid, query = future_map[future]
            try:
                hits, cost = future.result()
            except HttpError as exc:
                failures += 1
                note(f"  query failed ({qid}): {query!r} -> {exc}")
                results[(qid, query)] = {
                    "qid": qid, "query": query, "results": [], "cost": 0.0,
                    "ok": False, "error": str(exc),
                }
                continue
            note(f"  {qid}: {len(hits)} results for {query!r}")
            results[(qid, query)] = {"qid": qid, "query": query, "results": hits, "cost": cost, "ok": True}
    # deterministic merge order: as listed in the plan / on the command line
    for pair in pairs:
        records.append(results[pair])
    return records, failures


def cmd_search(args: argparse.Namespace) -> int:
    work = require_work(args.work)
    slug = slug_from_work(work)
    if getattr(args, "num_results", None) is None:
        args.num_results = default_num_results()
    if getattr(args, "max_chars", None) is None:
        args.max_chars = default_max_chars()
    plan: dict = {}
    plan_path = work / "plan.json"
    adhoc = bool(args.subq)
    if args.query and not adhoc:
        die("error: --query requires --subq (ad-hoc mode for one subquestion)", 2)
    if adhoc:
        if not args.query:
            die("error: ad-hoc mode needs at least one --query", 2)
        if not QID_RE.match(args.subq):
            die(f"error: --subq must match ^q[0-9]+$ (got {args.subq!r})", 2)
        if plan_path.exists():
            plan = read_json(plan_path, "plan.json")
        question = ""
        for item in plan.get("subquestions") or []:
            if isinstance(item, dict) and item.get("id") == args.subq:
                question = str(item.get("question") or "")
                break
        question = question or "(ad-hoc follow-up)"
        pairs = [(args.subq, query) for query in args.query]
    else:
        if not plan_path.exists():
            die(
                f"error: plan.json not found at {plan_path}\n"
                "Write the plan first (see SKILL.md step 2), then re-run: "
                f"python3 <skill>/scripts/research.py search --work {work}",
                2,
            )
        plan = read_json(plan_path, "plan.json")
        subquestions = validate_plan(plan)
        pairs = []
        for item in subquestions:
            for query in item["queries"]:
                pairs.append((item["id"], query))
    if args.dry_run:
        counts = {}
        for qid, _query in pairs:
            counts[qid] = counts.get(qid, 0) + 1
        if adhoc:
            print(f"ad-hoc ok: {len(pairs)} queries for {args.subq}")
        else:
            print(f"plan ok: {len(plan['subquestions'])} subquestions, {len(pairs)} queries")
            for qid in sorted(counts, key=lambda q: int(q[1:])):
                print(f"  {qid}: {counts[qid]} queries")
        return 0
    api_key = exa_key_required()
    note(f"searching: {len(pairs)} queries with {args.workers} workers")
    records, failures = _execute_queries(pairs, args, api_key)

    with sources_lock(work):
        registry = load_registry(work / "sources.json", slug)
        if plan.get("topic") and not registry.get("topic"):
            registry["topic"] = plan["topic"]
        before = {src["id"] for src in registry["sources"]}
        changes: list[dict] = []
        new_ids, touched = merge_records(registry, records, changes)
        run_cost = sum(rec.get("cost") or 0.0 for rec in records)
        registry["cost_dollars"] = round(float(registry.get("cost_dollars") or 0.0) + run_cost, 6)
        write_json_atomic(work / "sources.json", registry)

    if adhoc:
        errors = [rec["error"] for rec in records if rec.get("error")]
        append_log(work, {
            "op": "search_adhoc",
            "subq": args.subq,
            "queries": [query for _qid, query in pairs],
            "new_ids": new_ids,
            "matched_ids": touched,
            "fetched_id": None,
            "cost": round(run_cost, 6),
            "ok": not errors,
            "error": "; ".join(errors) or None,
        })
    else:
        for record, change in zip(records, changes):
            append_log(work, {
                "op": "search_batch",
                "subq": record["qid"],
                "queries": [record["query"]],
                "new_ids": change["new_ids"],
                "matched_ids": change["matched_ids"],
                "fetched_id": None,
                "cost": round(float(record.get("cost") or 0.0), 6),
                "ok": bool(record.get("ok")),
                "error": record.get("error"),
            })

    if adhoc:
        qid = args.subq
        picked = [src for src in registry["sources"] if src["id"] in set(touched)]
        picked.sort(key=lambda s: int(s["id"][1:]))
        print(f"ad-hoc results for {qid}: {len(picked)} sources ({len(new_ids)} new)")
        for src in picked:
            day = format_date(src.get("published"))
            suffix = f" ({day})" if day else ""
            print(f"\n[{src['id']}] {src.get('title') or '(untitled)'}{suffix}")
            print(f"    {src.get('url')}")
            excerpt = ""
            if src.get("highlights"):
                excerpt = src["highlights"][0]
            elif src.get("text"):
                excerpt = re.sub(r"\s+", " ", src["text"])[:400]
            if excerpt:
                print(f"    {excerpt[:400]}")
        written: list[str] = []
        if picked:
            with sources_lock(work):
                registry = load_registry(work / "sources.json", slug)
                written = [src["id"] for src in append_evidence(
                    work / "evidence" / f"{qid}.md", qid, question, picked
                )]
            if written:
                print(f"\nevidence appended for {', '.join(written)}: {work / 'evidence' / (qid + '.md')}")
            else:
                print(f"\nevidence already contains these sources: {work / 'evidence' / (qid + '.md')}")
        print(f"cost: ${run_cost:.4f} (total ${registry['cost_dollars']:.4f})")
        print(f"sources total: {len(registry['sources'])}")
        return 0

    # batch mode: regenerate every evidence digest from the registry
    subquestions = plan["subquestions"]
    per_q: dict[str, int] = {}
    for item in subquestions:
        qid = item["id"]
        picked = sources_for(registry, qid)
        per_q[qid] = len(picked)
        write_text_atomic(
            work / "evidence" / f"{qid}.md",
            render_evidence(qid, item.get("question", ""), picked) + "\n",
        )
    ok = len(pairs) - failures
    print(f"queries: {ok} ok, {failures} failed")
    print(f"unique sources: {len(registry['sources'])} ({len(before)} -> {len(registry['sources'])})")
    print("sources per subquestion:")
    for qid in sorted(per_q, key=lambda q: int(q[1:])):
        print(f"  {qid}: {per_q[qid]}")
    print(f"run cost: ${run_cost:.4f} (total ${registry['cost_dollars']:.4f})")
    print(f"sources file: {work / 'sources.json'}")
    print(f"evidence dir: {work / 'evidence'}")
    return 1 if ok == 0 and pairs else 0


def cmd_fetch(args: argparse.Namespace) -> int:
    work = require_work(args.work)
    sid = args.id.strip()
    if not re.match(r"^S[0-9]+$", sid):
        die(f"error: --id must look like S12 (got {sid!r})", 2)
    registry = load_registry(work / "sources.json", slug_from_work(work))
    src = source_by_id(registry, sid)
    if src is None:
        die(f"error: unknown source id {sid} in {work / 'sources.json'}", 1)
    api_key = exa_key_required()
    max_chars = max(500, int(args.max_chars))
    note(f"fetching {sid}: {src['url']}")
    try:
        page, cost = with_retries(
            lambda: exa_contents(src["url"], max_chars, api_key), 3, f"contents {sid}"
        )
    except HttpError as exc:
        append_log(work, {
            "op": "fetch", "subq": None, "queries": [], "new_ids": [], "matched_ids": [],
            "fetched_id": sid, "cost": None, "ok": False, "error": str(exc),
        })
        raise
    text = str(page.get("text") or "")
    with sources_lock(work):
        registry = load_registry(work / "sources.json", slug_from_work(work))
        stored = source_by_id(registry, sid)
        if stored is not None and text:
            stored["text"] = text
        if stored is not None:
            registry["cost_dollars"] = round(
                float(registry.get("cost_dollars") or 0.0) + cost, 6
            )
        registry["updated"] = utcnow()
        write_json_atomic(work / "sources.json", registry)
    append_log(work, {
        "op": "fetch", "subq": None, "queries": [], "new_ids": [], "matched_ids": [],
        "fetched_id": sid, "cost": round(cost, 6), "ok": True, "error": None,
    })
    print(f"[{sid}] {page.get('title') or src.get('title') or ''} — {src['url']}")
    print(text or "(no text returned by /contents)")
    return 0


SPANISH_HINTS = frozenset("""
de la que el en los se del las un por con no una su para es al lo como mas pero sus le ya o
este si porque esta entre cuando muy sin sobre tambien me hasta hay donde quien desde todo nos
durante todos uno les ni contra otros ese eso ante ellos e esto mi antes algunos que unos yo
otro otras otra el tanto esa estos mucho quienes nada muchos cual poco ella estar estas algunas
algo nosotros mi mis tu te ti tus ellas nosotras vosotros vosotras os mio mia mios mias tuyo
tuya tuyos tuyas suyo suya suyos suyas nuestro nuestra nuestros nuestras vuestro vuestra
""".split())
ENGLISH_HINTS = frozenset("""
the of and to in a is that for it as was with be by on not he i this are or his from at which
but have an they you were her she there one all we their has had what would will can been when
who more no if out so said up its about into than them only other some could time these two
may then do first any my now such like our over man me even most made after also did many off
before must through back years where much your way well down should because each just those
people how too little state good very make world still own see men work long get here between
""".split())


def detect_language(text: str) -> str:
    """Best-effort stopword ratio; defaults to English for short/ambiguous text."""
    words = re.findall(r"[a-záéíóúüñ]+", text.lower())
    spanish = sum(1 for word in words if word in SPANISH_HINTS)
    english = sum(1 for word in words if word in ENGLISH_HINTS)
    return "es" if spanish > english else "en"


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", _strip_references(text)))


def _normalize_heading(text: str) -> str:
    import unicodedata
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", folded).strip()


REQUIRED_SECTIONS = (
    ("Resumen/Summary", ("resumen", "summary")),
    ("Qué hacer/What to do", ("que hacer", "what to do")),
    ("No verificado/Not verified", ("no verificado", "not verified")),
)


def _missing_sections(text: str) -> list[str]:
    headings = [_normalize_heading(item) for item in re.findall(r"^##\s+(.+?)\s*$", text, re.MULTILINE)]
    missing = []
    for label, names in REQUIRED_SECTIONS:
        if not any(
            heading == name or heading.startswith(name + " ") or heading.startswith(name + ":")
            for heading in headings for name in names
        ):
            missing.append(label)
    return missing


MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
DEADLINE_RE = re.compile(
    r"\b(cierre|plazo|fecha\s+l[ií]mite|inscripciones|deadline|closes|apply\s+by|"
    r"applications?\s+close|due|vence|vencen)\b",
    re.IGNORECASE,
)


def _dates_in_line(line: str) -> list[tuple["date", int, int]]:
    from datetime import date
    found: list[tuple[date, int, int]] = []
    for match in re.finditer(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", line):
        try:
            found.append((date(int(match.group(1)), int(match.group(2)), int(match.group(3))),
                          match.start(), match.end()))
        except ValueError:
            pass
    for match in re.finditer(r"\b(\d{1,2})[/.](\d{1,2})[/.](20\d{2})\b", line):
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        day, month = (first, second) if first <= 12 or second > 12 else (second, first)
        try:
            found.append((date(year, month, day), match.start(), match.end()))
        except ValueError:
            pass
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    for match in re.finditer(
        rf"\b({month_names})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b", line, re.IGNORECASE
    ):
        try:
            found.append((date(int(match.group(3)), MONTHS[match.group(1).lower()],
                               int(match.group(2))), match.start(), match.end()))
        except ValueError:
            pass
    for match in re.finditer(
        rf"\b(\d{{1,2}})\s+(?:de\s+)?({month_names})\s+(?:de\s+)?(20\d{{2}})\b",
        line, re.IGNORECASE,
    ):
        try:
            found.append((date(int(match.group(3)), MONTHS[match.group(2).lower()],
                               int(match.group(1))), match.start(), match.end()))
        except ValueError:
            pass
    return found


def _date_warnings(text: str) -> list[str]:
    from datetime import datetime as _datetime
    today = _datetime.now().date()
    warnings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        keywords = list(DEADLINE_RE.finditer(line))
        if not keywords:
            continue
        dates = _dates_in_line(line)
        for value, start, _end in dates:
            if value < today and any(abs(keyword.start() - start) <= 80 for keyword in keywords):
                warnings.append(
                    f"warning: line {lineno} mentions a deadline date in the past "
                    f"({value.isoformat()}): {line.strip()[:180]}"
                )
                break
    return warnings


def _strip_references(text: str) -> str:
    pattern = re.compile(r"^##\s+(references|sources|referencias|fuentes)\s*$",
                         re.IGNORECASE | re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return text
    return text[: matches[0].start()].rstrip() + "\n"


def _reference_line(index: int, src: dict) -> str:
    title = re.sub(r"\s+", " ", str(src.get("title") or "")).strip() or src.get("url", "")
    host = host_of(src.get("url", "")) or "source"
    day = format_date(src.get("published")) or "n.d."
    return f"{index}. {title} — {host}, {day}. {src.get('url')}"


def _body_paragraphs(text: str) -> list[str]:
    """Paragraphs used for the citation-coverage warning (no headings/code/tables)."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    paragraphs = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#") or block.startswith("|") or block.startswith(">"):
            continue
        paragraphs.append(block)
    return paragraphs


def cmd_finalize(args: argparse.Namespace) -> int:
    work = require_work(args.work)
    draft_path = Path(args.draft).expanduser().resolve()
    if not draft_path.is_file():
        die(f"error: draft not found: {draft_path}", 2)
    if not args.out:
        die("error: --out is required (e.g. --out ~/research/<slug>.md)", 2)
    out_path = Path(args.out).expanduser().resolve()
    registry = load_registry(work / "sources.json", slug_from_work(work))
    by_id = {src["id"]: src for src in registry["sources"]}
    try:
        text = draft_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        die(f"error: cannot read draft {draft_path}: {exc}", 2)
    language = getattr(args, "lang", None) or detect_language(text)

    unknown: list[tuple[str, int]] = []
    order: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in CITE_RE.finditer(line):
            for sid in [part.strip() for part in match.group(1).split(",")]:
                if sid not in by_id:
                    unknown.append((sid, lineno))
                elif sid not in order:
                    order.append(sid)
    if not order and not unknown:
        append_log(work, {
            "op": "finalize", "subq": None, "queries": [], "new_ids": [], "matched_ids": [],
            "fetched_id": None, "cost": None, "ok": False, "error": "draft contains no citations",
            "report": str(out_path),
        })
        die(
            "error: the draft contains no citations.\n"
            "Every factual claim needs an [S#] citation from sources.json. Fix the draft and re-run.",
            1,
        )
    if unknown:
        note("error: the draft cites unknown source ids:")
        for sid, lineno in unknown:
            note(f"  {sid} on line {lineno}")
        note("Fix the draft (or run search/fetch to add those sources) and re-run finalize.")
        append_log(work, {
            "op": "finalize", "subq": None, "queries": [], "new_ids": [], "matched_ids": [],
            "fetched_id": None, "cost": None, "ok": False,
            "error": "unknown source ids: " + ", ".join(sorted({sid for sid, _ in unknown})),
            "report": str(out_path),
        })
        return 1

    missing_sections = _missing_sections(text)
    if missing_sections:
        note("error: the draft is missing required sections:")
        for section in missing_sections:
            note(f"  {section}")
        note("Add the sections in the report language, then re-run finalize.")
        append_log(work, {
            "op": "finalize", "subq": None, "queries": [], "new_ids": [], "matched_ids": [],
            "fetched_id": None, "cost": None, "ok": False,
            "error": "missing sections: " + ", ".join(missing_sections),
            "report": str(out_path),
        })
        return 1

    numbers = {sid: index for index, sid in enumerate(order, 1)}

    def replace(match: re.Match) -> str:
        ids = [part.strip() for part in match.group(1).split(",")]
        links = [f"[[{numbers[sid]}]]({by_id[sid]['url']})" for sid in ids]
        return ", ".join(links)

    body = _strip_references(text)
    new_text = CITE_RE.sub(replace, body)
    warnings: list[str] = []
    word_count = _word_count(text)
    if word_count > 5000:
        warnings.append(
            f"warning: report is {word_count} words (target <= 4500, hard warning above 5000)"
        )
    warnings.extend(_date_warnings(text))
    for warning in warnings:
        note(warning)

    paragraphs = _body_paragraphs(new_text)
    cited = sum(1 for para in paragraphs if CITE_LINK_RE.search(para))
    ratio = (cited / len(paragraphs)) if paragraphs else 0.0
    if paragraphs and ratio < MAX_PARAGRAPH_CITATION_WARN:
        note(
            f"warning: only {cited}/{len(paragraphs)} body paragraphs "
            f"({ratio:.0%}) contain a citation (target >= 60%)"
        )

    refs = ["## Referencias" if language == "es" else "## References", ""]
    for sid in order:
        refs.append(_reference_line(numbers[sid], by_id[sid]))
    final_text = new_text.rstrip() + "\n\n" + "\n".join(refs) + "\n"

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(final_text, encoding="utf-8")
    except OSError as exc:
        die(f"error: cannot write report to {out_path}: {exc}", 1)
    write_json_atomic(
        work / "final.json",
        {
            "out": str(out_path),
            "at": utcnow(),
            "sources_cited": len(order),
            "citations": {sid: numbers[sid] for sid in order},
            "coverage": {
                "cited_paragraphs": cited,
                "paragraphs": len(paragraphs),
                "ratio": round(ratio, 4),
            },
            "language": language,
            "word_count": word_count,
            "warnings": warnings,
        },
    )
    append_log(work, {
        "op": "finalize", "subq": None, "queries": [], "new_ids": [], "matched_ids": [],
        "fetched_id": None, "cost": None, "ok": True, "error": None,
        "report": str(out_path),
        "citations": len(order),
        "coverage": f"{cited}/{len(paragraphs)}",
        "coverage_ratio": round(ratio, 4),
        "language": language,
        "word_count": word_count,
        "warnings": len(warnings),
    })
    print(f"report: {out_path}")
    print(f"citations: {len(order)} distinct sources, renumbered to [[1]]..[[{len(order)}]]")
    print(f"paragraph citation coverage: {cited}/{len(paragraphs)} ({ratio:.0%})")
    print(f"references: {len(order)}")
    return 0


CONFIDENCE_VALUES = frozenset({"high", "medium", "low"})
DATE_STATUS_VALUES = frozenset({"open", "past", "unknown"})


def validate_finding(data, qid: str = "") -> list[str]:
    """Shape errors for one findings JSON file (used by status and tests)."""
    label = qid or str(data.get("id") if isinstance(data, dict) else "?")
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"{label}: findings must be a JSON object"]
    if not isinstance(data.get("id"), str) or not QID_RE.match(data.get("id", "")):
        errors.append(f"{label}: id must match ^q[0-9]+$")
    if not isinstance(data.get("question"), str) or not data.get("question", "").strip():
        errors.append(f"{label}: question must be a non-empty string")
    if not isinstance(data.get("answer_summary"), str) or not data.get("answer_summary", "").strip():
        errors.append(f"{label}: answer_summary must be a non-empty string")
    findings = data.get("findings")
    if not isinstance(findings, list) or not findings:
        errors.append(f"{label}: findings must be a non-empty list")
        findings = []
    for index, item in enumerate(findings):
        where = f"{label}: findings[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        if not isinstance(item.get("claim"), str) or not item.get("claim", "").strip():
            errors.append(f"{where}.claim must be a non-empty string")
        source_ids = item.get("source_ids")
        if not isinstance(source_ids, list) or not source_ids:
            errors.append(f"{where}.source_ids must be a non-empty list of S-ids")
        else:
            for sid in source_ids:
                if not isinstance(sid, str) or not re.match(r"^S[0-9]+$", sid):
                    errors.append(f"{where}.source_ids contains a non-S-id: {sid!r}")
        if item.get("confidence") not in CONFIDENCE_VALUES:
            errors.append(f"{where}.confidence must be high, medium or low")
        if not isinstance(item.get("verified"), bool):
            errors.append(f"{where}.verified must be true or false")
        if "date_status" in item and item.get("date_status") not in DATE_STATUS_VALUES:
            errors.append(f"{where}.date_status must be open, past or unknown")
    for field in ("contradictions", "gaps"):
        values = data.get(field)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            errors.append(f"{label}: {field} must be a list of strings")
    return errors


def cmd_status(args: argparse.Namespace) -> int:
    work = require_work(args.work)
    slug = slug_from_work(work)
    plan_path = work / "plan.json"
    subquestions: list[dict] = []
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            subquestions = [item for item in plan.get("subquestions") or [] if isinstance(item, dict)]
            slug = str(plan.get("slug") or slug)
            print(f"plan:      [x] {len(subquestions)} subquestions — {plan.get('topic') or '(no topic)'}")
        except json.JSONDecodeError:
            print("plan:      [!] plan.json is not valid JSON")
    else:
        print("plan:      [ ] missing (write plan.json)")

    registry = load_registry(work / "sources.json", slug)
    count = len(registry["sources"])
    print(f"sources:   [{'x' if count else ' '}] {count} unique (cost ${float(registry.get('cost_dollars') or 0):.4f})")

    qids = [str(item.get("id")) for item in subquestions if item.get("id")]
    evidence = [qid for qid in qids if (work / "evidence" / f"{qid}.md").exists()]
    findings = [qid for qid in qids if (work / "findings" / f"{qid}.json").exists()]
    if qids:
        missing_ev = [qid for qid in qids if qid not in evidence]
        missing_fi = [qid for qid in qids if qid not in findings]
        print(f"evidence:  [{'x' if not missing_ev else ' '}] {len(evidence)}/{len(qids)}" +
              (f" missing: {', '.join(missing_ev)}" if missing_ev else ""))
        print(f"findings:  [{'x' if not missing_fi else ' '}] {len(findings)}/{len(qids)}" +
              (f" missing: {', '.join(missing_fi)}" if missing_fi else ""))
    else:
        print(f"evidence:  [{'x' if evidence else ' '}] {len(evidence)} files")
        print(f"findings:  [{'x' if findings else ' '}] {len(findings)} files")

    shape_errors: list[str] = []
    for qid in findings:
        try:
            data = json.loads((work / "findings" / f"{qid}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            shape_errors.append(f"{qid}: not valid JSON ({exc})")
            continue
        shape_errors.extend(validate_finding(data, qid))
    if shape_errors:
        print(f"findings shape: [!] {len(shape_errors)} issue(s)")
        for error in shape_errors[:5]:
            print(f"  {error}")

    draft = work / "draft.md"
    print(f"draft:     [{'x' if draft.exists() else ' '}] {draft if draft.exists() else 'missing'}")

    final_info = {}
    if (work / "final.json").exists():
        try:
            final_info = json.loads((work / "final.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            final_info = {}
    final_md = Path(final_info.get("out") or research_dir() / f"{slug}.md")
    print(f"final:     [{'x' if final_md.exists() else ' '}] {final_md}")

    if not plan_path.exists():
        print("next: write plan.json (SKILL.md step 2), then run search")
    elif count == 0:
        print("next: run search --work <work dir>")
    elif qids and len(findings) < len(qids):
        print(f"next: produce findings for {', '.join(q for q in qids if q not in findings)} "
              "(delegate or research them yourself)")
    elif not draft.exists():
        print("next: write draft.md from templates/report.md")
    elif not final_md.exists():
        print("next: run finalize")
    else:
        print("next: deliver the summary + MEDIA line")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research.py",
        description="Deterministic deep-research pipeline helper (Exa REST + local files).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical flow:\n"
            "  research.py init --slug 2026-11-15-topic\n"
            "  research.py search --work ~/research/.work/2026-11-15-topic\n"
            "  research.py search --work DIR --subq q3 --query 'follow-up query'\n"
            "  research.py fetch --work DIR --id S12\n"
            "  research.py finalize --work DIR --draft DIR/draft.md --out ~/research/topic.md\n"
            "  research.py status --work DIR\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create the work dir for a slug")
    p_init.add_argument("--slug", required=True, help="slug matching ^[a-z0-9][a-z0-9-]{1,60}$")
    p_init.add_argument("--work", help="override the work dir (default ~/research/.work/<slug>)")
    p_init.set_defaults(func=cmd_init)

    p_search = sub.add_parser(
        "search",
        help="run plan.json queries in parallel, or ad-hoc follow-up queries for one subquestion",
    )
    p_search.add_argument("--work", required=True, help="work dir (from init)")
    p_search.add_argument("--subq", help="ad-hoc mode: subquestion id, e.g. q3")
    p_search.add_argument("--query", action="append", help="ad-hoc mode: query (repeatable)")
    p_search.add_argument("--workers", type=int, default=8, help="parallel queries (default 8)")
    p_search.add_argument("--num-results", type=int, default=None,
                          help=f"results per query (default ${NUM_RESULTS_ENV} or {DEFAULT_NUM_RESULTS})")
    p_search.add_argument("--max-chars", type=int, default=None,
                          help=f"page text characters per source (default ${MAX_CHARS_ENV} or {DEFAULT_MAX_CHARS})")
    p_search.add_argument("--dry-run", action="store_true", help="validate plan.json and print the query count; no API calls")
    p_search.set_defaults(func=cmd_search)

    p_fetch = sub.add_parser("fetch", help="fetch full page text for a known source id")
    p_fetch.add_argument("--work", required=True, help="work dir (from init)")
    p_fetch.add_argument("--id", required=True, help="source id, e.g. S12")
    p_fetch.add_argument("--max-chars", type=int, default=20000, help="max characters (default 20000)")
    p_fetch.set_defaults(func=cmd_fetch)

    p_final = sub.add_parser("finalize", help="validate citations, renumber, add References")
    p_final.add_argument("--work", required=True, help="work dir (from init)")
    p_final.add_argument("--draft", required=True, help="path to the draft markdown")
    p_final.add_argument("--out", required=True, help="final report path, e.g. ~/research/<slug>.md")
    p_final.add_argument("--lang", choices=("es", "en"),
                         help="report language for the References heading (auto-detected when omitted)")
    p_final.set_defaults(func=cmd_finalize)

    p_status = sub.add_parser("status", help="show which pipeline steps are done")
    p_status.add_argument("--work", required=True, help="work dir (from init)")
    p_status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        die("error: interrupted", 130)


if __name__ == "__main__":
    sys.exit(main())
