#!/usr/bin/env python3
"""
apt_signing_key_exporter - node_exporter textfile collector for APT signing key expiry.

Reads all APT source list files, identifies signing keys referenced via "signed-by=..."
(traditional .list format) or "Signed-By: ..." (deb822 .sources format), and writes
each key's expiration date as a Prometheus gauge metric to a file (or stdout).

Intended use: run once a day via cron or a systemd timer, writing to the node_exporter
textfile collector directory (e.g. /var/lib/node_exporter/textfile_collector/).

Usage:
    python3 apt_signing_key_exporter.py [--output FILE]

Requires:
    - python3-apt  (aptsources.sourceslist)
    - python3-gpg  (gpg, GPGME bindings ≥ 1.18)
"""

import argparse
import inspect
import os
import re
import sys
import tempfile
from collections import defaultdict
from typing import NamedTuple, Optional

import gpg

try:
    import aptsources.sourceslist as apt

    _HAS_APT = True
except ImportError:
    apt = None  # type: ignore[assignment]
    _HAS_APT = False

__version__ = "development"  # replaced in build script


# ----------------------------------------------------------------------
# Data types
# ----------------------------------------------------------------------


class SignedByRef(NamedTuple):
    """A single Signed-By reference found in an APT source entry."""

    source_file: str  # absolute path to the .list/.sources file
    key_ref: str  # file path, inline PGP block, or fingerprint hex


class GPGKeyRecord(NamedTuple):
    """One key (primary or subkey) from a GPGME key object."""

    fingerprint: str  # full 40-hex-char fingerprint
    uid: str  # first non-revoked, non-invalid user ID (or '')
    key_type: str  # 'pub' (primary) or 'sub' (subkey)
    expires: int  # expiry unix timestamp; 0 = never expires


# ----------------------------------------------------------------------
# Source list parsing
# ----------------------------------------------------------------------


def _signed_by_from_entry(entry: object) -> Optional[str]:
    """
    Extract the raw Signed-By value from an aptsources entry object.

    Handles both Deb822SourceEntry (.sources files) and the classic
    SourceEntry (.list files). Returns None when no Signed-By is present.
    """
    entry_type = type(entry).__name__

    if entry_type == "Deb822SourceEntry":
        # entry.section is an aptsources._deb822.Section backed by apt_pkg.TagSection.
        # For multiline values the leading space of each continuation line is
        # preserved by apt_pkg but is part of the PGP-armored block syntax,
        # so we do NOT strip continuation-line spaces here.
        val: Optional[str] = entry.section.get("Signed-By")  # type: ignore[union-attr]
        return val if val else None

    # SourceEntry / ExplodedDeb822SourceEntry - parse the raw text line.
    line: str = getattr(entry, "line", "")
    m = re.search(r"\bsigned-by=([^\s\]]+)", line, re.IGNORECASE)
    return m.group(1) if m else None


def collect_signed_by_refs() -> list[SignedByRef]:
    """Parse all APT source lists and return every Signed-By reference found.

    Disabled (commented-out) entries are skipped. Duplicate (source_file,
    key_ref) pairs are deduplicated.

    Returns an empty list and emits a warning when python3-apt is unavailable.
    """
    if not _HAS_APT:
        print(
            "Warning: python3-apt not available; cannot parse APT source lists.",
            file=sys.stderr,
        )
        return []

    # python3-apt >= 2.x added a deb822 keyword argument to SourcesList that
    # enables loading .sources files. Detect it at run-time for compatibility
    # with older releases (e.g. Ubuntu Noble ships 2.7.7).
    try:
        sig = inspect.signature(apt.SourcesList.__init__)  # type: ignore[union-attr]
        use_deb822 = "deb822" in sig.parameters
    except (ValueError, TypeError):
        use_deb822 = False

    try:
        sources = (
            apt.SourcesList(deb822=True)  # type: ignore[call-arg]
            if use_deb822
            else apt.SourcesList()  # type: ignore[call-arg]
        )
    except Exception as err:
        print(f"Warning: SourcesList init failed: {err}", file=sys.stderr)
        return []

    refs: list[SignedByRef] = []
    seen: set[tuple[str, str]] = set()

    for entry in sources.list:
        # Skip disabled (commented-out) entries.
        if getattr(entry, "disabled", False):
            continue

        key_ref = _signed_by_from_entry(entry)
        if not key_ref:
            continue

        pair = (entry.file, key_ref)
        if pair in seen:
            continue
        seen.add(pair)
        refs.append(SignedByRef(source_file=entry.file, key_ref=key_ref))

    return refs


# ----------------------------------------------------------------------
# GPG key inspection
# ----------------------------------------------------------------------


def _normalize_inline_key(pgp_block: str) -> bytes:
    """
    Normalise a deb822 inline PGP block and return it as UTF-8 bytes.

    In a deb822 file, continuation lines start with a single space, and
    empty lines within a value are represented as " ." (space + dot):

        Signed-By: -----BEGIN PGP PUBLIC KEY BLOCK-----
         .
         mQINBF...
         -----END PGP PUBLIC KEY BLOCK-----

    apt_pkg.TagSection preserves those leading spaces. Strip exactly one
    leading space per continuation line, and convert " ." back to an empty
    line (the PGP armor blank-line separator between header and base64 data).
    """
    lines = pgp_block.splitlines()
    cleaned = []
    for line in lines:
        if line == " .":
            cleaned.append("")  # deb822 empty-line sentinel → blank
        elif line.startswith(" "):
            cleaned.append(line[1:])  # strip one continuation-line space
        else:
            cleaned.append(line)
    return ("\n".join(cleaned) + "\n").encode()


def _parse_gpg_keys(keys) -> list[GPGKeyRecord]:
    """
    Convert an iterable of GPGME Key objects into GPGKeyRecord instances.

    In GPGME, key.subkeys is a flat list where element 0 is the primary key
    itself (key.subkeys[0].fpr == key.fpr) and subsequent elements are actual
    subkeys. All subkeys inherit the primary key's first valid UID.
    """
    records: list[GPGKeyRecord] = []
    for key in keys:
        first_uid = next(
            (u.uid for u in key.uids if not u.revoked and not u.invalid),
            "",
        )
        for i, sk in enumerate(key.subkeys):
            records.append(
                GPGKeyRecord(
                    fingerprint=sk.fpr,
                    uid=first_uid,
                    key_type="pub" if i == 0 else "sub",
                    expires=sk.expires,
                )
            )
    return records


def _classify_key_ref(key_ref: str) -> str:
    """Classify a Signed-By value as 'file', 'inline', or 'fingerprint'."""
    stripped = key_ref.strip()
    if stripped.startswith("/") or stripped.startswith("./"):
        return "file"
    if "-----BEGIN PGP" in stripped:
        return "inline"
    # Fingerprint: hex chars, optionally separated by spaces or colons
    if re.fullmatch(r"[0-9A-Fa-f][0-9A-Fa-f :]+", stripped):
        return "fingerprint"
    return "unknown"


def _import_raw_keys(key_bytes: bytes) -> list[GPGKeyRecord]:
    """
    Import *key_bytes* into a throwaway GPGME home directory and return records.

    `gpg.Context().keylist(source=...)` silently returns no keys on GPGME 1.18
    (GnuPG 2.2/2.4 as shipped in Debian bookworm through forky and Ubuntu noble
    through resolute). Importing into an isolated temporary homedir and then
    calling `keylist()` without a source argument is reliable across all
    tested versions.
    """
    with tempfile.TemporaryDirectory() as home:
        os.chmod(home, 0o700)
        with gpg.Context(home_dir=home) as ctx:
            ctx.key_import(key_bytes)
            return _parse_gpg_keys(ctx.keylist())


def read_key_records(key_ref: str) -> list[GPGKeyRecord]:
    """
    Return GPGKeyRecord list for any Signed-By value.

    Supports file paths (.gpg/.asc keyrings) and inline PGP blocks embedded
    in deb822 .sources files. Fingerprint-only references cannot be resolved
    without access to the system keyring and raise NotImplementedError.

    Raises:
        FileNotFoundError     - key file path does not exist
        NotImplementedError   - fingerprint-only Signed-By
        ValueError            - unrecognised Signed-By format
        gpg.errors.GPGMEError - GPGME reported an error
    """
    kind = _classify_key_ref(key_ref)

    if kind == "file":
        if not os.path.isfile(key_ref):
            raise FileNotFoundError(f"key file not found: {key_ref!r}")
        with open(key_ref, "rb") as f:
            return _import_raw_keys(f.read())

    if kind == "inline":
        return _import_raw_keys(_normalize_inline_key(key_ref))

    if kind == "fingerprint":
        raise NotImplementedError(
            f"fingerprint-only Signed-By ({key_ref!r}) cannot be resolved "
            "without access to the system keyring"
        )

    raise ValueError(f"unrecognised Signed-By value: {key_ref!r}")


# ----------------------------------------------------------------------
# Prometheus text format helpers
# ----------------------------------------------------------------------


def _escape_label(v: str) -> str:
    """Escape a label value for the Prometheus text exposition format."""
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _label_set(labels: dict[str, str]) -> str:
    pairs = ", ".join(f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items()))
    return "{" + pairs + "}"


def _export_gauge(
    name: str, help: str, samples: list[tuple[dict[str, str], int]]
) -> list[str]:
    lines = [f"# HELP {name} {help}", f"# TYPE {name} gauge"]
    for labels, value in samples:
        lines.append(f"{name}{_label_set(labels)} {value}")
    return lines


# ----------------------------------------------------------------------
# Metrics collection
# ----------------------------------------------------------------------


def collect_metrics() -> str:
    """Collect all APT signing key metrics and return Prometheus text."""
    refs = collect_signed_by_refs()

    # Map key_ref → set of source files that reference it (for deduplication).
    key_to_sources: dict[str, set[str]] = defaultdict(set)
    for ref in refs:
        key_to_sources[ref.key_ref].add(ref.source_file)

    expire_samples: list[tuple[dict[str, str], int]] = []
    error_samples: list[tuple[dict[str, str], int]] = []

    for key_ref, source_files in sorted(key_to_sources.items()):
        kind = _classify_key_ref(key_ref)
        key_file_label = key_ref if kind == "file" else "<inline>"

        try:
            key_records = read_key_records(key_ref)
        except NotImplementedError as err:
            # Fingerprint-only not supported
            print(f"Info: skipping unsupported Signed-By: {err}", file=sys.stderr)
            continue
        except Exception as err:
            short_reason = str(err)[:200]
            for source_file in sorted(source_files):
                error_samples.append(
                    (
                        {
                            "source_file": source_file,
                            "key_file": key_file_label,
                            "reason": short_reason,
                        },
                        1,
                    )
                )
            print(f"Warning: {err}", file=sys.stderr)
            continue

        if not key_records:
            for source_file in sorted(source_files):
                error_samples.append(
                    (
                        {
                            "source_file": source_file,
                            "key_file": key_file_label,
                            "reason": "no keys found in file",
                        },
                        1,
                    )
                )
            continue

        for record in key_records:
            for source_file in sorted(source_files):
                labels: dict[str, str] = {
                    "source_file": source_file,
                    "key_file": key_file_label,
                    "fingerprint": record.fingerprint,
                    "uid": record.uid,
                    "key_type": record.key_type,
                }
                expire_samples.append((labels, record.expires))

    output_lines: list[str] = []

    if expire_samples:
        output_lines.extend(
            _export_gauge(
                "apt_signing_key_expire_time_seconds",
                "Unix timestamp when the APT signing key expires"
                " (0 means the key never expires).",
                expire_samples,
            )
        )
        output_lines.append("")

    if error_samples:
        output_lines.extend(
            _export_gauge(
                "apt_signing_key_read_error",
                "1 if there was an error reading or parsing the signing key file.",
                error_samples,
            )
        )
        output_lines.append("")

    return "\n".join(output_lines)


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description=(
            "Write Prometheus metrics for APT signing key expiry dates to a file "
            "for consumption by node_exporter's textfile collector."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--output",
        "-o",
        default="-",
        metavar="FILE",
        help='Output file path, or "-" for stdout.',
    )
    p.add_argument(
        "--version", "-v", action="version", version=f"%(prog)s {__version__}"
    )

    args = p.parse_args()
    text = collect_metrics()

    if args.output == "-":
        sys.stdout.write(text)
    else:
        # Write atomically: write to a sibling temp file then rename, so that
        # node_exporter never reads a partially-written .prom file.
        out_path = os.path.abspath(args.output)
        out_dir = os.path.dirname(out_path)
        pid = os.getpid()
        tmp_fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=f".{pid}")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(text)
            os.replace(tmp_path, out_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
