#!/usr/bin/env bash
# Tear down the floor2 stack and start it again with fresh builds.
# The database volume is kept. Use `docker compose -p floor2 down -v` to wipe it.
#
# Usage: ./restart.sh   (works from any directory)

set -euo pipefail

# Run from the folder this script is in, where compose.yaml lives.
cd "$(dirname "$0")"

echo "==> Stopping containers"
docker compose -p floor2 down

echo "==> Rebuilding and starting"
docker compose -p floor2 up -d --build

echo "==> Status"
docker compose -p floor2 ps
echo
echo "Web page: http://localhost:8000"
