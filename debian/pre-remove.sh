#!/bin/sh
set -e

# prerm remove        — called before the package is removed.
# prerm upgrade <ver> — called before an upgrade; do NOT stop the timer here
#                       because postinst will restart it after the new files
#                       are in place.

case "$1" in
remove|deconfigure)
	if [ -d /run/systemd/system ]; then
		# Stop and disable the timer; ignore failures (e.g. already stopped).
		systemctl stop  apt-signing-key-exporter.timer   || true
		systemctl stop  apt-signing-key-exporter.service || true
		systemctl disable apt-signing-key-exporter.timer || true
	fi
	;;
esac
