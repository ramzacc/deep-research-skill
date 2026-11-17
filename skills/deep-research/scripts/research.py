#!/usr/bin/env python3
"""Deterministic helper for the deep-research skill.

Subcommands
-----------
init      create the work dir for a slug and print the paths to use
search    batch (plan.json) or ad-hoc (-subq/-query) web searches, merged on disk
fetch     full text of one source id (or a new URL), with optional --grep windows
finalize  validate + renumber citations, add References, write the final report
status    show which pipeline steps are done (used to resume)

Search and page fetching use Exa's Search & Contents API (type "lite"; billed
per request from each response's `costDollars.total`). All state lives in a work
dir (default: ~/research/.work/<slug>/):

    plan.json                 written by the agent
    sources.json              source registry (stable S-ids, merged under flock)
    pages/<S#>.md             full page text per source, used for evidence passages
    evidence/<qid>.md         per-subquestion digest for subagents
    findings/<qid>.json       written by subagents (or by the agent)
    draft.md                  written by the agent
    final.json                written by finalize (final md path and citation map)

Only the Python standard library is used. Python >= 3.10.

Exit codes: 0 ok, 1 operation failed (bad citations, network error, no grep
match), 2 bad input (invalid slug/plan/arguments or missing API key).
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
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"
# Page text requested from /search and cached in pages/<S#>.md. The short text
# kept in sources.json is capped separately by DEEP_RESEARCH_MAX_CHARS.
SEARCH_TEXT_MAX_CHARS = 20_000
SITE_TOKEN_RE = re.compile(r"\bsite:(\S+)", re.IGNORECASE)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")
QID_RE = re.compile(r"^q[0-9]+$")
IDENTIFY_Q1_RE = re.compile(
    r"^Identify exactly which .+ is meant: its canonical name, current URL and "
    r"authorship history\.?$",
    re.DOTALL,
)
CITE_RE = re.compile(r"\[(S[0-9]+(?:\s*,\s*S[0-9]+)*)\]")
CITE_LINK_RE = re.compile(r"\[\[[0-9]+\]\]\(")
BARE_ID_RE = re.compile(r"\bS[0-9]+\b")
BARE_ID_LIST_RE = re.compile(r"\bS[0-9]+(?:\s*[,;/]\s*S[0-9]+)+\b")
BARE_ID_CELL_RE = re.compile(r"^\s*S[0-9]+\s*$")
MAX_PARAGRAPH_CITATION_WARN = 0.60

# Test-only knobs (documented in README.md). Never set these in production.
MOCK_ENV = "DEEP_RESEARCH_MOCK"
BACKOFF_ENV = "DEEP_RESEARCH_BACKOFF_BASE"

# Results per query. DEEP_RESEARCH_NUM_RESULTS is the default; --num-results wins.
# Exa lite's base price covers up to 10 results; more costs extra.
NUM_RESULTS_ENV = "DEEP_RESEARCH_NUM_RESULTS"
DEFAULT_NUM_RESULTS = 10
# Characters of page text kept per source in sources.json (the full page goes to
# pages/<S#>.md). DEEP_RESEARCH_MAX_CHARS is the default; --max-chars wins.
MAX_CHARS_ENV = "DEEP_RESEARCH_MAX_CHARS"
DEFAULT_MAX_CHARS = 6000
TEXT_MAX_CHARS = DEFAULT_MAX_CHARS
# Full page text cached per source in pages/<S#>.md.
PAGE_MAX_CHARS = 300_000
PAGES_DIR = "pages"
# Evidence digest: characters of selected passages per source, and how many
# sources get a full section (the rest are one-line entries).
PASSAGE_CHARS = 1500
DIGEST_FULL_LIMIT = 25
# fetch --grep: context characters around a match, and the window cap.
GREP_WINDOW = 400
GREP_MAX_WINDOWS = 15
EXA_KEY_ENV = "DEEP_RESEARCH_EXA_KEY"
LOG_FILE = "log.jsonl"
QUERY_INDEX = "queries.json"

# Hosts whose content is user-generated or low accountability; tagged in the
# evidence digest, never filtered out.
SOCIAL_HOSTS = (
    "facebook.com", "instagram.com", "threads.net", "tiktok.com", "x.com",
    "twitter.com", "linkedin.com", "quora.com", "reddit.com", "weixin.qq.com",
    "qq.com", "zhihu.com", "medium.com", "substack.com", "youtube.com",
)

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


def _mock_route(payload: dict, endpoint: str = "search") -> dict | None:
    fixture = _load_mock_fixture()
    routes = fixture.get("routes") or []
    haystack = str(payload.get("query", ""))
    if not haystack:
        haystack = " ".join(str(url) for url in payload.get("urls") or [])
    matches = []
    for idx, route in enumerate(routes):
        if not isinstance(route, dict):
            continue
        wanted = route.get("endpoint")
        if wanted and wanted != endpoint:
            continue
        needle = route.get("contains")
        if needle and needle not in haystack:
            continue
        matches.append((idx, route))
    if not matches:
        key = "default_contents" if endpoint == "contents" else "default_search"
        default = fixture.get(key) or fixture.get("default")
        return {"responses": [default]} if default else None
    # most specific match wins: longest matched needle, then declaration order
    matches.sort(key=lambda pair: (-len(pair[1].get("contains") or ""), pair[0]))
    return matches[0][1]


def _mock_response(endpoint: str, payload: dict) -> dict:
    route = _mock_route(payload, endpoint)
    if route is None:
        raise HttpError(599, f"no mock route for {endpoint} payload {json.dumps(payload)[:300]}")
    responses = route.get("responses") or [{"json": {}}]
    key = f"{os.environ.get(MOCK_ENV)}::{id(route)}"
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


def http_post_json(url: str, payload: dict, timeout: float = 45.0,
                   headers: dict[str, str] | None = None) -> dict:
    """POST JSON, return parsed JSON. Raises HttpError on any failure."""
    endpoint = "contents" if url.rstrip("/").endswith("/contents") else "search"
    if os.environ.get(MOCK_ENV):
        return _mock_response(endpoint, payload)
    body = json.dumps(payload).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": "hermes-deep-research/1.0",
    }
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=request_headers,
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
    """Results per query: DEEP_RESEARCH_NUM_RESULTS, then 10. The CLI flag wins."""
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


def parse_today_arg(value: str | None) -> date:
    """--today YYYY-MM-DD (default: the system date)."""
    if not value:
        return today_date()
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        die(f"error: --today must be YYYY-MM-DD (got {value!r})", 2)


def exa_key_required() -> str:
    key = os.environ.get(EXA_KEY_ENV, "").strip()
    if not key:
        die(
            f"error: {EXA_KEY_ENV} is not set.\n"
            "Deep research needs an Exa API key:\n"
            f"  hermes config set {EXA_KEY_ENV} <your key>",
            2,
        )
    return key


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


def split_site_operators(query: str) -> tuple[str, list[str]]:
    """Move site:domain tokens out of the query text (Exa wants includeDomains).

    Returns (query_text, domains). A query made only of site: tokens keeps its
    original text; the domains are still returned.
    """
    domains: list[str] = []

    def collect(match: re.Match) -> str:
        domain = match.group(1).strip().strip("\"'").lower().rstrip("/")
        if domain and domain not in domains:
            domains.append(domain)
        return " "

    stripped = " ".join(SITE_TOKEN_RE.sub(collect, query).split())
    if not domains:
        return query, domains
    return (stripped or query), domains


def _exa_post(url: str, payload: dict, api_key: str) -> dict:
    try:
        return http_post_json(url, payload, headers={"x-api-key": api_key})
    except HttpError as exc:
        if exc.status == 402:
            raise HttpError(
                402, "insufficient balance on the Exa account; top up your balance"
            ) from exc
        raise


def _cost_dollars(data: dict) -> float:
    try:
        return float((data.get("costDollars") or {}).get("total") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def exa_search(query: str, num_results: int, api_key: str) -> tuple[list[dict], float]:
    """One Exa /search request (type lite). Returns (results, cost).

    Each result carries the page text (capped at SEARCH_TEXT_MAX_CHARS) and its
    highlights; the registry keeps the joined highlights as `summary` and only
    the leading TEXT_MAX_CHARS of text.
    """
    query_text, domains = split_site_operators(query)
    payload = {
        "query": query_text,
        "type": "lite",
        "numResults": num_results,
        "contents": {
            "text": {"maxCharacters": SEARCH_TEXT_MAX_CHARS},
            "highlights": True,
        },
    }
    if domains:
        payload["includeDomains"] = domains
    data = _exa_post(EXA_SEARCH_URL, payload, api_key)
    raw = data.get("results") or []
    if not isinstance(raw, list):
        raw = []
    results: list[dict] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        highlights = item.get("highlights")
        if not isinstance(highlights, list):
            highlights = []
        summary = " ".join(str(hl).strip() for hl in highlights if str(hl).strip())
        results.append({
            "url": url,
            "title": str(item.get("title") or "").strip(),
            "publishedDate": item.get("publishedDate") or "",
            "summary": summary,
            "content": str(item.get("text") or "")[:PAGE_MAX_CHARS],
            # Exa returns no score; keep its relevance order on DeepSeek's 0-3 rerank scale.
            "rerank": round(max(0.5, 2.0 - 0.1 * position), 2),
        })
    return results, _cost_dollars(data)


def exa_contents(url: str, max_chars: int, api_key: str) -> tuple[dict, float]:
    payload = {"urls": [url], "text": {"maxCharacters": max_chars}}
    data = _exa_post(EXA_CONTENTS_URL, payload, api_key)
    results = data.get("results") or []
    if not results or not isinstance(results[0], dict):
        raise HttpError(502, f"/contents returned no result for {url}")
    item = results[0]
    page = {
        "title": str(item.get("title") or "").strip(),
        "text": str(item.get("text") or "")[:max_chars],
    }
    return page, _cost_dollars(data)


# --------------------------------------------------------------------------
# page cache
# --------------------------------------------------------------------------

def page_path(work: Path, sid: str) -> Path:
    return work / PAGES_DIR / f"{sid}.md"


def read_page(work: Path, sid: str) -> str:
    try:
        return page_path(work, sid).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def store_page(work: Path, sid: str, content: str) -> bool:
    """Cache page text, keeping the longer copy. Returns True when written."""
    content = str(content or "")
    if not content:
        return False
    if len(content) <= len(read_page(work, sid)):
        return False
    try:
        (work / PAGES_DIR).mkdir(parents=True, exist_ok=True)
        write_text_atomic(page_path(work, sid), content[:PAGE_MAX_CHARS])
    except OSError as exc:
        note(f"warning: could not cache page for {sid}: {exc}")
        return False
    return True


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


def normalize_query(query: str) -> str:
    """Case/whitespace/punctuation-insensitive key for the duplicate guard."""
    lowered = str(query or "").lower()
    cleaned = re.sub(r"[^\w\s]", " ", lowered, flags=re.UNICODE)
    return " ".join(cleaned.split())


def load_query_index(work: Path) -> dict:
    path = work / QUERY_INDEX
    if not path.exists():
        return {"queries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        note(f"warning: ignoring unreadable {path}: {exc}")
        return {"queries": {}}
    if not isinstance(data, dict) or not isinstance(data.get("queries"), dict):
        return {"queries": {}}
    return data


def reserve_queries(work: Path, queries: list[str]) -> tuple[list[str], list[str]]:
    """Reserve new queries under the source lock; return (new, skipped duplicates).

    Reserving before the API call keeps two concurrent ad-hoc researchers from
    paying for the same query. A reserved query that fails remains reserved.
    """
    new: list[str] = []
    skipped: list[str] = []
    with sources_lock(work):
        data = load_query_index(work)
        index = data.setdefault("queries", {})
        for query in queries:
            key = normalize_query(query)
            if key in index:
                skipped.append(query)
                continue
            index[key] = {"query": query, "at": utcnow()}
            new.append(query)
        data["updated"] = utcnow()
        try:
            write_json_atomic(work / QUERY_INDEX, data)
        except OSError as exc:
            note(f"warning: could not write {work / QUERY_INDEX}: {exc}")
    return new, skipped


def _next_source_num(registry: dict) -> int:
    next_num = 1
    for src in registry["sources"]:
        match = re.match(r"^S([0-9]+)$", str(src.get("id", "")))
        if match:
            next_num = max(next_num, int(match.group(1)) + 1)
    return next_num


def merge_records(registry: dict, records: list[dict], changes: list[dict] | None = None,
                  work: Path | None = None, text_chars: int = TEXT_MAX_CHARS) -> tuple[list[str], list[str]]:
    """Merge search records into the registry in place.

    records: [{"qid": "q1", "query": "...", "results": [exa result, ...]}]
    Returns (new_ids, touched_ids). When *changes* is a list, one
    {"new_ids": [...], "matched_ids": [...]} entry per record is appended.
    Full page content is cached in pages/<S#>.md when *work* is given.
    """
    by_norm = {normalize_url(src.get("url", "")): src for src in registry["sources"]}
    next_num = _next_source_num(registry)
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
                    "subquestions": [],
                    "queries": [],
                    "summary": "",
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
            if not src.get("summary"):
                src["summary"] = str(item.get("summary") or "").strip()
            for field in ("authority", "rerank"):
                value = item.get(field)
                if not _is_number(value):
                    continue
                if src.get(field) is None or value > src[field]:
                    src[field] = value
            if qid and qid not in src["subquestions"]:
                src["subquestions"].append(qid)
            if query and query not in src["queries"]:
                src["queries"].append(query)
            content = str(item.get("content") or "")
            if content:
                short = content[:text_chars]
                if len(short) > len(src.get("text") or ""):
                    src["text"] = short
            if work is not None:
                store_page(work, src["id"], content)
            touched.append(src["id"])
            record_touched.append(src["id"])
        if changes is not None:
            changes.append({"new_ids": record_new, "matched_ids": record_touched})
    # keep first-appearance order stable even if an id was seen again later
    registry["updated"] = utcnow()
    return new_ids, touched


def register_url(work: Path, slug: str, url: str, qid: str) -> tuple[dict, bool]:
    """Add a URL that no search returned yet. Returns (source, created)."""
    norm = normalize_url(url)
    with sources_lock(work):
        registry = load_registry(work / "sources.json", slug)
        src = next((s for s in registry["sources"] if normalize_url(s.get("url", "")) == norm), None)
        created = src is None
        if src is None:
            src = {
                "id": f"S{_next_source_num(registry)}",
                "url": url,
                "title": "",
                "published": "",
                "subquestions": [qid] if qid else [],
                "queries": [],
                "summary": "",
                "text": "",
            }
            registry["sources"].append(src)
        elif qid and qid not in src.setdefault("subquestions", []):
            src["subquestions"].append(qid)
        registry["updated"] = utcnow()
        write_json_atomic(work / "sources.json", registry)
    return src, created


def source_by_id(registry: dict, sid: str) -> dict | None:
    for src in registry["sources"]:
        if src.get("id") == sid:
            return src
    return None


def sources_for(registry: dict, qid: str) -> list[dict]:
    return [src for src in registry["sources"] if qid in (src.get("subquestions") or [])]


# --------------------------------------------------------------------------
# passages, ranking, evidence
# --------------------------------------------------------------------------

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
both life being under never day same another know while last might us great old year come since
against go came right used take three
""".split())
QUERY_STOPWORDS = SPANISH_HINTS | ENGLISH_HINTS | frozenset({
    "search", "results", "result", "http", "https", "www", "com", "site", "query",
    "official", "página", "pagina", "page", "filetype", "pdf",
})
WORD_RE = re.compile(r"[a-z0-9]+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ0-9\"“'])")
PASSAGE_VALUE_RE = re.compile(
    r"\d|\b(?:usd|eur|pen|mxn|clp|ars|cop|brl|gbp|soles?|d[óo]lares?)\b|[€£¥$]",
    re.IGNORECASE,
)


def fold(text: str) -> str:
    """Lowercase and strip accents (NFKD) for term matching."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def query_terms(*texts: str) -> set[str]:
    """Distinct content words of the query text, accent-folded, stopwords removed."""
    terms: set[str] = set()
    for text in texts:
        for word in WORD_RE.findall(fold(text)):
            if len(word) < 3 or word.isdigit() or word in QUERY_STOPWORDS:
                continue
            terms.add(word)
    return terms


def split_passages(text: str, max_unit: int = 600) -> list[str]:
    """Paragraphs, long ones split into sentences, each unit <= max_unit chars."""
    units: list[str] = []
    for block in re.split(r"\n\s*\n", str(text or "")):
        block = " ".join(block.split())
        if not block:
            continue
        if len(block) <= max_unit:
            units.append(block)
            continue
        for sentence in SENTENCE_SPLIT_RE.split(block):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_unit:
                units.append(sentence)
            else:
                units.extend(
                    sentence[start:start + max_unit]
                    for start in range(0, len(sentence), max_unit)
                )
    return units


def select_passages(content: str, terms: set[str], max_chars: int = PASSAGE_CHARS) -> str:
    """Most query-relevant passages of *content*, in document order, `…` between.

    A passage scores one point per distinct query term it contains, plus a bonus
    when it carries a number, currency or date (where verifiable values live).
    Falls back to the leading text when nothing matches.
    """
    units = split_passages(content)
    if not units:
        return ""
    if not terms:
        return " ".join(units)[:max_chars]
    scored: list[tuple[float, int]] = []
    for index, unit in enumerate(units):
        folded = fold(unit)
        words = set(WORD_RE.findall(folded))
        hits = len(words & terms)
        if not hits:
            continue
        bonus = 2.0 if PASSAGE_VALUE_RE.search(unit) else 0.0
        scored.append((hits + bonus, index))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    chosen: list[tuple[int, str]] = []
    used = 0
    for _score, index in scored:
        extra = len("\n…\n") if chosen else 0
        remaining = max_chars - used - extra
        if remaining <= 0:
            break
        piece = units[index][:remaining].rstrip()
        if not piece:
            continue
        chosen.append((index, piece))
        used += len(piece) + extra
    if not chosen:
        return " ".join(units)[:max_chars]
    chosen.sort()
    return "\n…\n".join(piece for _index, piece in chosen)


def source_passages(work: Path, src: dict, question: str,
                    max_chars: int = PASSAGE_CHARS) -> str:
    """Passages for one source: cached full page, else the short registry text.

    Query terms come from the subquestion plus every query that returned this
    source (the registry keeps one queries list per source, not per subquestion).
    """
    content = read_page(work, src.get("id", "")) or str(src.get("text") or "")
    terms = query_terms(question, *(src.get("queries") or []))
    return select_passages(content, terms, max_chars)


def _sid_num(sid) -> int:
    digits = re.sub(r"[^0-9]", "", str(sid or ""))
    return int(digits) if digits else 0


def today_date() -> "date":
    return datetime.now().date()


def parse_iso_date(value) -> "date | None":
    """Parse a registry `published` value (ISO timestamp, date or YYYY-MM)."""
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = re.match(r"^(\d{4})-(\d{2})$", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), 1)
        except ValueError:
            return None
    return None


YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def year_hint(url: str, title: str, current_year: int) -> int | None:
    """Newest plausible year in a URL or title, ignoring years inside digit runs."""
    years = [
        int(match.group(1))
        for match in YEAR_RE.finditer(f"{url or ''} {title or ''}")
        if 1990 <= int(match.group(1)) <= current_year
    ]
    return max(years) if years else None


def _years_before(today: "date", years: int) -> "date":
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def recency_bonus(src: dict, today: "date | None" = None) -> float:
    """Ranking nudge for freshness: new values usually live in recent pages."""
    today = today or today_date()
    when = parse_iso_date(src.get("published"))
    if when is not None:
        age_days = max(0, (today - when).days)
        if age_days <= 90:
            return 0.6
        if age_days <= 365:
            return 0.3
        if when < _years_before(today, 2):
            return -0.4
        return 0.0
    hint = year_hint(src.get("url", ""), src.get("title", ""), today.year)
    if hint is None:
        return 0.0
    if hint == today.year:
        return 0.4
    if hint == today.year - 1:
        return 0.1
    if hint <= today.year - 3:
        return -0.5
    return 0.0


def source_score(src: dict, today: "date | None" = None) -> float:
    """Combined evidence score: rerank score (when present) plus the recency bonus."""
    rerank = src.get("rerank")
    base = float(rerank) if _is_number(rerank) else 0.0
    return round(base + recency_bonus(src, today), 6)


def _rank_value(src: dict, today: "date | None") -> tuple[float, int, str]:
    authority = src.get("authority")
    return (
        source_score(src, today),
        int(authority) if isinstance(authority, int) and not isinstance(authority, bool) else -1,
        format_date(src.get("published")),
    )


def rank_sources(sources: list[dict], today: "date | None" = None) -> list[dict]:
    """Order by combined rerank+recency score, then authority, then newest; stable on S-id."""
    today = today or today_date()
    ordered = sorted(sources, key=lambda src: _sid_num(src.get("id")))
    return sorted(ordered, key=lambda src: _rank_value(src, today), reverse=True)


def is_social_host(url: str) -> bool:
    host = host_of(url)
    return any(host == domain or host.endswith("." + domain) for domain in SOCIAL_HOSTS)


def is_possibly_outdated(src: dict, today: "date | None" = None) -> bool:
    """True when the date or year hint is two or more years old."""
    today = today or today_date()
    when = parse_iso_date(src.get("published"))
    if when is not None:
        return when <= _years_before(today, 2)
    hint = year_hint(src.get("url", ""), src.get("title", ""), today.year)
    return hint is not None and today.year - hint >= 2


def source_quality_tags(src: dict, today: "date | None" = None) -> list[str]:
    """Digest tags that warn about source quality. They never filter a source out."""
    tags: list[str] = []
    authority = src.get("authority")
    if isinstance(authority, int) and not isinstance(authority, bool) and 0 <= authority <= 1:
        tags.append("low-authority")
    if is_social_host(src.get("url", "")):
        tags.append("social/forum")
    if is_possibly_outdated(src, today):
        tags.append("possibly-outdated")
    return tags


def format_date(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else text[:24]


def _when_label(src: dict, today: "date | None" = None) -> str:
    day = format_date(src.get("published"))
    if day:
        return day
    today = today or today_date()
    hint = year_hint(src.get("url", ""), src.get("title", ""), today.year)
    return f"undated (year hint {hint})" if hint else "undated"


def _source_header(src: dict, today: "date | None" = None) -> str:
    when = _when_label(src, today)
    header = f"### [{src['id']}] {src.get('title') or '(untitled)'} ({when}) — {src.get('url')}"
    authority = src.get("authority")
    if isinstance(authority, int) and not isinstance(authority, bool):
        header += f" · authority {authority}"
    rerank = src.get("rerank")
    if _is_number(rerank):
        header += f" · rerank {rerank:g}"
    tags = source_quality_tags(src, today)
    if tags:
        header += " · " + " · ".join(tags)
    return header


def source_section(work: Path, src: dict, question: str,
                   max_text_chars: int = PASSAGE_CHARS,
                   today: "date | None" = None) -> str:
    lines = [_source_header(src, today)]
    summary = " ".join(str(src.get("summary") or "").split())
    if summary:
        lines.append(f"- {summary}")
    passages = source_passages(work, src, question, max_text_chars)
    if passages:
        lines += ["", passages]
    lines.append("")
    return "\n".join(lines)


def source_one_liner(src: dict, today: "date | None" = None) -> str:
    authority = src.get("authority")
    if not isinstance(authority, int) or isinstance(authority, bool):
        authority_text = "authority unknown"
    else:
        authority_text = f"authority {authority}"
    line = (
        f"[{src['id']}] {src.get('title') or '(untitled)'} — {src.get('url')} · "
        f"{authority_text} · {_when_label(src, today)}"
    )
    tags = source_quality_tags(src, today)
    if tags:
        line += " · " + " · ".join(tags)
    return line


def render_evidence(qid: str, question: str, sources: list[dict], work: Path,
                    today: "date | None" = None) -> str:
    today = today or today_date()
    lines = [f"# {qid} — {question}", ""]
    if not sources:
        lines += ["No sources collected for this subquestion yet.", ""]
        return "\n".join(lines)
    ranked = rank_sources(sources, today)
    full = ranked[:DIGEST_FULL_LIMIT]
    rest = ranked[DIGEST_FULL_LIMIT:]
    lines += [
        f"{len(sources)} candidate sources. Cite them as [S#]; ids are stable.",
        f"Full sections for the top {len(full)} (best recency, then authority and date); the "
        "rest are one-liners you can still fetch with `fetch --id S#` and find with "
        "`recent --grep`.",
        "",
    ]
    for src in full:
        lines.append(source_section(work, src, question, PASSAGE_CHARS, today))
    if rest:
        lines += ["## Other candidates", ""]
        lines += [source_one_liner(src, today) for src in rest]
        lines.append("")
    return "\n".join(lines)


def append_evidence(path: Path, qid: str, question: str, sources: list[dict],
                    work: Path, today: "date | None" = None) -> list[dict]:
    """Append source sections not already present. Returns the sources appended."""
    today = today or today_date()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    fresh = [src for src in rank_sources(sources, today) if f"### [{src['id']}]" not in existing]
    if not fresh:
        return []
    chunk = "\n".join(source_section(work, src, question, PASSAGE_CHARS, today) for src in fresh)
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
    post_actions = plan.get("post_actions", [])
    if not isinstance(post_actions, list):
        errors.append("plan.json: post_actions must be a list of strings when present")
        post_actions = []
    else:
        for index, action in enumerate(post_actions):
            if not isinstance(action, str) or not action.strip():
                errors.append(f"plan.json: post_actions[{index}] must be a non-empty string")
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
    (work / PAGES_DIR).mkdir(parents=True, exist_ok=True)
    plan_path = work / "plan.json"
    print(f"work dir: {work}")
    print(f"plan file: {plan_path}")
    print(f"evidence dir: {work / 'evidence'}")
    print(f"findings dir: {work / 'findings'}")
    print(f"pages dir: {work / PAGES_DIR}")
    if plan_path.exists():
        print("note: plan.json already exists; it will be reused (overwrite it to re-plan)")
    if not (work / "sources.json").exists():
        write_json_atomic(work / "sources.json", empty_registry(slug))
        print(f"sources file: {work / 'sources.json'}")
    return 0


def _execute_queries(pairs: list[tuple[str, str]], args: argparse.Namespace,
                     api_key: str) -> tuple[list[dict], int]:
    """Run (qid, query) pairs in parallel. Returns (records, failures)."""
    records: list[dict] = []
    failures = 0

    def run(query: str) -> tuple[list[dict], float]:
        return exa_search(query, args.num_results, api_key)

    workers = max(1, min(args.workers, len(pairs) or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(with_retries, lambda q=query: run(q), 3, query): (qid, query)
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
    today = parse_today_arg(getattr(args, "today", None))
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
    api_key = exa_key_required() if pairs else ""
    new_queries, skipped = reserve_queries(work, [query for _qid, query in pairs])
    for query in skipped:
        print(f"skipped duplicate: {query}")
    remaining: dict[str, int] = {}
    for query in new_queries:
        remaining[query] = remaining.get(query, 0) + 1
    effective_pairs = []
    for qid, query in pairs:
        if remaining.get(query, 0) > 0:
            effective_pairs.append((qid, query))
            remaining[query] -= 1
    if effective_pairs:
        note(f"searching: {len(effective_pairs)} queries with {args.workers} workers")
        records, failures = _execute_queries(effective_pairs, args, api_key)
    else:
        records, failures = [], 0

    with sources_lock(work):
        registry = load_registry(work / "sources.json", slug)
        if plan.get("topic") and not registry.get("topic"):
            registry["topic"] = plan["topic"]
        before = {src["id"] for src in registry["sources"]}
        changes: list[dict] = []
        new_ids, touched = merge_records(
            registry, records, changes, work=work, text_chars=args.max_chars
        )
        run_cost = sum(rec.get("cost") or 0.0 for rec in records)
        registry["cost_dollars"] = round(float(registry.get("cost_dollars") or 0.0) + run_cost, 6)
        write_json_atomic(work / "sources.json", registry)

    if adhoc:
        if effective_pairs:
            errors = [rec["error"] for rec in records if rec.get("error")]
            append_log(work, {
                "op": "search_adhoc",
                "subq": args.subq,
                "queries": [query for _qid, query in effective_pairs],
                "new_ids": new_ids,
                "matched_ids": touched,
                "fetched_id": None,
                "provider": "exa",
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
                "provider": "exa",
                "cost": round(float(record.get("cost") or 0.0), 6),
                "ok": bool(record.get("ok")),
                "error": record.get("error"),
            })

    if adhoc:
        qid = args.subq
        picked = [src for src in registry["sources"] if src["id"] in set(touched)]
        picked = rank_sources(picked)
        print(f"ad-hoc results for {qid}: {len(picked)} sources ({len(new_ids)} new)")
        for src in picked:
            day = format_date(src.get("published"))
            suffix = f" ({day})" if day else ""
            print(f"\n[{src['id']}] {src.get('title') or '(untitled)'}{suffix}")
            print(f"    {src.get('url')}")
            excerpt = " ".join(str(src.get("summary") or src.get("text") or "").split())
            if excerpt:
                print(f"    {excerpt[:400]}")
        written: list[str] = []
        if picked:
            with sources_lock(work):
                registry = load_registry(work / "sources.json", slug)
                written = [src["id"] for src in append_evidence(
                    work / "evidence" / f"{qid}.md", qid, question, picked, work, today
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
            render_evidence(qid, str(item.get("question") or ""), picked, work, today) + "\n",
        )
    ok = len(effective_pairs) - failures
    skipped_note = f", {len(skipped)} skipped duplicate(s)" if skipped else ""
    print(f"queries: {ok} ok, {failures} failed{skipped_note}")
    print(f"unique sources: {len(registry['sources'])} ({len(before)} -> {len(registry['sources'])})")
    print("sources per subquestion:")
    for qid in sorted(per_q, key=lambda q: int(q[1:])):
        print(f"  {qid}: {per_q[qid]}")
    print(f"run cost: ${run_cost:.4f} (total ${registry['cost_dollars']:.4f})")
    print(f"sources file: {work / 'sources.json'}")
    print(f"evidence dir: {work / 'evidence'}")
    return 1 if ok == 0 and effective_pairs else 0


def _fold_with_offsets(text: str) -> tuple[str, list[int]]:
    """Fold *text* and map each folded character back to its original index."""
    out: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        folded = "".join(
            part for part in unicodedata.normalize("NFKD", char.lower())
            if not unicodedata.combining(part)
        )
        for part in folded:
            out.append(part)
            offsets.append(index)
    offsets.append(len(text))
    return "".join(out), offsets


def grep_windows(text: str, terms: list[str], window: int = GREP_WINDOW,
                 limit: int = GREP_MAX_WINDOWS) -> list[tuple[int, int, str]]:
    """Case/accent-insensitive match windows (±window chars), merged when overlapping."""
    folded, offsets = _fold_with_offsets(text)
    spans: list[tuple[int, int]] = []
    for term in terms:
        needle = fold(term)
        if not needle:
            continue
        start = folded.find(needle)
        while start != -1:
            end = start + len(needle)
            spans.append((offsets[start], offsets[end - 1] + 1))
            start = folded.find(needle, start + len(needle))
    if not spans:
        return []
    spans.sort()
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        begin = max(0, start - window)
        finish = min(len(text), end + window)
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    return [(begin, finish, text[begin:finish]) for begin, finish in merged[:limit]]


def cmd_fetch(args: argparse.Namespace) -> int:
    work = require_work(args.work)
    slug = slug_from_work(work)
    registry = load_registry(work / "sources.json", slug)
    subq = (args.subq or "").strip()
    registered: dict | None = None
    registered_new = False
    if args.url:
        if not subq:
            die("error: --url requires --subq (the subquestion the page answers)", 2)
        if not QID_RE.match(subq):
            die(f"error: --subq must match ^q[0-9]+$ (got {args.subq!r})", 2)
        registered, registered_new = register_url(work, slug, args.url, subq)
        sid = registered["id"]
    else:
        sid = args.id.strip()
        if not re.match(r"^S[0-9]+$", sid):
            die(f"error: --id must look like S12 (got {sid!r})", 2)
    registry = load_registry(work / "sources.json", slug)
    src = source_by_id(registry, sid)
    if src is None:
        die(f"error: unknown source id {sid} in {work / 'sources.json'}", 1)
    max_chars = max(500, int(args.max_chars))
    page_text = read_page(work, sid)
    cost = 0.0
    if page_text:
        note(f"using cached page for {sid}: {page_path(work, sid)}")
        cached = True
        append_log(work, {
            "op": "fetch", "subq": subq or None, "queries": [],
            "new_ids": [sid] if registered_new else [], "matched_ids": [sid],
            "fetched_id": sid, "provider": "exa", "cost": 0.0,
            "ok": True, "error": None, "cached": True,
        })
    else:
        cached = False
        api_key = exa_key_required()
        note(f"fetching {sid}: {src['url']}")
        try:
            page, cost = with_retries(
                lambda: exa_contents(src["url"], PAGE_MAX_CHARS, api_key), 3,
                f"Exa contents {sid}",
            )
        except HttpError as exc:
            append_log(work, {
                "op": "fetch", "subq": subq or None, "queries": [], "new_ids": [],
                "matched_ids": [], "fetched_id": sid, "provider": "exa",
                "cost": None, "ok": False, "error": str(exc),
            })
            raise
        page_text = str(page.get("text") or "")
        store_page(work, sid, page_text)
        with sources_lock(work):
            registry = load_registry(work / "sources.json", slug)
            stored = source_by_id(registry, sid)
            if stored is not None:
                if page_text:
                    stored["text"] = page_text[:TEXT_MAX_CHARS]
                if page.get("title") and not stored.get("title"):
                    stored["title"] = page["title"]
                registry["cost_dollars"] = round(
                    float(registry.get("cost_dollars") or 0.0) + cost, 6
                )
            registry["updated"] = utcnow()
            write_json_atomic(work / "sources.json", registry)
        src = source_by_id(registry, sid) or src
        append_log(work, {
            "op": "fetch", "subq": subq or None, "queries": [],
            "new_ids": [sid] if registered_new else [], "matched_ids": [sid],
            "fetched_id": sid, "provider": "exa", "cost": round(cost, 6),
            "ok": True, "error": None,
        })
    if registered_new and subq and page_text:
        with sources_lock(work):
            registry = load_registry(work / "sources.json", slug)
            fresh = source_by_id(registry, sid)
            if fresh is not None:
                append_evidence(work / "evidence" / f"{subq}.md", subq,
                                _subquestion_text(work, subq), [fresh], work)

    print(f"[{sid}] {src.get('title') or ''} — {src['url']}")
    print(f"page: {len(page_text)} chars · {'cached' if cached else 'fetched'}")
    if not page_text:
        print("(no text returned by Exa contents)")
        return 0
    terms = [term for term in (args.grep or []) if str(term).strip()]
    if not terms:
        print(page_text[:max_chars])
        return 0
    windows = grep_windows(page_text, terms)
    if not windows:
        print(f"no matches for: {', '.join(terms)}")
        return 1
    for index, (begin, end, text) in enumerate(windows, 1):
        print(f"\n--- match {index} @{begin}..{end} ---")
        print(text)
    print(f"\n{len(windows)} window(s) for: {', '.join(terms)}")
    return 0


def _subquestion_text(work: Path, qid: str) -> str:
    plan_path = work / "plan.json"
    if not plan_path.exists():
        return ""
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    for item in plan.get("subquestions") or []:
        if isinstance(item, dict) and item.get("id") == qid:
            return str(item.get("question") or "")
    return ""


def recent_matches(work: Path, sources: list[dict], terms: list[str]) -> list[dict]:
    """Sources whose title, summary or cached page contain every term."""
    folded_terms = [folded for folded in (fold(term) for term in terms) if folded]
    if not folded_terms:
        return []
    matches: list[dict] = []
    for src in sources:
        haystack = fold(" ".join([
            str(src.get("title") or ""),
            str(src.get("summary") or ""),
            read_page(work, src.get("id", "")) or str(src.get("text") or ""),
        ]))
        if all(term in haystack for term in folded_terms):
            matches.append(src)
    return matches


def _recent_sort_date(src: dict, today: "date") -> str:
    when = parse_iso_date(src.get("published"))
    if when is not None:
        return when.isoformat()
    hint = year_hint(src.get("url", ""), src.get("title", ""), today.year)
    return f"{hint}-01-01" if hint else ""


def sort_recent(sources: list[dict], today: "date | None" = None) -> list[dict]:
    """Newest first by date or year hint; undated without a hint last."""
    today = today or today_date()
    ordered = sorted(sources, key=lambda src: _sid_num(src.get("id")))
    return sorted(
        ordered,
        key=lambda src: (_recent_sort_date(src, today), _rank_value(src, today)[1]),
        reverse=True,
    )


def recent_line(src: dict, today: "date | None" = None) -> str:
    today = today or today_date()
    day = format_date(src.get("published"))
    if day:
        when = day
    else:
        hint = year_hint(src.get("url", ""), src.get("title", ""), today.year)
        when = f"year hint {hint}" if hint else "undated"
    authority = src.get("authority")
    if not isinstance(authority, int) or isinstance(authority, bool):
        authority_text = "authority unknown"
    else:
        authority_text = f"authority {authority}"
    return (
        f"[{src['id']}] {when} · {authority_text} · "
        f"{src.get('title') or '(untitled)'} — {src.get('url')}"
    )


def cmd_recent(args: argparse.Namespace) -> int:
    work = require_work(args.work)
    terms = [term.strip() for term in args.grep if term.strip()]
    if not terms:
        die("error: --grep needs at least one non-empty term", 2)
    registry = load_registry(work / "sources.json", slug_from_work(work))
    today = today_date()
    matches = sort_recent(recent_matches(work, registry["sources"], terms), today)
    limit = max(1, int(args.limit))
    print(f"matched: {len(matches)} source(s) for: {', '.join(terms)} (newest first)")
    for src in matches[:limit]:
        print(recent_line(src, today))
    if len(matches) > limit:
        print(f"... {len(matches) - limit} more (raise --limit)")
    if not matches:
        print("no registered source matches all terms; run search --subq <QID> --query \"...\" "
              "to find newer sources")
    return 0


def detect_language(text: str) -> str:
    """Best-effort stopword ratio; defaults to English for short/ambiguous text."""
    words = re.findall(r"[a-záéíóúüñ]+", text.lower())
    spanish = sum(1 for word in words if word in SPANISH_HINTS)
    english = sum(1 for word in words if word in ENGLISH_HINTS)
    return "es" if spanish > english else "en"


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", _strip_references(text)))


def _normalize_heading(text: str) -> str:
    folded = fold(text)
    return re.sub(r"\s+", " ", folded).strip()


REQUIRED_SECTIONS = (
    ("Resumen/Summary", ("resumen", "summary")),
    ("Implicaciones/Implications", ("implicaciones", "implications")),
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


def _dates_in_line(line: str) -> list[tuple[date, int, int]]:
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


CLOSED_RE = re.compile(
    r"\b(cerr[óo]|cerrad[oa]s?|venci[óo]|vencid[oa]s?|ya pas[óo]|closed|expired|has passed|was due)\b",
    re.IGNORECASE,
)


def _date_warnings(text: str) -> list[str]:
    from datetime import datetime as _datetime
    today = _datetime.now().date()
    warnings = []
    for lineno, line in enumerate(text.splitlines(), 1):
        keywords = list(DEADLINE_RE.finditer(line))
        if not keywords or CLOSED_RE.search(line):
            continue
        dates = _dates_in_line(line)
        for value, start, _end in dates:
            if value < today and any(abs(keyword.start() - start) <= 80 for keyword in keywords):
                warnings.append(
                    f"warning: line {lineno} mentions a deadline that has passed "
                    f"({value.isoformat()}); informational only: {line.strip()[:180]}"
                )
                break
    return warnings


NUMBER_RE = re.compile(r"\d[\d.,]*\d|\d")
COMPUTED_RE = re.compile(r"≈|c[áa]lculo|calculat|estimad|estimat|supuesto|assum", re.IGNORECASE)
SUPPORT_SKIP_SECTIONS = ("supuestos", "assumptions", "metodologia", "methodology")
SUPPORT_WARN_LIMIT = 25
WEAK_SECTIONS = ("resumen", "summary", "implicaciones", "implications")
WEAK_WARN_LIMIT = 10


def _number_keys(text: str) -> set[str]:
    """Digit-only keys for numbers with >= 3 significant digits (5.699, 5,699 and 5699 match)."""
    keys = set()
    for match in NUMBER_RE.finditer(text):
        key = re.sub(r"[.,]", "", match.group(0)).lstrip("0")
        if len(key) >= 3:
            keys.add(key)
    return keys


def _citation_units(text: str):
    """Yield (lineno, section, unit_text, cited_ids) for each sentence or table cell with a citation."""
    section = ""
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            section = _normalize_heading(stripped.lstrip("#"))
            continue
        if stripped.startswith("|"):
            units = [cell for cell in stripped.strip("|").split("|")]
        else:
            units = re.split(r"(?<=[.!?])\s+", stripped)
        for unit in units:
            ids = []
            for match in CITE_RE.finditer(unit):
                ids.extend(part.strip() for part in match.group(1).split(","))
            if ids:
                yield lineno, section, unit, ids


def _citation_support_warnings(text: str, by_id: dict) -> list[str]:
    """Numbers in a cited sentence/cell must appear in at least one of its cited sources."""
    cache: dict[str, set[str]] = {}

    def source_keys(sid: str) -> set[str]:
        if sid not in cache:
            src = by_id.get(sid) or {}
            blob = " ".join([str(src.get("title") or ""), str(src.get("text") or ""),
                             str(src.get("summary") or "")])
            cache[sid] = _number_keys(blob)
        return cache[sid]

    problems = []
    for lineno, section, unit, ids in _citation_units(text):
        if any(section.startswith(name) for name in SUPPORT_SKIP_SECTIONS):
            continue
        if COMPUTED_RE.search(unit):
            continue
        claim = CITE_RE.sub(" ", unit)
        supported = set().union(*(source_keys(sid) for sid in ids if sid in by_id))
        missing = sorted(_number_keys(claim) - supported)
        if missing:
            problems.append((lineno, missing, ids, claim.strip()))
    if not problems:
        return []
    warnings = [
        f"warning: {len(problems)} cited sentence(s)/cell(s) contain numbers not found in the "
        "sources they cite (wrong citation, stale/misread value, or an unmarked calculation):"
    ]
    for lineno, missing, ids, claim in problems[:SUPPORT_WARN_LIMIT]:
        warnings.append(
            f"  line {lineno}: {', '.join(missing)} not in {', '.join(ids)}: {claim[:140]}"
        )
    if len(problems) > SUPPORT_WARN_LIMIT:
        warnings.append(f"  ... and {len(problems) - SUPPORT_WARN_LIMIT} more")
    return warnings


def _bare_source_ids(text: str, by_id: dict) -> list[tuple[str, int]]:
    """Existing S-ids written outside a citation bracket, with their line numbers."""
    found: list[tuple[str, int]] = []
    # Leaked ids look like "S476, S450" or a table cell holding only ids; a single id-like
    # token in prose ("Galaxy S23") is left alone.
    for lineno, line in enumerate(text.splitlines(), 1):
        cleaned = CITE_RE.sub(" ", line)
        spans = [m.group(0) for m in BARE_ID_LIST_RE.finditer(cleaned)]
        if cleaned.lstrip().startswith("|"):
            spans += [cell for cell in cleaned.split("|") if BARE_ID_CELL_RE.match(cell)]
        for span in spans:
            for sid in BARE_ID_RE.findall(span):
                if sid in by_id:
                    found.append((sid, lineno))
    return found


def _weak_source_warnings(text: str, by_id: dict) -> list[str]:
    """Summary/What-to-do sentences whose only citations are weak sources."""
    problems = []
    for lineno, section, unit, ids in _citation_units(text):
        if not any(section.startswith(name) for name in WEAK_SECTIONS):
            continue
        if any(not source_quality_tags(by_id.get(sid) or {}) for sid in ids):
            continue
        problems.append((lineno, ids, CITE_RE.sub(" ", unit).strip()))
    if not problems:
        return []
    warnings = [
        f"warning: {len(problems)} Summary/Implications sentence(s)/cell(s) cite only "
        "social/forum or low-authority sources:"
    ]
    for lineno, ids, claim in problems[:WEAK_WARN_LIMIT]:
        warnings.append(f"  line {lineno}: {', '.join(ids)}: {claim[:140]}")
    if len(problems) > WEAK_WARN_LIMIT:
        warnings.append(f"  ... and {len(problems) - WEAK_WARN_LIMIT} more")
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

    bare = _bare_source_ids(text, by_id)
    if bare:
        by_line: dict[int, list[str]] = {}
        for sid, lineno in bare:
            by_line.setdefault(lineno, []).append(sid)
        note("error: the draft mentions source ids outside a citation bracket "
             "(they are not linked or counted):")
        for lineno in sorted(by_line):
            ids = sorted(set(by_line[lineno]), key=_sid_num)
            note(f"  line {lineno}: {', '.join(ids)}")
        note("Cite each as [S#] (for example [S476]) or drop the text. "
             "Sources appear only as citation brackets.")
        append_log(work, {
            "op": "finalize", "subq": None, "queries": [], "new_ids": [], "matched_ids": [],
            "fetched_id": None, "cost": None, "ok": False,
            "error": "bare source ids: " + ", ".join(sorted({sid for sid, _ in bare}, key=_sid_num)),
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
    if word_count > 9000:
        warnings.append(
            f"warning: report is {word_count} words (no fixed target; a typical report is "
            "4,000-7,000 words; warning above 9,000)"
        )
    warnings.extend(_date_warnings(text))
    warnings.extend(_citation_support_warnings(text, by_id))
    warnings.extend(_weak_source_warnings(text, by_id))
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
AS_OF_RE = re.compile(r"^(?:unknown|\d{4}-\d{2}(?:-\d{2})?)$")


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
        if "as_of" in item:
            as_of = item.get("as_of")
            if not isinstance(as_of, str) or not AS_OF_RE.match(as_of.strip()):
                errors.append(f"{where}.as_of must be YYYY-MM, YYYY-MM-DD or unknown")
        if "unit" in item:
            unit = item.get("unit")
            if not isinstance(unit, str) or not unit.strip():
                errors.append(f"{where}.unit must be a non-empty string")
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
    post_actions: list[str] = []
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            subquestions = [item for item in plan.get("subquestions") or [] if isinstance(item, dict)]
            post_actions = [item for item in plan.get("post_actions") or [] if isinstance(item, str) and item.strip()]
            slug = str(plan.get("slug") or slug)
            print(f"plan:      [x] {len(subquestions)} subquestions — {plan.get('topic') or '(no topic)'}")
            if post_actions:
                print(f"post_actions: {len(post_actions)}")
                for action in post_actions:
                    print(f"  - {action}")
        except json.JSONDecodeError:
            print("plan:      [!] plan.json is not valid JSON")
    else:
        print("plan:      [ ] missing (write plan.json)")

    registry = load_registry(work / "sources.json", slug)
    count = len(registry["sources"])
    print(f"sources:   [{'x' if count else ' '}] {count} unique (cost ${float(registry.get('cost_dollars') or 0):.4f})")
    pages = len(list((work / PAGES_DIR).glob("S*.md"))) if (work / PAGES_DIR).is_dir() else 0
    print(f"pages:     [{'x' if pages else ' '}] {pages} cached full page(s)")

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
    elif post_actions:
        print("next: run the plan.json post_actions, then deliver the summary + MEDIA line")
    else:
        print("next: deliver the summary + MEDIA line")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research.py",
        description="Deterministic deep-research pipeline helper (Exa Search & Contents + local files).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical flow:\n"
            "  research.py init --slug 2026-11-15-topic\n"
            "  research.py search --work ~/research/.work/2026-11-15-topic\n"
            "  research.py search --work DIR --subq q3 --query 'follow-up query'\n"
            "  research.py fetch --work DIR --id S12 --grep 'tariff'\n"
            "  research.py recent --work DIR --grep tariff kWh\n"
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
                          help=f"page text kept per source in sources.json (default ${MAX_CHARS_ENV} or {DEFAULT_MAX_CHARS})")
    p_search.add_argument("--today", default=None,
                          help="YYYY-MM-DD used for the recency ranking (default: system date)")
    p_search.add_argument("--dry-run", action="store_true", help="validate plan.json and print the query count; no API calls")
    p_search.set_defaults(func=cmd_search)

    p_fetch = sub.add_parser("fetch", help="print a cached page, or fetch one; --grep finds values")
    p_fetch.add_argument("--work", required=True, help="work dir (from init)")
    target = p_fetch.add_mutually_exclusive_group(required=True)
    target.add_argument("--id", help="source id, e.g. S12")
    target.add_argument("--url", help="URL not in sources.json yet; registered as a new S-id")
    p_fetch.add_argument("--subq", help="subquestion id; required with --url")
    p_fetch.add_argument("--max-chars", type=int, default=20000,
                         help="characters printed without --grep (default 20000)")
    p_fetch.add_argument("--grep", action="append", metavar="TERM",
                         help="print ±400-char windows around each match (repeatable)")
    p_fetch.set_defaults(func=cmd_fetch)

    p_recent = sub.add_parser("recent", help="list matching sources newest first (no network)")
    p_recent.add_argument("--work", required=True, help="work dir (from init)")
    p_recent.add_argument("--grep", nargs="+", required=True, metavar="TERM",
                          help="terms that must all appear in the title, summary or cached page")
    p_recent.add_argument("--limit", type=int, default=15, help="rows to print (default 15)")
    p_recent.set_defaults(func=cmd_recent)

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
