#!/usr/bin/env bash
# Installs the udev rule so the Avantes spectrometer is accessible over USB
# before it gets passed into the Docker container. Linux only — run once per
# host, from the repo root (or anywhere, it locates its own files).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
RULE_SRC="$SCRIPT_DIR/99-avantes.rules"
RULE_DST="/etc/udev/rules.d/99-avantes.rules"

if [ ! -f "$RULE_SRC" ]; then
    echo "Error: $RULE_SRC not found (expected next to this script)." >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Root privileges needed, re-running with sudo..."
    exec sudo "$0" "$@"
fi

cp "$RULE_SRC" "$RULE_DST"
udevadm control --reload-rules
udevadm trigger

echo "Installed $RULE_DST"
echo "Unplug and replug the spectrometer for the rule to take effect."
