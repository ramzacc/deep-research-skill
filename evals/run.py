#!/usr/bin/env python3
"""Run the deep-research skill end to end inside a real, isolated Hermes Agent.

Matrix: topics x skill configs x repetitions. Each run gets its own HOME and
HERMES_HOME, a variant of the skill, an Exa key and the OpenCode Go endpoint.
Stats are collected from the work dir, `log.jsonl`, `final.json`, the
stdout/stderr transcript and Hermes' per-home `state.db` usage ledger. The
deliverable is the markdown report.

Secrets come from `evals/.env` (gitignored, chmod 600) and are only written to
`<run>/hermes-home/.env` (chmod 600). They are never printed or copied into
tracked files, stats or transcripts.

Usage
-----
    python3 evals/run.py --batch smoke --topics smoke --configs B --reps 1
    python3 evals/run.py --batch full --topics posgrado,gpu-peru --configs A,B,C,D --reps 2 --parallel 3
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "evals"
RUN_DIR = EVAL_DIR / ".run"
RESULTS_DIR = EVAL_DIR / "results"
ENV_FILE = EVAL_DIR / ".env"
TOPICS_TRACKED = EVAL_DIR / "topics.json"
TOPICS_LOCAL = EVAL_DIR / "topics.local.json"
VARIANTS_DIR = RUN_DIR / "skills"
HERMES_VENV = RUN_DIR / "hermes-venv"
HERMES_BIN = HERMES_VENV / "bin" / "hermes"

MODEL = "deepseek-v4.1-flash"
PROVIDER = "opencode-go"
REASONING = "medium"
TOOLSETS = "terminal,file,web,delegation,skills"
SKILL_NAME = "deep-research"

# deepseek-v4.1-flash list prices, USD per million tokens.
PRICE_INPUT = 0.15
PRICE_OUTPUT = 0.60
PRICE_CACHED = 0.003

SECRET_KEYS = ("EXA_API_KEY", "OPENCODE_GO_API_KEY")
REQUIRED_KEYS = SECRET_KEYS + ("OPENCODE_GO_BASE_URL",)
RUN_EXA_KEY_VAR = "DEEP_RESEARCH_EXA_KEY"

TOPIC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

SPANISH_STOPWORDS = frozenset("""
de la que el en los se del las un por con no una su para es al lo como mas pero sus le ya o
este si porque esta entre cuando muy sin sobre tambien me hasta hay donde quien desde todo nos
durante todos uno les ni contra otros ese eso ante ellos e esto mi antes algunos que unos yo otro
otras otra el tanto esa estos mucho quienes nada muchos cual poco ella estar estas algunas algo
nosotros mi mis tu te ti tus ellas nosotras vosotros vosotras os mio mia mios mias tuyo tuya
tuyos tuyas suyo suya suyos suyas nuestro nuestra nuestros nuestras vuestro vuestra vuestros
vuestras esos esas estoy estas esta estamos estais estan este estes estemos esteis esten estare
estaras estara estaremos estareis estaran estaria estarias estariamos estariais estarian estaba
estabas estabamos estabais estaban estuve estuviste estuvo estuvimos estuvisteis estuvieron
""".split())

ENGLISH_STOPWORDS = frozenset("""
the of and to in a is that for it as was with be by on not he i this are or his from at which
but have an they you were her she there one all we their has had what would will can been when
who more no if out so said up its about into than them only other some could time these two
may then do first any my now such like our over man me even most made after also did many off
before must through back years where much your way well down should because each just those
people mr how too little state good very make world still own see men work long get here between
both life being under never day same another know while last might us great old year come since
against go came right used take three
""".split())


# --------------------------------------------------------------------------
# env / topics
# --------------------------------------------------------------------------

def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_secrets() -> dict[str, str]:
    if not ENV_FILE.is_file():
        raise SystemExit(f"missing {ENV_FILE.relative_to(ROOT)} (chmod 600, gitignored)")
    mode = ENV_FILE.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(f"{ENV_FILE.relative_to(ROOT)} is mode {mode:o}; run: chmod 600 {ENV_FILE}")
    values = load_env_file(ENV_FILE)
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise SystemExit(f"missing keys in {ENV_FILE.relative_to(ROOT)}: {', '.join(missing)}")
    return values


def load_topics() -> dict[str, dict]:
    """Tracked topics first (generic smoke), then local ones override by id.

    `evals/topics.local.json` is gitignored and holds the private real topics;
    `evals/topics.json` keeps the generic smoke topic so the harness is usable
    on a fresh checkout and while the local file exists.
    """
    topics: dict[str, dict] = {}
    for path in (TOPICS_TRACKED, TOPICS_LOCAL):
        if not path.is_file():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise SystemExit(f"{path} must contain a JSON list of {{id, prompt}}")
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("prompt"), str):
                raise SystemExit(f"{path}: every topic needs string id and prompt")
            topic_id = item["id"]
            if not TOPIC_ID_RE.match(topic_id):
                raise SystemExit(f"{path}: invalid topic id {topic_id!r}")
            topics[topic_id] = {"id": topic_id, "prompt": item["prompt"]}
    if not topics:
        raise SystemExit("no topics: expected evals/topics.json or evals/topics.local.json")
    return topics


def redact(text: str, secrets: dict[str, str]) -> str:
    for key in SECRET_KEYS:
        value = secrets.get(key)
        if value:
            text = text.replace(value, f"<{key}>")
    return text


# --------------------------------------------------------------------------
# Hermes environment
# --------------------------------------------------------------------------

def ensure_hermes() -> None:
    if not HERMES_BIN.is_file():
        raise SystemExit(
            f"missing {HERMES_BIN.relative_to(ROOT)}.\n"
            "Install the isolated copy first (main branch, Python 3.14, locked deps):\n"
            f"  ls {RUN_DIR / 'hermes-src'} || git clone --depth 1 https://github.com/NousResearch/hermes-agent {RUN_DIR / 'hermes-src'}\n"
            f"  UV_PROJECT_ENVIRONMENT=$PWD/{HERMES_VENV.relative_to(ROOT)} uv sync --project {RUN_DIR / 'hermes-src'} --no-dev"
        )


def hermes_version() -> str:
    """Record the pinned checkout, not `hermes --version`: that command bootstraps a
    profile home before argument handling and would touch the real ~/.hermes."""
    proc = subprocess.run(["git", "-C", str(RUN_DIR / "hermes-src"), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True)
    commit = proc.stdout.strip()
    return f"git.{commit}" if proc.returncode == 0 and commit else "unknown"


def write_hermes_home(home: Path, skill_dir: Path, secrets: dict[str, str], num_results: int,
                      base_url: str) -> None:
    (home / "skills").mkdir(parents=True, exist_ok=True)
    dest = home / "skills" / SKILL_NAME
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(skill_dir, dest)
    env_lines = [
        f"{RUN_EXA_KEY_VAR}={secrets['EXA_API_KEY']}",
        f"OPENCODE_GO_API_KEY={secrets['OPENCODE_GO_API_KEY']}",
        f"OPENCODE_GO_BASE_URL={secrets['OPENCODE_GO_BASE_URL']}",
    ]
    env_path = home / ".env"
    env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    config = f"""\
_config_version: 53
model:
  default: {MODEL}
  provider: {PROVIDER}
  base_url: {base_url}
agent:
  reasoning_effort: {REASONING}
delegation:
  max_concurrent_children: 10
  oneshot_max_children: 10
  subagent_auto_approve: true
  reasoning_effort: {REASONING}
skills:
  template_vars: true
"""
    (home / "config.yaml").write_text(config, encoding="utf-8")


def stop_gateway(home: Path) -> None:
    """Stop the per-profile gateway that `hermes chat` auto-starts.

    The gateway is detached from our process group and idles for 10 minutes by
    default, holding state.db. Each eval run owns its HERMES_HOME, so the pid
    in <home>/gateway.pid plus its HERMES_HOME env identify it safely.
    """
    expected = str(home).encode()
    pids: list[int] = []
    pid_path = home / "gateway.pid"
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        try:
            pids.append(int(json.loads(raw)["pid"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pids.append(int(raw))
    except (OSError, ValueError):
        pass
    # The pid file is absent for some auto-started gateways; find only processes
    # whose own HERMES_HOME is this run's home (never a broad process sweep).
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ")
            environ = (proc / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"gateway" not in cmdline:
            continue
        if not any(part.startswith(b"HERMES_HOME=") and part.split(b"=", 1)[1] == expected for part in environ):
            continue
        try:
            pid = int(proc.name)
        except ValueError:
            continue
        if pid not in pids:
            pids.append(pid)
    for pid in pids:
        if pid <= 1:
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if "gateway" not in cmdline:
            continue
        if not any(part.startswith(b"HERMES_HOME=") and part.split(b"=", 1)[1] == expected for part in environ):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        for _ in range(100):
            time.sleep(0.05)
            if not Path(f"/proc/{pid}").exists():
                break
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def real_hermes_home_snapshot() -> dict:
    path = Path("~/.hermes").expanduser()
    try:
        stat = path.stat()
        return {"exists": True, "mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    except FileNotFoundError:
        return {"exists": False, "mtime_ns": 0, "size": 0}


# --------------------------------------------------------------------------
# run execution
# --------------------------------------------------------------------------

def run_id(topic: str, config: str, rep: int) -> str:
    return f"{topic}__{config}__r{rep}"


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-")[:120]


def run_one(batch: str, topic: dict, config: str, rep: int, args: argparse.Namespace,
            secrets: dict[str, str], variant_skill: Path, version: str, num_results: int,
            max_chars: int, base_url: str) -> dict:
    rid = run_id(topic["id"], config, rep)
    out = RESULTS_DIR / batch / "runs" / rid
    stats_path = out / "stats.json"
    if stats_path.exists() and not args.force:
        return {"run_id": rid, "skipped": True, "stats": json.loads(stats_path.read_text(encoding="utf-8"))}

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    child_home = out / "home"
    child_home.mkdir()
    hermes_home = RUN_DIR / f"hermes-home-{safe_name(batch)}-{safe_name(rid)}"
    stop_gateway(hermes_home)
    if hermes_home.exists():
        shutil.rmtree(hermes_home)
    hermes_home.mkdir(parents=True)
    write_hermes_home(hermes_home, variant_skill, secrets, num_results, base_url)

    env = os.environ.copy()
    env.update({
        "HOME": str(child_home),
        "HERMES_HOME": str(hermes_home),
        "DEEP_RESEARCH_NUM_RESULTS": str(num_results),
        "DEEP_RESEARCH_MAX_CHARS": str(max_chars),
    })
    for key in REQUIRED_KEYS:
        env[key] = secrets[key]
    # The dedicated key reaches the terminal only through HERMES_HOME/.env and
    # the skill's required_environment_variables declaration; EXA_API_KEY must
    # not leak in from the caller.
    env.pop("EXA_API_KEY", None)
    env.pop(RUN_EXA_KEY_VAR, None)
    env.pop("HERMES_SINGLE_QUERY_SESSION", None)

    cmd = [
        str(HERMES_BIN), "chat", "--oneshot", "-Q",
        "-q", topic["prompt"],
        "-m", MODEL, "--provider", PROVIDER, "--reasoning", REASONING,
        "-t", TOOLSETS, "-s", SKILL_NAME, "--yolo",
    ]
    stdout_path = out / "stdout.txt"
    stderr_path = out / "stderr.txt"
    before_home = real_hermes_home_snapshot()
    started = time.time()
    timed_out = False
    exit_code = None
    with open(stdout_path, "wb") as stdout_f, open(stderr_path, "wb") as stderr_f:
        proc = subprocess.Popen(cmd, cwd=out, env=env, stdout=stdout_f, stderr=stderr_f,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            exit_code = proc.wait(timeout=args.timeout_min * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            exit_code = proc.wait(timeout=30)
    wall = time.time() - started
    stop_gateway(hermes_home)
    after_home = real_hermes_home_snapshot()

    stats = collect_stats(
        batch=batch, topic=topic, config=config, rep=rep, out=out,
        child_home=child_home, hermes_home=hermes_home, stdout_path=stdout_path,
        stderr_path=stderr_path, wall=wall, exit_code=exit_code, timed_out=timed_out,
        before_home=before_home, after_home=after_home, secrets=secrets,
        variant_skill=variant_skill, version=version, invocation="-s deep-research",
        num_results=num_results, max_chars=max_chars, base_url=base_url,
    )
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "stats.md").write_text(render_stats_md(stats), encoding="utf-8")
    return {"run_id": rid, "skipped": False, "stats": stats}


# --------------------------------------------------------------------------
# stats collection
# --------------------------------------------------------------------------

BOX_CHARS = ("│", "┌", "└", "├", "─", "╭", "╰", "╮", "╯")


def read_reply(path: Path) -> str:
    """Final response from stdout, minus the gateway's boxed tool previews."""
    if not path.is_file():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = [line for line in lines if not line.lstrip().startswith(BOX_CHARS)]
    return "\n".join(kept).strip()


def pick_work_dir(child_home: Path) -> Path | None:
    root = child_home / "research" / ".work"
    if not root.is_dir():
        return None
    def score(path: Path) -> tuple:
        sources = 0
        if (path / "sources.json").is_file():
            try:
                sources = len(json.loads((path / "sources.json").read_text(encoding="utf-8")).get("sources") or [])
            except (json.JSONDecodeError, OSError):
                sources = 0
        findings = len(list((path / "findings").glob("*.json"))) if (path / "findings").is_dir() else 0
        return ((path / "plan.json").is_file(), findings, sources, path.stat().st_mtime)
    dirs = [path for path in root.iterdir() if path.is_dir()]
    return max(dirs, key=score) if dirs else None


def one_line(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_log(work: Path | None) -> list[dict]:
    if work is None:
        return []
    path = work / "log.jsonl"
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def run_dir_exa_cost(run_dir: Path) -> float:
    """Exa spend for one run: stats.json when finished, live sources.json while running."""
    stats = read_json(run_dir / "stats.json", {})
    if stats:
        try:
            return float(stats.get("exa_cost_usd") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    total = 0.0
    work_root = run_dir / "home" / "research" / ".work"
    if work_root.is_dir():
        for sources in work_root.glob("*/sources.json"):
            registry = read_json(sources, {})
            try:
                total += float(registry.get("cost_dollars") or 0.0)
            except (TypeError, ValueError):
                continue
    return total


def batch_exa_spend(batch: str) -> float:
    """Exa spend of every run dir in the batch, finished or in flight."""
    runs_dir = RESULTS_DIR / batch / "runs"
    if not runs_dir.is_dir():
        return 0.0
    return sum(run_dir_exa_cost(path) for path in runs_dir.iterdir() if path.is_dir())


def ordered_runs(topics: list[dict], configs: list[str], reps: int, batch: str) -> list[tuple]:
    """rep 1 first, topic by topic, same shuffled config order in every rep."""
    orders: dict[str, list[str]] = {}
    for topic in topics:
        order = list(configs)
        random.Random(f"{batch}:{topic['id']}").shuffle(order)
        orders[topic["id"]] = order
    return [
        (topic, config, rep)
        for rep in range(1, reps + 1)
        for topic in topics
        for config in orders[topic["id"]]
    ]


def db_metrics(hermes_home: Path) -> dict:
    """Token usage, session counts and tool calls from the profile's state.db."""
    empty = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
             "api_calls": 0, "sessions": 0, "subagent_sessions": 0, "usage_rows": 0,
             "tool_calls": {}}
    db = hermes_home / "state.db"
    if not db.is_file():
        return empty
    con = None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        row = con.execute("""
            SELECT COALESCE(SUM(input_tokens),0) AS input,
                   COALESCE(SUM(output_tokens),0) AS output,
                   COALESCE(SUM(cache_read_tokens),0) AS cache_read,
                   COALESCE(SUM(cache_write_tokens),0) AS cache_write,
                   COALESCE(SUM(api_call_count),0) AS api_calls,
                   COUNT(*) AS usage_rows
            FROM session_model_usage
        """).fetchone()
        sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        subagents = con.execute("""
            SELECT COUNT(*) FROM sessions
            WHERE (parent_session_id IS NOT NULL AND parent_session_id != '')
               OR COALESCE(model_config, '') LIKE '%_delegate_from%'
        """).fetchone()[0]
        tool_calls: dict[str, int] = {}
        for tool_row in con.execute(
            "SELECT tool_calls FROM messages WHERE tool_calls IS NOT NULL AND tool_calls != ''"
        ):
            try:
                calls = json.loads(tool_row["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(calls, dict):
                calls = [calls]
            if not isinstance(calls, list):
                continue
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else call
                name = str(function.get("name") or "").strip()
                if name:
                    tool_calls[name] = tool_calls.get(name, 0) + 1
        if not tool_calls:
            for tool_row in con.execute(
                "SELECT tool_name, COUNT(*) AS n FROM messages "
                "WHERE tool_name IS NOT NULL AND tool_name != '' GROUP BY tool_name"
            ):
                tool_calls[str(tool_row["tool_name"])] = int(tool_row["n"])
        return {
            "input": int(row["input"]), "output": int(row["output"]),
            "cache_read": int(row["cache_read"]), "cache_write": int(row["cache_write"]),
            "api_calls": int(row["api_calls"]), "usage_rows": int(row["usage_rows"]),
            "sessions": int(sessions), "subagent_sessions": int(subagents),
            "tool_calls": tool_calls,
        }
    except sqlite3.Error:
        return empty
    finally:
        if con is not None:
            con.close()


def detect_language(text: str) -> str:
    words = re.findall(r"[a-záéíóúüñ]+", text.lower())
    if len(words) < 40:
        return "unknown"
    spanish = sum(1 for word in words if word in SPANISH_STOPWORDS)
    english = sum(1 for word in words if word in ENGLISH_STOPWORDS)
    if spanish + english == 0:
        return "unknown"
    if spanish >= english * 1.25:
        return "es"
    if english >= spanish * 1.25:
        return "en"
    return "mixed"


def thematic_sections(text: str) -> tuple[int, int]:
    headings = re.findall(r"^##\s+(.+?)\s*$", text, flags=re.MULTILINE)
    non_thematic = {
        "executive summary", "summary", "resumen",
        "disagreements & uncertainties", "disagreements and uncertainties",
        "contradicciones e incertidumbres", "not verified", "no verificado",
        "what to do", "qué hacer", "que hacer", "assumptions", "supuestos",
        "methodology", "metodología", "references", "referencias", "sources", "fuentes",
    }
    thematic = [h for h in headings if h.strip().lower() not in non_thematic]
    return len(headings), len(thematic)


def media_line(reply: str) -> tuple[str | None, bool]:
    for line in reversed(reply.splitlines()):
        stripped = line.strip().strip("`").strip()
        if stripped.startswith("MEDIA:"):
            target = stripped[len("MEDIA:"):].strip().strip("`").strip()
            if not target:
                return None, False
            path = Path(target).expanduser()
            return target, path.is_file()
    return None, False


CLARIFY_RE = re.compile(
    r"(could you|can you) (clarify|confirm|specify)|please (clarify|specify|confirm)|"
    r"what (exactly|specifically) (do you|would you)|which (topic|subject|scope|question)|"
    r"necesito (que )?(me )?(aclares|confirmes|especifiques)|podr[ií]as (aclarar|confirmar|especificar)",
    re.IGNORECASE,
)


def collect_stats(*, batch: str, topic: dict, config: str, rep: int, out: Path,
                  child_home: Path, hermes_home: Path, stdout_path: Path, stderr_path: Path,
                  wall: float, exit_code: int, timed_out: bool, before_home: dict,
                  after_home: dict, secrets: dict[str, str], variant_skill: Path,
                  version: str, invocation: str, num_results: int, max_chars: int,
                  base_url: str) -> dict:
    reply = redact(read_reply(stdout_path), secrets)
    work_original = pick_work_dir(child_home)
    work = out / "work"
    if work_original is not None:
        shutil.copytree(work_original, work, dirs_exist_ok=True)

    plan = read_json(work / "plan.json", {}) if work_original else {}
    registry = read_json(work / "sources.json", {}) if work_original else {}
    final_info = read_json(work / "final.json", {}) if work_original else {}
    log = read_log(work if work_original else None)

    asks = plan.get("asks") or []
    post_actions = plan.get("post_actions") or []
    covers = [
        cover for item in (plan.get("subquestions") or [])
        for cover in (item.get("covers") or [])
    ]
    covered_asks = {cover for cover in covers if isinstance(cover, int) and 0 <= cover < len(asks)}
    findings_total = 0
    findings_verified = 0
    findings_date_status: dict[str, int] = {}
    for finding_path in sorted((work / "findings").glob("*.json")) if (work / "findings").is_dir() else []:
        data = read_json(finding_path, {})
        for finding in data.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            findings_total += 1
            if finding.get("verified") is True:
                findings_verified += 1
            status = finding.get("date_status")
            if status:
                findings_date_status[str(status)] = findings_date_status.get(str(status), 0) + 1

    sources = registry.get("sources") or []
    by_id = {src.get("id"): src for src in sources}
    citation_map = final_info.get("citations") or {}
    if not citation_map:
        # fallback: derive order from the final report's linked citations
        report_guess = None
        md_files = [p for p in (child_home / "research").glob("*.md") if p.is_file()] if (child_home / "research").is_dir() else []
        if md_files:
            report_guess = max(md_files, key=lambda p: p.stat().st_mtime)
        if report_guess is not None:
            seen: list[str] = []
            for match in re.finditer(r"\[\[(\d+)\]\]\(([^)]+)\)", report_guess.read_text(encoding="utf-8", errors="replace")):
                url = match.group(2)
                for sid, src in by_id.items():
                    if src.get("url") == url and sid not in seen:
                        seen.append(sid)
            citation_map = {sid: index for index, sid in enumerate(seen, 1)}

    # find the final report, copy into <run>/report/
    report_md_original: Path | None = None
    if final_info.get("out"):
        report_md_original = Path(final_info["out"]).expanduser()
    if report_md_original is None or not report_md_original.is_file():
        research = child_home / "research"
        candidates = [p for p in research.glob("*.md") if p.is_file()] if research.is_dir() else []
        report_md_original = max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None
    report_dir = out / "report"
    report_dir.mkdir(exist_ok=True)
    report_md = None
    report_text = ""
    if report_md_original is not None and report_md_original.is_file():
        report_md = report_dir / report_md_original.name
        shutil.copy2(report_md_original, report_md)
        report_text = report_md.read_text(encoding="utf-8", errors="replace")

    (out / "reply.md").write_text(reply.rstrip() + "\n", encoding="utf-8")

    # counts
    n_batch = sum(len(rec.get("queries") or []) for rec in log if rec.get("op") == "search_batch")
    n_adhoc = sum(len(rec.get("queries") or []) for rec in log if rec.get("op") == "search_adhoc")
    fetched = [rec.get("fetched_id") for rec in log if rec.get("op") == "fetch" and rec.get("ok")]
    fetched = [sid for sid in fetched if sid]
    usage = db_metrics(hermes_home)
    tool_counts: dict[str, int] = usage["tool_calls"]
    delegate_calls = tool_counts.get("delegate_task", 0)

    cited = []
    for sid, ref in citation_map.items():
        src = by_id.get(sid) or {}
        cited.append({
            "ref": int(ref), "id": sid, "title": one_line(src.get("title")),
            "url": src.get("url") or "", "subquestions": src.get("subquestions") or [],
        })
    cited.sort(key=lambda item: item["ref"])

    # Hermes stores non-cached input in input_tokens and cached prompt tokens
    # separately in cache_read_tokens (their sum is the provider prompt_tokens).
    llm_cost = (
        usage["input"] * PRICE_INPUT
        + usage["cache_read"] * PRICE_CACHED
        + (usage["output"] + usage["cache_write"]) * PRICE_OUTPUT
    ) / 1_000_000
    exa_cost = float(registry.get("cost_dollars") or 0.0)

    coverage = final_info.get("coverage") or {}
    cited_paragraphs = int(coverage.get("cited_paragraphs") or 0)
    paragraphs = int(coverage.get("paragraphs") or 0)
    coverage_ratio = float(coverage.get("ratio") or 0.0)

    n_h2, n_thematic = thematic_sections(report_text) if report_text else (0, 0)
    words = len(re.findall(r"\S+", report_text)) if report_text else 0
    body = report_text.split("\n## References", 1)[0] if report_text else ""
    body_words = len(re.findall(r"\S+", body)) if body else 0
    media, media_ok = media_line(reply)

    errors: list[str] = []
    warnings: list[str] = []
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    for line in stderr_text.splitlines():
        clean = redact(line.strip(), secrets)
        if not clean:
            continue
        low = clean.lower()
        if "traceback" in low or re.search(r"\berror\b|\bfailed\b|http [45][0-9][0-9]", low):
            errors.append(clean)
        elif "warning" in low:
            warnings.append(clean)
    for record in log:
        if record.get("ok") is False and record.get("error"):
            errors.append(redact(f"{record.get('op')} failed: {record.get('error')}", secrets))
    errors = list(dict.fromkeys(errors))[:40]
    warnings = list(dict.fromkeys(warnings))[:40]

    # status
    if timed_out:
        status = "timeout"
    elif report_md is not None:
        status = "ok"
    elif CLARIFY_RE.search(reply) or (reply.strip().endswith("?") and work_original is None):
        status = "asked_clarification"
    elif exit_code != 0:
        status = "failed"
    else:
        status = "no_report"

    return {
        "batch": batch,
        "run_id": out.name,
        "topic": topic["id"],
        "config": config,
        "rep": rep,
        "status": status,
        "started_at": datetime.fromtimestamp(time.time() - wall, timezone.utc).isoformat(timespec="seconds"),
        "wall_seconds": round(wall, 1),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "hermes": {"version": version, "model": MODEL, "provider": PROVIDER,
                   "base_url": base_url, "reasoning": REASONING, "toolsets": TOOLSETS,
                   "skill_invocation": invocation, "num_results": num_results,
                   "max_chars": max_chars},
        "llm_tokens": {
            "input": usage["input"], "output": usage["output"],
            "cache_read": usage["cache_read"], "cache_write": usage["cache_write"],
            "input_total": usage["input"] + usage["cache_read"],
            "total": usage["input"] + usage["cache_read"] + usage["output"],
            "api_calls": usage["api_calls"], "usage_rows": usage["usage_rows"],
        },
        "llm_cost_usd": round(llm_cost, 6),
        "exa_cost_usd": round(exa_cost, 6),
        "total_cost_usd": round(llm_cost + exa_cost, 6),
        "pricing": {"input_per_m": PRICE_INPUT, "output_per_m": PRICE_OUTPUT, "cached_per_m": PRICE_CACHED},
        "n_subquestions": len(plan.get("subquestions") or []),
        "n_asks": len(asks),
        "n_covered_asks": len(covered_asks),
        "n_post_actions": len(post_actions),
        "findings_total": findings_total,
        "findings_verified": findings_verified,
        "findings_date_status": findings_date_status,
        "n_queries_batch": n_batch,
        "n_queries_adhoc": n_adhoc,
        "n_fetches": len(fetched),
        "n_sessions": usage["sessions"],
        "n_subagent_sessions": usage["subagent_sessions"],
        "delegation_called": delegate_calls > 0 or usage["subagent_sessions"] > 0,
        "delegate_task_calls": delegate_calls,
        "tool_calls": tool_counts,
        "sources_found": [
            {"id": src.get("id"), "title": one_line(src.get("title")), "url": src.get("url") or "",
             "subquestions": src.get("subquestions") or []}
            for src in sources
        ],
        "sources_fetched": [
            {"id": sid, "title": one_line((by_id.get(sid) or {}).get("title")),
             "url": (by_id.get(sid) or {}).get("url") or ""}
            for sid in dict.fromkeys(fetched)
        ],
        "sources_cited": cited,
        "cited_found_ratio": round(len(cited) / len(sources), 4) if sources else 0.0,
        "citation_coverage_pct": round(coverage_ratio * 100, 1),
        "citation_paragraphs": {"cited": cited_paragraphs, "total": paragraphs},
        "report_words": words,
        "report_words_body": body_words,
        "n_h2_total": n_h2,
        "n_thematic_sections": n_thematic,
        "final_word_count": int(final_info.get("word_count") or 0),
        "finalize_warnings": final_info.get("warnings") or [],
        "language": detect_language(report_text),
        "report_md": str(report_md.relative_to(out)) if report_md else None,
        "media_line": media,
        "media_line_valid": media_ok,
        "reply_ends_with_question": reply.strip().endswith("?"),
        "work_dir_present": work_original is not None,
        "errors": errors,
        "warnings": warnings,
        "real_hermes_home_touched": before_home != after_home,
        "real_hermes_home_before": before_home,
        "real_hermes_home_after": after_home,
    }


def render_stats_md(stats: dict) -> str:
    lines = [
        f"# {stats['topic']} — config {stats['config']} — rep {stats['rep']}",
        "",
        f"- status: **{stats['status']}**",
        f"- wall: {stats['wall_seconds']} s",
        f"- report: {stats['report_md'] or 'missing'} ({stats['report_words']} words, "
        f"{stats['n_thematic_sections']} thematic sections, language {stats['language']})",
        f"- MEDIA line: {stats['media_line'] or 'none'} (valid: {stats['media_line_valid']})",
        "",
        "## Pipeline",
        f"- subquestions: {stats['n_subquestions']} · asks covered: "
        f"{stats.get('n_covered_asks', 0)}/{stats.get('n_asks', 0)} · "
        f"post_actions: {stats.get('n_post_actions', 0)}",
        f"- queries: {stats['n_queries_batch']} batch + {stats['n_queries_adhoc']} ad-hoc, "
        f"fetches: {stats['n_fetches']}",
        f"- findings: {stats.get('findings_total', 0)} total, "
        f"{stats.get('findings_verified', 0)} verified=true",
        f"- delegation called: {stats['delegation_called']} "
        f"({stats['delegate_task_calls']} delegate_task calls, {stats['n_subagent_sessions']} subagent sessions)",
        f"- sessions: {stats['n_sessions']}",
        "",
        "## Cost",
        f"- tokens: {stats['llm_tokens'].get('input_total', stats['llm_tokens']['input'])} in "
        f"({stats['llm_tokens']['cache_read']} cached), {stats['llm_tokens']['output']} out, "
        f"{stats['llm_tokens']['api_calls']} API calls",
        f"- LLM: ${stats['llm_cost_usd']:.4f} · Exa: ${stats['exa_cost_usd']:.4f} · "
        f"total: ${stats['total_cost_usd']:.4f}",
        "",
        "## Citations",
        f"- found: {len(stats['sources_found'])} · fetched: {len(stats['sources_fetched'])} · "
        f"cited: {len(stats['sources_cited'])} · cited/found: {stats['cited_found_ratio']:.0%}",
        f"- paragraph coverage: {stats['citation_paragraphs']['cited']}/"
        f"{stats['citation_paragraphs']['total']} ({stats['citation_coverage_pct']}%)",
        "",
    ]
    if stats["sources_found"]:
        lines += ["### Sources found", ""]
        for src in stats["sources_found"]:
            subs = ",".join(src["subquestions"])
            lines.append(f"- [{src['id']}] {src['title']} — {src['url']} ({subs})")
        lines.append("")
    if stats["sources_cited"]:
        lines += ["### Sources cited", ""]
        for src in stats["sources_cited"]:
            lines.append(f"- [{src['ref']}] ← {src['id']} {src['title']} — {src['url']}")
        lines.append("")
    if stats["errors"] or stats["warnings"]:
        lines += ["## Errors / warnings", ""]
        lines += [f"- error: {item}" for item in stats["errors"]]
        lines += [f"- warning: {item}" for item in stats["warnings"]]
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deep-research eval matrix in isolated Hermes homes.")
    parser.add_argument("--batch", required=True, help="result batch name")
    parser.add_argument("--topics", default="", help="comma-separated topic ids (default: all)")
    parser.add_argument("--configs", default="A,B,C,D", help="comma-separated skill configs (A-F)")
    parser.add_argument("--reps", type=int, default=1, help="repetitions per topic x config")
    parser.add_argument("--parallel", type=int, default=3, help="parallel Hermes runs")
    parser.add_argument("--timeout-min", type=int, default=40, help="per-run wall timeout")
    parser.add_argument("--max-exa-usd", type=float, default=None,
                        help="do not start new runs once the batch's Exa spend reaches this cap")
    parser.add_argument("--force", action="store_true", help="re-run even when stats.json exists")
    parser.add_argument("--dry-run", action="store_true", help="print the run matrix, do nothing")
    args = parser.parse_args(argv)

    topics_all = load_topics()
    requested = [part.strip() for part in args.topics.split(",") if part.strip()]
    topics = [topics_all[tid] for tid in requested] if requested else list(topics_all.values())
    missing = [tid for tid in requested if tid not in topics_all]
    if missing:
        raise SystemExit(f"unknown topic id(s): {', '.join(missing)}")

    configs = [part.strip().upper() for part in args.configs.split(",") if part.strip()]
    if not configs:
        raise SystemExit("no configs selected")
    if args.reps < 1:
        raise SystemExit("--reps must be >= 1")

    runs = ordered_runs(topics, configs, args.reps, args.batch)
    if args.dry_run:
        print(f"batch: {args.batch}  runs: {len(runs)}  parallel: {args.parallel}")
        for topic, config, rep in runs:
            print(f"  {run_id(topic['id'], config, rep)}  (config {config}, rep {rep})")
        return 0

    secrets = load_secrets()
    ensure_hermes()
    sys.path.insert(0, str(EVAL_DIR))
    import variants  # noqa: E402  (same directory)

    unknown = [config for config in configs if config not in variants.CONFIGS]
    if unknown:
        raise SystemExit(f"unknown config(s): {', '.join(unknown)}")
    built = variants.build_all(VARIANTS_DIR)
    version = hermes_version()
    base_url = secrets["OPENCODE_GO_BASE_URL"]
    print(f"hermes: {version}")
    print(f"runs: {len(runs)}  batch: {args.batch}  parallel: {args.parallel}", file=sys.stderr)

    failures = 0
    completed = 0
    not_started = 0
    pending = []
    for topic, config, rep in runs:
        rid = run_id(topic["id"], config, rep)
        stats_path = RESULTS_DIR / args.batch / "runs" / rid / "stats.json"
        if stats_path.exists() and not args.force:
            print(f"[{rid}] skip (stats.json exists)", file=sys.stderr)
        else:
            pending.append((topic, config, rep))

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.parallel))
    active: dict[concurrent.futures.Future, tuple] = {}
    try:
        while pending or active:
            while pending and len(active) < max(1, args.parallel):
                if args.max_exa_usd is not None:
                    spend = batch_exa_spend(args.batch)
                    if spend >= args.max_exa_usd:
                        not_started = len(pending)
                        print(f"budget reached (${spend:.3f} >= ${args.max_exa_usd:.3f}), "
                              f"{not_started} run(s) not started", file=sys.stderr)
                        pending.clear()
                        break
                topic, config, rep = pending.pop(0)
                future = pool.submit(
                    run_one, args.batch, topic, config, rep, args, secrets, built[config],
                    version, variants.CONFIGS[config]["num_results"],
                    variants.CONFIGS[config].get("max_chars", 6000), base_url,
                )
                active[future] = (topic, config, rep)
            if not active:
                break
            done, _ = concurrent.futures.wait(active, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                topic, config, rep = active.pop(future)
                rid = run_id(topic["id"], config, rep)
                try:
                    result = future.result()
                except Exception as exc:  # keep the batch alive; the run dir has the transcript
                    failures += 1
                    print(f"[{rid}] CRASH: {exc}", file=sys.stderr)
                    continue
                if result.get("skipped"):
                    print(f"[{rid}] skip (stats.json exists)", file=sys.stderr)
                    continue
                stats = result["stats"]
                completed += 1
                if stats["status"] != "ok":
                    failures += 1
                print(f"[{rid}] {stats['status']} · {stats['wall_seconds']}s · "
                      f"{len(stats['sources_found'])} sources · {len(stats['sources_cited'])} cited · "
                      f"${stats['total_cost_usd']:.3f}", file=sys.stderr)
    finally:
        pool.shutdown(wait=True)
    suffix = f", {not_started} not started (budget)" if not_started else ""
    print(f"done: {completed} run(s), {failures} not ok{suffix}", file=sys.stderr)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
