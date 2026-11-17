#!/usr/bin/env python3
"""Build patched copies of the deep-research skill for the eval matrix.

There is one config: **K**, the shipped skill, unpatched. It exists so the eval
runner has a stable place to copy the skill from and one place to record the
run-time knobs.

Configs
-------
K  shipped skill exactly as committed, 20 results per query (DeepSeek Search & Fetch)

`num_results` is applied at run time through `DEEP_RESEARCH_NUM_RESULTS`.

Usage
-----
    python3 evals/variants.py --check
    python3 evals/variants.py --build [--out evals/.run/skills]
"""

from __future__ import annotations

import argparse
import difflib
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = ROOT / "skills" / "deep-research"
DEFAULT_OUT = ROOT / "evals" / ".run" / "skills"

CONFIGS: dict[str, dict] = {
    "K": {
        "description": "shipped skill, unpatched: 20 results/query, DeepSeek Search & Fetch",
        "num_results": 20,
    },
}


class VariantError(RuntimeError):
    """A variant could not be built."""


def patched_skill_md(config: str) -> str:
    """The SKILL.md a variant ships with. K is unpatched."""
    if config not in CONFIGS:
        raise VariantError(f"unknown config {config!r}; choose from {', '.join(CONFIGS)}")
    return (SOURCE_SKILL / "SKILL.md").read_text(encoding="utf-8")


def build_variant(config: str, out_root: Path) -> Path:
    """Copy the shipped skill to <out_root>/<config>/deep-research."""
    if config not in CONFIGS:
        raise VariantError(f"unknown config {config!r}; choose from {', '.join(CONFIGS)}")
    dest = out_root / config / "deep-research"
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE_SKILL, dest)
    (dest / "SKILL.md").write_text(patched_skill_md(config), encoding="utf-8")
    return dest


def build_all(out_root: Path) -> dict[str, Path]:
    return {config: build_variant(config, out_root) for config in CONFIGS}


def check() -> int:
    original = (SOURCE_SKILL / "SKILL.md").read_text(encoding="utf-8").splitlines(keepends=True)
    failed = False
    with tempfile.TemporaryDirectory(prefix="deep-research-variants-") as tmp:
        root = Path(tmp)
        for config in CONFIGS:
            try:
                dest = build_variant(config, root)
            except (VariantError, OSError) as exc:
                print(f"ERROR {config}: {exc}", file=sys.stderr)
                failed = True
                continue
            patched = (dest / "SKILL.md").read_text(encoding="utf-8").splitlines(keepends=True)
            diff = list(difflib.unified_diff(
                original, patched,
                fromfile="skills/deep-research/SKILL.md",
                tofile=f"variants/{config}/deep-research/SKILL.md",
                n=2,
            ))
            print(f"=== {config}: {CONFIGS[config]['description']} ===")
            if diff:
                failed = True
                sys.stdout.writelines(diff)
            else:
                print("(no SKILL.md changes)")
            print()
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build patched deep-research skill variants.")
    parser.add_argument("--build", action="store_true", help="write variants under --out")
    parser.add_argument("--check", action="store_true", help="verify each variant builds unchanged")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output root (default evals/.run/skills)")
    parser.add_argument("--configs", default=",".join(CONFIGS), help="comma-separated config ids")
    args = parser.parse_args(argv)
    if not args.build and not args.check:
        parser.error("choose --build or --check")

    configs = [part.strip().upper() for part in args.configs.split(",") if part.strip()]
    if args.check:
        return check()
    for config in configs:
        try:
            dest = build_variant(config, Path(args.out))
        except VariantError as exc:
            print(f"ERROR {config}: {exc}", file=sys.stderr)
            return 1
        print(f"{config}: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
