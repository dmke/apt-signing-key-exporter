# Hacking guide

This document describes how the code works and how the test suite is
structured.


## How the code works

The entry point is `src/apt_signing_key_exporter.py`. It is a single Python
file with no third-party dependencies beyond `python3-apt` and `python3-gpg`,
both of which are standard packages on Debian and Ubuntu.

The execution path is:

```
if __name__ == "__main__"
  +-- collect_metrics()
      +-- collect_signed_by_refs()   — parse APT source lists
      +-- read_key_records()         — inspect each key with GPGME
      +-- _export_gauge()           — format Prometheus text
```


### Stage 1: source list parsing (`collect_signed_by_refs`)

`python3-apt`'s `aptsources.sourceslist.SourcesList` is used to enumerate all
active source entries. Disabled (commented-out) entries are skipped.

Two entry types are handled:

- **`Deb822SourceEntry`** (`.sources` files, deb822 format): the `Signed-By`
  field is read directly from the `apt_pkg.TagSection` via `entry.section.get`.
  Its value may be a file path, a 40-hex-character fingerprint, or a full
  inline PGP armored block (multi-line, with continuation-line spaces).

- **`SourceEntry`** (`.list` files, traditional one-line format): the raw
  `entry.line` string is searched for a `signed-by=...` option with a regex.

The `deb822=True` keyword argument to `SourcesList.__init__` (required to load
`.sources` files) was added in python3-apt 2.x. The script detects its
presence at runtime via `inspect.signature` and falls back gracefully on older
releases.

Deduplication is applied: if the same `(source_file, key_ref)` pair appears
more than once (e.g. because several entries in the same file reference the
same keyring), it is emitted only once.


### Stage 2: key inspection (`read_key_records`, `_import_raw_keys`)

Each unique `Signed-By` value is classified by `_classify_key_ref`:

| Classification | Example value |
|:---------------|:--------------|
| `file`         | `/usr/share/keyrings/docker.gpg` |
| `inline`       | `-----BEGIN PGP PUBLIC KEY BLOCK-----\n ...` |
| `fingerprint`  | `9DC858229FC7DD38854AE2D88D81803C0EBFCD88` |

File and inline keys are passed to `_import_raw_keys`, which:

1. Creates an isolated temporary GNUPG home directory (`tempfile.TemporaryDirectory`, mode `0700`).
2. Opens a `gpg.Context(home_dir=...)` pointed at it.
3. Calls `ctx.key_import(raw_bytes)` to import the key material.
4. Calls `ctx.keylist()` (without a `source=` argument) to enumerate the imported keys.

**Why not `ctx.keylist(source=path)`?**  On GPGME 1.18 (shipped in all tested
distros: Debian bookworm through forky, Ubuntu noble through resolute),
`keylist(source=...)` silently returns an empty iterator regardless of the key
content. The `key_import` + `keylist` pattern is reliable across all tested
versions.

**Inline key normalization (`_normalize_inline_key`):** In deb822 files,
continuation lines carry a mandatory leading space, and blank lines within a
multi-line value are encoded as a single ` .` (space + dot). Before passing
the block to GPGME, `_normalize_inline_key` strips exactly one leading space
per line and converts ` .` back to an empty line (the PGP armor blank-line
separator between the header and base64 data).

Fingerprint-only `Signed-By` values are not supported (they would require
reading the system keyring) and are skipped with an informational log message.

For each imported key, `_parse_gpg_keys` walks `key.subkeys` — a flat
list where index 0 is the primary key and subsequent entries are subkeys — and
produces one `GPGKeyRecord` per (sub)key. Each record carries the full
fingerprint, first valid UID, `key_type` (`pub`/`sub`), expiry Unix timestamp,
whether the key can sign (`can_sign`), and whether it is currently usable
(`valid` — not expired, revoked, invalid, or disabled). An expiry of `0` means
the key never expires.

`_governing_signing_key` then collapses those records to the single key that
determines when `apt` can no longer verify a repository. `apt` accepts any
signing-capable key in the keyring, so encryption- or auth-only subkeys are
irrelevant and an expired signing subkey does not matter while a newer one is
still valid. It returns a still-valid signer that never expires if present,
otherwise the valid signer with the latest expiry, otherwise (no valid signer
left) the most-recently-expired signer so its past timestamp still trips alerts.
`None` is returned — and counted as a read error — when no signing-capable key
exists at all.


### Stage 3: Prometheus text formatting

`collect_metrics` groups records by key reference, iterates in sorted order
for deterministic output, and builds two metric families:

- `apt_signing_key_expire_time_seconds` — expiry timestamp of the governing
  signing key, one sample per keyring (see `_governing_signing_key` above)
- `apt_signing_key_read_errors` — total number of key files that could not be
  read or parsed (label-free gauge, always emitted, even when `0`); the full
  error message for each failure is written to stderr

Integer timestamps are emitted as plain integers.

When writing to a file, the output is written to a sibling temp file in the
same directory and then renamed into place with `os.replace`, so node_exporter
never reads a partially-written `.prom` file.


## How to test


### Test infrastructure overview

Tests run inside local Docker images that have the necessary Python packages
and all test fixtures pre-baked in. The exporter is not baked into the
images; instead, a fresh `.deb` is built from the current working tree and
installed inside each container at test time. This gives end-to-end coverage
of both the script and the packaging.

Five distros are tested:

| Tag | Base image |
|:----|:-----------|
| `apt-sources-test:bookworm` | `debian:bookworm-slim` |
| `apt-sources-test:trixie`   | `debian:trixie-slim` |
| `apt-sources-test:forky`    | `debian:forky-slim` |
| `apt-sources-test:noble`    | `ubuntu:noble` |
| `apt-sources-test:resolute` | `ubuntu:resolute` |

Each image has:
- `python3`, `python3-apt`, `python3-gpg` installed
- All distro APT sources removed (so only the test fixtures are visible)
- Test keyrings baked into `/usr/share/keyrings/apt-sources-test/`
- Test source files baked into `/etc/apt/sources.list.d/`


### Test fixtures

`test/keyrings/` contains four static GPG public key pairs:

| File stem | Primary key | Expiry |
|:----------|:------------|:-------|
| `test-no-expiry`     | `536D4567B236D5EFF8151915A6DE716A2CE10440` | never |
| `test-future-expiry` | `B338DDF2D03421B411BA4A022CEA661F48AFCBD3` | 2030-06-01 |
| `test-past-expiry`   | `0A9FF69850E08A5A71C87467FF91AA56DB400815` | 2021-01-01 (already expired) |
| `test-superseded`    | `DD1137EED39A7AD01A4A948A2F9129A3DCEAF0C4` | never (see below) |

`test-superseded` reproduces the real-world "caddy.asc" shape: a signing-capable
primary that never expires, an **expired encryption** subkey, an **expired
(superseded) signing** subkey, and a **valid non-expiring signing** subkey.
apt verifies fine with the still-valid keys, so the exporter must collapse the
keyring to the never-expiring signer (`expires=0`) and must not emit rows for
the two expired subkeys — the regression test for that behaviour lives in
`run-tests.py` (`SUPERSEDED_EXPIRED_SUBKEYS`).

Each key is exported in both binary (`.gpg`) and ASCII-armored (`.asc`) form.
Private keys are never committed; they exist only in a temporary directory
during key generation and are deleted on exit.

`test/sources/` contains five source fixtures that exercise different code
paths:

| File | What it tests |
|:-----|:--------------|
| `test-file.sources`        | deb822 format with `Signed-By: /path/to/keyfile` |
| `test-legacy.list`         | traditional one-line format; includes a commented-out (disabled) entry |
| `test-past-expiry.sources` | deb822 with an already-expired key |
| `test-superseded.list`     | keyring with expired encryption / superseded signing subkeys alongside a valid signer (`.list` so all distros, incl. old python3-apt, load it) |
| `test-inline.sources`      | deb822 with the full PGP block inlined via `Signed-By:` |


### Workflow

#### 1. Build the test images (once, or after changing fixtures)

```console
$ test/create-images.py
```

This builds all five images in parallel. Individual distros can be specified
as arguments:

```console
$ test/create-images.py bookworm noble
```

Additional flags:

- `--no-pull` — skip pulling base images (use local cache)
- `--no-cache` — force a full Docker layer rebuild

The images take roughly 60-90 seconds to build (dominated by `apt-get install`
inside each container).


#### 2. Run the tests

```console
$ test/run-tests.py
```

This calls `python3 build/build.py` to stamp the git version and build a fresh
`.deb` from the current working tree using `nfpm`, then for each distro:

1. Starts a fresh container from the pre-built test image.
2. Bind-mounts the `.deb` read-only into the container at `/tmp/apt-signing-key-exporter.deb`.
3. Runs `dpkg -i /tmp/...deb`. The postinstall script's `systemctl` calls are
   silently skipped because `/run/systemd/system` does not exist in a plain
   Docker container.
4. Runs `/usr/bin/apt_signing_key_exporter` and captures stdout/stderr.
5. Asserts on the output: metric names present, expected fingerprints present,
   disabled entry absent, `apt_signing_key_read_errors 0` present.

The `.deb` temp file is removed automatically on exit via `try/finally`.

To test a single distro:

```console
$ test/run-tests.py bookworm
```


#### 3. Regenerate test keys (maintenance only)

The test keys are static and committed to the repository. Regenerate them
only if the existing keys are lost or if the fixture set needs to change:

```console
$ test/update-keys.py
```

This creates four fresh keys, exports them to `test/keyrings/`, and rewrites
`test/sources/test-inline.sources` with the new no-expiry key embedded.
Private keys are held in a `tempfile.TemporaryDirectory` and deleted on exit.

**After regenerating keys**, the expected fingerprints hard-coded in
`test/run-tests.py` (`FINGERPRINTS` dict, plus `SUPERSEDED_EXPIRED_SUBKEYS` for
the superseded key's two expired subkeys) must be updated to match the new
values, which are printed by `update-keys.py` at the end of its run. The test
images must also be rebuilt (`python3 test/create-images.py`) because the
keyrings are baked into the images.
