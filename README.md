# apt-signing-key-exporter

A [Prometheus][] [node_exporter][] [textfile-collector][textfile] generator
script that reads all APT source list files on a Debian or Ubuntu host,
identifies the signing keys referenced by each source entry, and writes each
key's expiration date as a gauge metric.

Intended to give advance warning before a signing key expires and breaks
`apt-get update`.

[Prometheus]: https://prometheus.io/


## How it works

The script is a single-shot process - it runs, writes a `.prom` file, and
exits. A systemd timer fires it once a day. The `.prom` file is picked up
by [node_exporter's textfile collector][textfile].

Source entries are read via `python3-apt`. Both the traditional one-line
format (`.list` files) and the modern deb822 format (`.sources` files) are
supported, including inline PGP key blocks embedded in `.sources` files.
Keys are inspected via `python3-gpg` (GPGME bindings).

[textfile]: https://github.com/prometheus/node_exporter#textfile-collector


## Metrics

| Metric | Type | Description | Labels |
|:-------|:-----|:------------|:-------|
| `apt_signing_key_expire_time_seconds`  | gauge | Unix timestamp when the key expires; `0` = never expires | `source_file`, `key_file`, `fingerprint`, `uid`, `key_type` (`pub` or `sub`) |
| `apt_signing_key_read_errors`          | gauge | Number of key files that could not be read or parsed (full error logged to stderr) | _(none)_ |

Example output:

```
# HELP apt_signing_key_expire_time_seconds Unix timestamp when the APT signing key expires (0 means the key never expires).
# TYPE apt_signing_key_expire_time_seconds gauge
apt_signing_key_expire_time_seconds{fingerprint="...",key_file="/usr/share/keyrings/docker.gpg",key_type="pub",source_file="/etc/apt/sources.list.d/docker.sources",uid="Docker Release (CE deb) <docker@docker.com>"} 0
```


## Requirements

- Debian 12 (bookworm) or later, or Ubuntu 24.04 (noble) or later
- `python3`
- `python3-apt`
- `python3-gpg`
- [node_exporter][] with the textfile collector enabled

[node_exporter]: https://github.com/prometheus/node_exporter


## Installation

### From a .deb package (recommended)

The package is built with [nfpm][]. Install nfpm, then:

```console
$ python3 build/build.py
# dpkg -i build/apt-signing-key-exporter_*.deb
```

The package installs:

- `/usr/bin/apt_signing_key_exporter` - the exporter script
- `/lib/systemd/system/apt-signing-key-exporter.service` - oneshot service unit
- `/lib/systemd/system/apt-signing-key-exporter.timer` - daily timer unit
- `/etc/default/apt-signing-key-exporter` - configuration

The postinstall script runs `systemctl daemon-reload`, enables, and starts
the timer. The service is also started immediately so metrics are available
right away without waiting for midnight.

[nfpm]: https://nfpm.goreleaser.com/


### Manual installation

Download the [apt_signing_key_exporter.py](./src/apt_signing_key_exporter.py)
(or clone this repository), then run:

```console
# apt install python3-apt python3-gpg
# install -m 0755 /path/to/apt_signing_key_exporter.py /usr/bin/apt_signing_key_exporter
```

Then run it directly or set up your own cron job / systemd unit.


## Configuration

Edit `/etc/default/apt-signing-key-exporter`:

```sh
# Path to the output file for node_exporter's textfile collector.
APT_SIGNING_KEY_EXPORTER_OUTPUT=/var/lib/prometheus/node-exporter/apt-signing-key-exporter.prom
```

The default path matches the location used by the Debian
`prometheus-node-exporter` package. Adjust if your node_exporter watches a
different directory.


## Usage

```
apt_signing_key_exporter [--output FILE]

  --output FILE, -o FILE   Write metrics to FILE (default: stdout)
  --version                Print version and exit
```

Write to stdout (useful for testing):

```console
$ /usr/bin/apt_signing_key_exporter
```

Write to the textfile collector directory:

```console
$ /usr/bin/apt_signing_key_exporter \
    -o /var/lib/prometheus/node-exporter/apt-signing-key-exporter.prom
```

Output is written atomically (write to a sibling temp file, then `rename(2)`)
so node_exporter never reads a partially-written file.


## Limitations

- **Fingerprint-only `Signed-By`** values (a 40-hex-char fingerprint rather
  than a path to a key file) are skipped with an informational log message.
  Resolving these would require reading the system keyring, which is outside
  the scope of this tool.
- Keys that are referenced by a source entry but whose file does not exist
  increment the `apt_signing_key_read_errors` counter; the full error message
  is written to stderr.


## License

MIT - see [LICENSE.md](LICENSE.md).
