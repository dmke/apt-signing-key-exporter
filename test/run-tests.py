#!/usr/bin/env python3
"""
test/run-tests.py — build a snapshot .deb, install it inside pre-built Docker
                     test images, and verify the Prometheus output.

Pre-requisite: build test images first:
    python3 test/create-images.py

Usage:
    python3 test/run-tests.py [-v] [DISTRO ...]

If no DISTRO arguments are given all distros are tested in sequence.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent

sys.path.insert(0, str(SCRIPT_DIR))
from config import DISTROS, IMAGE_PREFIX  # noqa: E402

# Expected primary-key fingerprints for the three static test keys.
# Run python3 test/update-keys.py and update these values if the keyrings
# are ever regenerated.
FINGERPRINTS = {
    "no-expiry": "536D4567B236D5EFF8151915A6DE716A2CE10440",
    "future-expiry": "B338DDF2D03421B411BA4A022CEA661F48AFCBD3",
    "past-expiry": "0A9FF69850E08A5A71C87467FF91AA56DB400815",
}

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def _colorize(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}" if sys.stdout.isatty() else text


class Results:
    def __init__(self) -> None:
        self.passed = self.failed = self.skipped = 0

    def ok(self, label: str) -> None:
        self.passed += 1
        print(f"  [{_colorize('PASS', _GREEN)}] {label}")

    def fail(self, label: str) -> None:
        self.failed += 1
        print(f"  [{_colorize('FAIL', _RED)}] {label}")

    def skip(self, label: str) -> None:
        self.skipped += 1
        print(f"  [{_colorize('SKIP', _YELLOW)}] {label}")

    def assert_in(self, label: str, needle: str, haystack: str) -> None:
        if needle in haystack:
            self.ok(label)
        else:
            self.fail(f"{label}  (pattern not found: {needle!r})")

    def assert_not_in(self, label: str, needle: str, haystack: str) -> None:
        if needle not in haystack:
            self.ok(label)
        else:
            self.fail(f"{label}  (unexpected pattern found: {needle!r})")


def image_exists(image: str) -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True
        ).returncode
        == 0
    )


# Inline shell script run inside each test container.
_CONTAINER_SCRIPT = """\
DEBIAN_FRONTEND=noninteractive \
    apt-get install -y /tmp/apt-signing-key-exporter.deb \
    >/tmp/install_stdout 2>/tmp/install_stderr \
|| { echo "apt-get install failed:" >&2; cat /tmp/install_stderr >&2; exit 1; }
apt_signing_key_exporter 2>/tmp/script_stderr
cat /tmp/script_stderr >&2
"""


def run_in_container(image: str, deb_path: Path) -> tuple[int, str, str]:
    """Install the .deb and run the exporter; return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--volume",
            f"{deb_path}:/tmp/apt-signing-key-exporter.deb:ro",
            image,
            "bash",
            "-c",
            _CONTAINER_SCRIPT,
        ],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def run_distro(
    distro: str, deb_path: Path, results: Results, verbose: bool = False
) -> None:
    image = f"{IMAGE_PREFIX}:{distro}"
    print(f"\n{'=' * 80}")
    print(f"Distro: {distro}  ({image})")

    if not image_exists(image):
        results.skip(
            f"image not found locally — run: python3 test/create-images.py {distro}"
        )
        return

    exit_code, stdout, stderr = run_in_container(image, deb_path)

    # Always show container output on failure; only show it in verbose mode otherwise.
    if exit_code != 0 or verbose:
        print()
        print(f"  --- stdout (exit {exit_code}) ---")
        for line in stdout.splitlines():
            print(f"  {line}")
        if stderr.strip():
            print("  --- stderr ---")
            for line in stderr.splitlines():
                print(f"  {line}")
        print("  ---")

    if exit_code == 0:
        results.ok("exit code is 0")
    else:
        results.fail(f"exit code is {exit_code} (expected 0)")

    results.assert_in(
        "expire metric present", "apt_signing_key_expire_time_seconds", stdout
    )

    # Primary-key fingerprints for all three test keys must appear.
    # Subkeys will produce additional rows with different fingerprints.
    for name, fpr in FINGERPRINTS.items():
        results.assert_in(f"{name} key fingerprint", fpr, stdout)

    # The no-expiry key must report expires=0.
    results.assert_in(
        "no-expiry key reports expires=0",
        f'fingerprint="{FINGERPRINTS["no-expiry"]}"',
        stdout,
    )

    # The commented-out line in test-legacy.list must NOT produce a metric.
    results.assert_not_in(
        "disabled .list entry absent", "legacy-disabled.example.invalid", stdout
    )

    # No key-read errors expected for well-formed test fixtures.
    results.assert_not_in("no read-error metric", "apt_signing_key_read_error", stdout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build a snapshot .deb and test it inside Docker containers.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Always print container stdout/stderr (default: only on failure).",
    )
    parser.add_argument(
        "distros",
        nargs="*",
        metavar="DISTRO",
        help=f"Distros to test (default: all).  Valid: {', '.join(DISTROS)}.",
    )
    args = parser.parse_args()

    if args.distros:
        unknown = [d for d in args.distros if d not in DISTROS]
        if unknown:
            parser.error(
                f"Unknown distro(s): {', '.join(unknown)}.  Valid: {', '.join(DISTROS)}."
            )
    distros = args.distros or list(DISTROS)

    print("Testing apt_signing_key_exporter")
    print(f"Distros: {' '.join(distros)}")

    with tempfile.NamedTemporaryFile(
        prefix="apt-signing-key-exporter_", suffix=".deb", delete=False
    ) as fh:
        deb_path = Path(fh.name)

    try:
        print("\nBuilding snapshot .deb ...")
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "build" / "build.py"), str(deb_path)],
            check=True,
        )
        print()

        results = Results()
        for distro in distros:
            run_distro(distro, deb_path, results, verbose=args.verbose)

        print(
            f"\n{'=' * 80}"
            f"\nResults: "
            f"{_colorize(str(results.passed), _GREEN)} passed, "
            f"{_colorize(str(results.failed), _RED)} failed, "
            f"{_colorize(str(results.skipped), _YELLOW)} skipped"
        )
        sys.exit(0 if results.failed == 0 else 1)
    finally:
        deb_path.unlink(missing_ok=True)
