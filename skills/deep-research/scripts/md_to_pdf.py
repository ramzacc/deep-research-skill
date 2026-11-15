#!/usr/bin/env python3
"""Convert a markdown research report to a styled PDF with clickable links.

Usage:
    python3 md_to_pdf.py <in.md> [out.pdf]

Implementation: `markdown-pdf` (PyMuPDF based, pure wheels) inside a private
venv at ~/.local/share/hermes-deep-research/venv, bootstrapped on first run and
reused afterwards. If the venv cannot be created (e.g. python3-venv missing),
falls back to pandoc when it is on PATH, then to an actionable error.

Only the Python standard library is required by this file itself.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

IN_VENV_ENV = "HERMES_DEEP_RESEARCH_IN_VENV"
VENV_OVERRIDE_ENV = "HERMES_DEEP_RESEARCH_VENV"
PIN = "markdown-pdf>=1.13"

CSS = """
body { font-family: sans-serif; font-size: 10.5pt; line-height: 1.45; color: #1b1b1b; }
h1 { font-size: 20pt; margin: 0 0 6pt 0; }
h2 { font-size: 14.5pt; margin: 16pt 0 6pt 0; border-bottom: 1px solid #d0d0d0; padding-bottom: 2pt; }
h3 { font-size: 12pt; margin: 12pt 0 4pt 0; }
p { margin: 0 0 8pt 0; }
ul, ol { margin: 0 0 8pt 0; padding-left: 16pt; }
li { margin-bottom: 3pt; }
a { color: #0b57d0; text-decoration: none; }
code { font-family: monospace; font-size: 9pt; background: #f2f2f2; }
pre { background: #f5f5f5; padding: 6pt; margin: 0 0 8pt 0; }
table { border-collapse: collapse; margin: 0 0 10pt 0; }
th, td { border: 1px solid #c8c8c8; padding: 3pt 6pt; text-align: left; vertical-align: top; }
th { background: #f0f0f0; }
blockquote { color: #444; border-left: 3px solid #ccc; margin: 0 0 8pt 0; padding-left: 8pt; }
"""


def note(message: str) -> None:
    sys.stderr.write(message.rstrip("\n") + "\n")
    sys.stderr.flush()


def die(message: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    sys.stderr.write(message.rstrip("\n") + "\n")
    raise SystemExit(code)


def venv_dir() -> Path:
    override = os.environ.get(VENV_OVERRIDE_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "hermes-deep-research" / "venv"


def venv_python() -> Path:
    return venv_dir() / "bin" / "python"


def venv_ready() -> bool:
    py = venv_python()
    if not py.exists():
        return False
    probe = subprocess.run(
        [str(py), "-c", "import markdown_pdf"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    return probe.returncode == 0


def bootstrap_venv() -> bool:
    """Create the private venv and install markdown-pdf. True on success."""
    vd = venv_dir()
    vd.parent.mkdir(parents=True, exist_ok=True)
    note(f"pdf: creating private venv at {vd} (first run only)")
    try:
        created = subprocess.run(
            [sys.executable, "-m", "venv", str(vd)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        note(f"pdf: venv creation failed: {exc}")
        return False
    if created.returncode != 0:
        detail = (created.stderr or created.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {created.returncode}"
        note(f"pdf: venv creation failed: {tail}")
        return False
    note(f"pdf: installing {PIN} ...")
    try:
        installed = subprocess.run(
            [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "--quiet", PIN],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        note(f"pdf: pip install failed: {exc}")
        return False
    if installed.returncode != 0:
        tail = (installed.stderr or installed.stdout or "").strip().splitlines()
        note(f"pdf: pip install failed: {tail[-1] if tail else 'unknown error'}")
    return venv_ready()


def reexec_in_venv(argv: list[str]) -> int:
    py = venv_python()
    env = dict(os.environ)
    env[IN_VENV_ENV] = "1"
    note(f"pdf: using venv {venv_dir()}")
    os.environ[IN_VENV_ENV] = "1"
    os.execve(str(py), [str(py), str(Path(__file__).resolve()), *argv], env)
    return 1  # unreachable


def title_of(markdown: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1)
    return "Research report"


def convert_with_markdown_pdf(in_path: Path, out_path: Path, started: float) -> int:
    from markdown_pdf import MarkdownPdf, Section  # imported inside the venv only

    markdown = in_path.read_text(encoding="utf-8")
    title = title_of(markdown)
    pdf = MarkdownPdf(toc_level=2, optimize=True)
    pdf.meta["title"] = title
    pdf.meta["author"] = "Hermes deep-research"
    pdf.add_section(Section(markdown, toc=False, paper_size="A4"), user_css=CSS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(out_path)
    if not out_path.exists() or out_path.stat().st_size == 0:
        die(f"error: pdf was not written: {out_path}", 1)
    elapsed = time.time() - started
    print(f"pdf: {out_path}")
    print(f"title: {title}")
    print(f"size: {out_path.stat().st_size} bytes")
    print(f"time: {elapsed:.2f}s")
    return 0


def convert_with_pandoc(in_path: Path, out_path: Path, started: float) -> int | None:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return None
    note("pdf: falling back to pandoc")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    attempts = [
        [pandoc, str(in_path), "-o", str(out_path), "--standalone"],
        [pandoc, str(in_path), "-o", str(out_path), "--standalone", "--pdf-engine=weasyprint"],
        [pandoc, str(in_path), "-o", str(out_path), "--standalone", "--pdf-engine=wkhtmltopdf"],
    ]
    for cmd in attempts:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            elapsed = time.time() - started
            print(f"pdf: {out_path}")
            print(f"size: {out_path.stat().st_size} bytes")
            print(f"time: {elapsed:.2f}s")
            print("engine: pandoc")
            return 0
    note("pdf: pandoc exists but no working PDF engine (install weasyprint or wkhtmltopdf)")
    return None


def main(argv: list[str]) -> int:
    if len(argv) == 1 and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if not argv or len(argv) > 2:
        die("usage: md_to_pdf.py <in.md> [out.pdf]", 2)
    started = time.time()
    in_path = Path(argv[0]).expanduser().resolve()
    if not in_path.is_file():
        die(f"error: input markdown not found: {in_path}", 2)
    out_path = (
        Path(argv[1]).expanduser().resolve() if len(argv) == 2 else in_path.with_suffix(".pdf")
    )
    if out_path == in_path:
        die("error: output path must differ from the input markdown path", 2)

    if os.environ.get(IN_VENV_ENV) != "1":
        if venv_ready():
            return reexec_in_venv(argv)
        if bootstrap_venv():
            return reexec_in_venv(argv)
        fallback = convert_with_pandoc(in_path, out_path, started)
        if fallback is not None:
            return fallback
        die(
            "error: cannot build a PDF: no usable markdown-pdf venv and no pandoc.\n"
            "Fix one of:\n"
            "  1) Install venv support and re-run (it bootstraps itself):\n"
            "       Debian/Ubuntu: sudo apt install python3-venv\n"
            "       Fedora:        sudo dnf install python3-pip\n"
            f"  2) pip install --user {PIN}  (then set {VENV_OVERRIDE_ENV} to a venv you control)\n"
            "  3) Install pandoc plus weasyprint (or wkhtmltopdf) and re-run.\n"
            f"The report markdown is still available at: {in_path}",
            1,
        )

    # inside the venv
    try:
        return convert_with_markdown_pdf(in_path, out_path, started)
    except ImportError as exc:
        note(f"pdf: markdown-pdf import failed inside venv: {exc}")
        fallback = convert_with_pandoc(in_path, out_path, started)
        if fallback is not None:
            return fallback
        die(
            f"error: markdown-pdf is not importable from {venv_dir()}.\n"
            f"Remove the venv and re-run to reinstall: rm -rf {venv_dir()}",
            1,
        )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
