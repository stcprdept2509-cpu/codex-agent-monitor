#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

URL="http://localhost:${PORT:-8799}/"
echo "Agent Monitor"
echo "Opening ${URL}"
echo

python3 server.py &
SERVER_PID=$!

sleep 1
open "${URL}"

wait "${SERVER_PID}"
