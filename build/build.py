#!/usr/bin/env python3
"""
build/build.py — stamp the git version and build a .deb package.

Usage:
    python3 build/build.py [TARGET]

TARGET  output path for the produced .deb.
          file path → nfpm writes exactly to that path
          directory → nfpm picks the name automatically
          (default: build/)

Side effects in build/:
    build/apt_signing_key_exporter.py  — versioned copy of the exporter script

Requires: git, nfpm
"""

import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent


def git_version() -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "describe", "--always", "--tags", "--dirty"],
        capture_output=True,
        text=True,
        check=True,
    )
    raw = result.stdout.strip()
    # Tag found: strip the conventional 'v' prefix (e.g. v1.2.3 → 1.2.3).
    if raw.startswith("v"):
        return raw[1:]
    # No tags: git fell back to a bare commit hash (e.g. "f28b6ff" or
    # "f28b6ff-dirty").  Prefix with 0.0.0+ so the version is valid Debian
    # syntax and sorts below any real release, regardless of whether the first
    # character of the hash happens to be a digit.
    return "0.0.0+" + raw


def stamp_script(version: str) -> None:
    """Write a version-stamped copy of the exporter script into build/."""
    src = REPO_ROOT / "src" / "apt_signing_key_exporter.py"
    dst = SCRIPT_DIR / "apt_signing_key_exporter.py"
    dst.write_text(
        re.sub(
            r'__version__ = "[^"]*"',
            f'__version__ = "{version}"',
            src.read_text(),
        )
    )


def build_deb(version: str, target: Path) -> None:
    """Run nfpm to produce the .deb, passing VERSION via the environment."""
    subprocess.run(
        [
            "nfpm",
            "package",
            "--packager",
            "deb",
            "--config",
            str(SCRIPT_DIR / "nfpm.yaml"),
            "--target",
            str(target),
        ],
        env={**os.environ, "VERSION": version},
        check=True,
        cwd=REPO_ROOT,
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        # Treat as a file path: ensure its parent directory exists.
        if not target.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
    else:
        target = SCRIPT_DIR
        target.mkdir(parents=True, exist_ok=True)

    version = git_version()
    print(f"Version: {version}")

    stamp_script(version)
    build_deb(version, target)
