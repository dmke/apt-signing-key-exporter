#!/bin/sh
set -e

# postinst configure [old-version]  — called on fresh install and on upgrade.
# postinst abort-upgrade <new-version> — called when an upgrade failed and
#                                         the old package is being reinstated.

case "$1" in
configure|abort-upgrade)
	# Only interact with systemd if PID 1 is actually systemd (i.e. not
	# inside a chroot or a container without systemd).
	if [ -d /run/systemd/system ]; then
		systemctl daemon-reload

		# Enable the timer (idempotent) and start it if not already active.
		systemctl enable apt-signing-key-exporter.timer
		systemctl start apt-signing-key-exporter.timer || true

		# Run the service once immediately so that metrics are available
		# straight after installation without waiting for the daily timer.
		systemctl start apt-signing-key-exporter.service || true
	fi
	;;
esac
