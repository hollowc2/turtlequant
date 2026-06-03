#!/usr/bin/env bash
# One-time VPS layout for TurtleQuant (run with sudo if needed).
set -euo pipefail

STATE_DIR=/opt/turtlequant/state
DATA_DIR=/opt/turtlequant/data

mkdir -p "$STATE_DIR" "$DATA_DIR"
chmod 755 /opt/turtlequant "$STATE_DIR" "$DATA_DIR"

if ! docker network inspect monitoring_net >/dev/null 2>&1; then
  echo "Creating Docker network monitoring_net..."
  docker network create monitoring_net
else
  echo "Docker network monitoring_net already exists."
fi

echo "Host dirs ready:"
ls -ld /opt/turtlequant "$STATE_DIR" "$DATA_DIR"
