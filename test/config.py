"""Shared configuration for create-images.py and run-tests.py."""

IMAGE_PREFIX = "apt-sources-test"

# Ordered mapping of distro name → base Docker image.
# Insertion order defines the default build/test sequence.
DISTROS: dict[str, str] = {
    "bookworm": "debian:bookworm-slim",
    "trixie": "debian:trixie-slim",
    "forky": "debian:forky-slim",
    "noble": "ubuntu:noble",
    "resolute": "ubuntu:resolute",
}
