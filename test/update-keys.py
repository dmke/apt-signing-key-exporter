#!/usr/bin/env python3
"""
test/update-keys.py — (re)generate static GPG test keys in test/keyrings/.

This script is for MAINTENANCE ONLY.  The generated .gpg and .asc files are
committed to the repository so that tests can run without regenerating keys.

WARNING: re-running this script creates keys with NEW fingerprints and
invalidates the FINGERPRINTS dict in test/run-tests.py.  The script prints
a summary at the end to make the update easy.

A throwaway GNUPGHOME is used so private keys never leave the temp directory.

Usage:
    python3 test/update-keys.py
"""

import subprocess
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
KEYRINGS = SCRIPT_DIR / "keyrings"
SOURCES = SCRIPT_DIR / "sources"

# ---------------------------------------------------------------------- helpers


def run_gpg(
    *args: str,
    gnupghome: Path,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, "GNUPGHOME": str(gnupghome)}
    return subprocess.run(
        ["gpg", "--batch", "--no-tty", "--quiet", *args],
        input=input,
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )


def fingerprint(email: str, gnupghome: Path) -> str:
    result = run_gpg("--list-keys", "--with-colons", email, gnupghome=gnupghome)
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if parts[0] == "fpr":
            return parts[9]
    raise RuntimeError(f"fingerprint not found for {email!r}")


def export_binary(fpr: str, dest: Path, gnupghome: Path) -> None:
    result = run_gpg("--export", fpr, gnupghome=gnupghome)
    dest.write_bytes(result.stdout.encode("latin-1"))


def export_armored(fpr: str, dest: Path, gnupghome: Path) -> None:
    result = run_gpg("--armor", "--export", fpr, gnupghome=gnupghome)
    dest.write_text(result.stdout)


# -------------------------------------------------------------------- key specs

_KEY_PARAMS = [
    (
        "no-expiry",
        "no-expiry@test.invalid",
        """\
%no-protection
Key-Type:      RSA
Key-Length:    2048
Subkey-Type:   RSA
Subkey-Length: 2048
Name-Real:     Test Key No Expiry
Name-Email:    no-expiry@test.invalid
Expire-Date:   0
%commit
""",
        None,  # no faked system time
    ),
    (
        "future-expiry",
        "future@test.invalid",
        """\
%no-protection
Key-Type:      RSA
Key-Length:    2048
Subkey-Type:   RSA
Subkey-Length: 2048
Name-Real:     Test Key Future Expiry
Name-Email:    future@test.invalid
Expire-Date:   2030-06-01
%commit
""",
        None,
    ),
    (
        "past-expiry",
        "past@test.invalid",
        """\
%no-protection
Key-Type:      RSA
Key-Length:    2048
Subkey-Type:   RSA
Subkey-Length: 2048
Name-Real:     Test Key Past Expiry
Name-Email:    past@test.invalid
Expire-Date:   2021-01-01
%commit
""",
        "20200101T000000!",  # back-date so key is demonstrably expired
    ),
]


# --------------------------------------------------------------- inline sources


def build_inline_sources(asc_path: Path, out_path: Path) -> None:
    lines = asc_path.read_text().rstrip().splitlines()
    signed_by_lines = []
    for i, line in enumerate(lines):
        if i == 0:
            signed_by_lines.append("Signed-By: " + line)
        elif line == "":
            signed_by_lines.append(" .")
        else:
            signed_by_lines.append(" " + line)

    content = (
        "Types: deb\n"
        "URIs: https://inline.example.invalid\n"
        "Suites: stable\n"
        "Components: main\n" + "\n".join(signed_by_lines) + "\n"
    )
    out_path.write_text(content)
    print(f"  written: {out_path.relative_to(SCRIPT_DIR.parent)}")


# ------------------------------------------------------------------------- main


def main() -> None:
    KEYRINGS.mkdir(parents=True, exist_ok=True)
    SOURCES.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="gnupghome-") as _tmp:
        gnupghome = Path(_tmp)
        gnupghome.chmod(0o700)

        fprs: dict[str, str] = {}

        for stem, email, params, fake_time in _KEY_PARAMS:
            label = stem.replace("-", " ")
            print(f"==> Generating key: {label}")
            args = ["--gen-key"]
            if fake_time:
                args.append("--faked-system-time")
                args.append(fake_time)
            run_gpg(*args, gnupghome=gnupghome, input=params)

            fpr = fingerprint(email, gnupghome)
            fprs[stem] = fpr
            gpg_dest = KEYRINGS / f"test-{stem}.gpg"
            asc_dest = KEYRINGS / f"test-{stem}.asc"

            # Binary export needs raw bytes; run gpg directly without text mode.
            import os

            env = {**os.environ, "GNUPGHOME": str(gnupghome)}
            with open(gpg_dest, "wb") as fh:
                subprocess.run(
                    ["gpg", "--batch", "--no-tty", "--quiet", "--export", fpr],
                    stdout=fh,
                    env=env,
                    check=True,
                )
            export_armored(fpr, asc_dest, gnupghome)
            print(f"    fingerprint: {fpr}")
            print(f"    written:     keyrings/test-{stem}.gpg")
            print(f"    written:     keyrings/test-{stem}.asc")

        print()
        print("==> Regenerating test/sources/test-inline.sources")
        build_inline_sources(
            KEYRINGS / "test-no-expiry.asc",
            SOURCES / "test-inline.sources",
        )

        print()
        print("==> Key summary — update FINGERPRINTS in test/run-tests.py if needed:")
        col = max(len(s) for s in fprs) + 2
        for stem, fpr in fprs.items():
            print(f"  {stem:<{col}}  {fpr}")

        print()
        print("Done.")


if __name__ == "__main__":
    main()
