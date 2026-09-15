#!/usr/bin/env python3
"""Regenerate the pinned test-dependency locks from requirements-test.txt.

Resolves with uv (not pip-compile) because pip-compile on this repo's
dev machine (macOS) evaluates environment markers against the host, so
darwin-only extras leak into a lock meant for Linux CI. uv's
--python-platform lets us resolve for the CI target directly.

requirements-test.lock covers everything with a Linux/py3.12 wheel and
is installed with --only-binary :all:. requirements-test-sdist.lock
covers the few packages with no wheel at all (sdist only) and is
installed separately with --no-deps, since --only-binary can't place
them. That split is discovered by re-resolving with --only-binary
:all: and reading off which packages uv rejects, not from a hand-kept
list, so a new sdist-only dependency fails this script loudly instead
of silently breaking the CI install.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "requirements-test.txt"
FULL_LOCK = ROOT / "requirements-test.lock"
SDIST_LOCK = ROOT / "requirements-test-sdist.lock"

PYTHON_VERSION = "3.12"
PYTHON_PLATFORM = "x86_64-manylinux_2_28"  # matches the ubuntu-24.04 CI runner

SDIST_HEADER = (
    "#\n"
    "# packages below publish no wheel (sdist only); hash-pinned, no deps.\n"
    "# Regenerate both lock files with: make test-lock-regen\n"
    "#\n"
)

NO_WHEEL_RE = re.compile(r"Because ([A-Za-z0-9][A-Za-z0-9._-]*)==\S+ has no usable wheels")
ENTRY_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==")
MAX_NO_WHEEL_ITERATIONS = 50


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, cwd=ROOT)


def base_compile_cmd(output: Path) -> list[str]:
    return [
        "uv",
        "pip",
        "compile",
        "--python-version",
        PYTHON_VERSION,
        "--python-platform",
        PYTHON_PLATFORM,
        "--generate-hashes",
        "--prerelease=allow",
        "--output-file",
        str(output),
        str(SOURCE),
    ]


def compile_full_lock(output: Path) -> None:
    result = run(base_compile_cmd(output))
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        sys.exit(f"uv pip compile failed (exit {result.returncode})")


def find_sdist_only_packages(constraint: Path) -> list[str]:
    """Re-resolve with --only-binary :all:, peeling off one no-wheel
    package at a time (via --no-binary) until it resolves, pinned to
    the versions already chosen in `constraint` so the resolver can't
    wander into unrelated version conflicts."""
    no_binary: list[str] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        probe_output = Path(tmpdir) / "probe.lock"
        for _ in range(MAX_NO_WHEEL_ITERATIONS):
            cmd = base_compile_cmd(probe_output)
            cmd += ["--only-binary", ":all:", "--constraint", str(constraint)]
            for pkg in no_binary:
                cmd += ["--no-binary", pkg]
            result = run(cmd)
            if result.returncode == 0:
                return no_binary
            match = NO_WHEEL_RE.search(result.stderr)
            if not match:
                sys.stderr.write(result.stderr)
                sys.exit("uv pip compile failed for a reason other than a missing wheel")
            pkg = match.group(1).lower()
            if pkg in no_binary:
                sys.stderr.write(result.stderr)
                sys.exit(f"{pkg} flagged as no-wheel twice; resolver made no progress")
            no_binary.append(pkg)
    sys.exit(f"gave up after {MAX_NO_WHEEL_ITERATIONS} iterations without a stable no-wheel set")


def parse_entries(lock_text: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    lines = lock_text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        match = ENTRY_RE.match(lines[i])
        if not match:
            i += 1
            continue
        name = match.group(1).lower()
        block = [lines[i]]
        i += 1
        while i < len(lines) and (lines[i].startswith(" ") or lines[i].lstrip().startswith("#")):
            block.append(lines[i])
            i += 1
        entries.append((name, "".join(block)))
    return entries


def main() -> None:
    compile_full_lock(FULL_LOCK)
    full_text = FULL_LOCK.read_text()
    header = "".join(
        line for line in full_text.splitlines(keepends=True) if line.startswith("#")
    )

    print("Checking which packages have no wheel for "
          f"py{PYTHON_VERSION} {PYTHON_PLATFORM}...")
    sdist_names = find_sdist_only_packages(FULL_LOCK)

    entries = parse_entries(full_text)
    if not entries:
        sys.exit("uv pip compile produced no pinned entries; refusing to write empty locks")

    binary_blocks = [block for name, block in entries if name not in sdist_names]
    sdist_blocks = [block for name, block in entries if name in sdist_names]

    if len(sdist_blocks) != len(sdist_names):
        sys.exit(
            f"expected {len(sdist_names)} sdist-only entries in the full lock, "
            f"found {len(sdist_blocks)}"
        )

    FULL_LOCK.write_text(header + "".join(binary_blocks))

    if sdist_names:
        print("No-wheel (sdist-only) packages: " + ", ".join(sorted(sdist_names)))
    else:
        print("No sdist-only packages found.")
    SDIST_LOCK.write_text(SDIST_HEADER + "".join(sdist_blocks))

    print(f"Wrote {FULL_LOCK} ({len(binary_blocks)} packages)")
    print(f"Wrote {SDIST_LOCK} ({len(sdist_blocks)} packages)")


if __name__ == "__main__":
    main()
