#!/usr/bin/env python3
"""
test/create-images.py — build local Docker test images with dependencies and
                         test fixtures pre-baked in.

Produces one image per supported distro:

    apt-sources-test:bookworm   (FROM debian:bookworm-slim)
    apt-sources-test:trixie     (FROM debian:trixie-slim)
    apt-sources-test:forky      (FROM debian:forky-slim)
    apt-sources-test:noble      (FROM ubuntu:noble)
    apt-sources-test:resolute   (FROM ubuntu:resolute)

Each image has python3, python3-apt, and python3-gpg installed, the distro's
own APT sources removed, and the test keyrings/sources baked in.  The
exporter script itself is NOT included — it is installed from a fresh .deb
at test time so that run-tests.py always exercises the current working tree.

Usage:
    python3 test/create-images.py [OPTIONS] [DISTRO ...]

Options:
    --no-pull    Skip pulling base images (use locally cached versions).
    --no-cache   Pass --no-cache to docker build (force full rebuild).

If no DISTRO arguments are given all distros are built in parallel.
"""

import argparse
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
REPO_ROOT = SCRIPT_DIR.parent

sys.path.insert(0, str(SCRIPT_DIR))
from config import DISTROS, IMAGE_PREFIX  # noqa: E402

# Each build uses a temp Dockerfile so parallel jobs don't fight over stdin.
_DOCKERFILE = """\
FROM {base}

# Install Python and the APT / GPGME bindings.  Use --no-install-recommends to
# keep the image lean; clean the apt cache afterwards.
RUN apt-get update -qq \\
 && DEBIAN_FRONTEND=noninteractive \\
    apt-get install -y -qq --no-install-recommends \\
        python3 python3-apt python3-gpg \\
 && rm -rf /var/lib/apt/lists/*

# Remove all distro APT source entries so that the test fixtures are the only
# sources visible to aptsources.sourceslist.SourcesList().
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*

# Bake in the test keyrings and source fixtures.
# Keyrings go into a subdirectory so they don't shadow the distro's own keys
# (those were needed during the apt-get install step above).
COPY test/keyrings/ /usr/share/keyrings/apt-sources-test/
COPY test/sources/  /etc/apt/sources.list.d/
"""


def pull_image(base: str) -> None:
    # Failure is non-fatal: the build will use whatever is cached locally.
    subprocess.run(["docker", "pull", "--quiet", base], check=False)


def build_image(distro: str, base: str, no_cache: bool) -> tuple[bool, str]:
    """Build one test image; return (success, combined stdout+stderr)."""
    tag = f"{IMAGE_PREFIX}:{distro}"
    with tempfile.NamedTemporaryFile(
        suffix=".Dockerfile", mode="w", delete=False
    ) as fh:
        fh.write(_DOCKERFILE.format(base=base))
        df_path = Path(fh.name)

    try:
        cmd = ["docker", "build", "--tag", tag, "--file", str(df_path)]
        if no_cache:
            cmd.append("--no-cache")
        cmd.append(str(REPO_ROOT))

        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0, result.stdout + result.stderr
    finally:
        df_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build Docker test images for apt-signing-key-exporter.",
    )
    parser.add_argument(
        "--no-pull", action="store_true", help="Skip pulling base images."
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="Force full Docker layer rebuild."
    )
    parser.add_argument(
        "distros",
        nargs="*",
        metavar="DISTRO",
        help=f"Distros to build (default: all).  Valid: {', '.join(DISTROS)}.",
    )
    args = parser.parse_args()

    unknown = [d for d in args.distros if d not in DISTROS]
    if unknown:
        parser.error(
            f"Unknown distro(s): {', '.join(unknown)}.  Valid: {', '.join(DISTROS)}."
        )

    selected = {d: DISTROS[d] for d in (args.distros or DISTROS)}

    if not args.no_pull:
        print("Pulling base images...")
        with ThreadPoolExecutor() as ex:
            futures = [ex.submit(pull_image, base) for base in selected.values()]
            for f in futures:
                f.result()
        print()

    print("Building images (in parallel)...")
    results: dict[str, tuple[bool, str]] = {}

    with ThreadPoolExecutor() as ex:
        future_to_distro = {
            ex.submit(build_image, d, b, args.no_cache): d for d, b in selected.items()
        }
        for future in as_completed(future_to_distro):
            distro = future_to_distro[future]
            results[distro] = future.result()

    failed = 0
    for distro in selected:
        tag = f"{IMAGE_PREFIX}:{distro}"
        ok, log = results[distro]
        if ok:
            print(f"  [OK]   {tag}")
        else:
            failed += 1
            print(f"  [FAIL] {tag}")
            print("  --- build log ---")
            for line in log.splitlines():
                print(f"  {line}")
            print("  ---")

    print()
    if failed:
        print(f"{failed} image(s) failed to build.", file=sys.stderr)
        sys.exit(1)
    print("All images built successfully.")


if __name__ == "__main__":
    main()
