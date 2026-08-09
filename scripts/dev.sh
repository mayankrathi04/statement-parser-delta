#!/usr/bin/env bash
# Development mode: two processes.
#
#   FastAPI  http://127.0.0.1:8770   the API, auto-reloading on Python changes
#   Vite     http://127.0.0.1:5173   the UI, hot-reloading on React changes
#
# Open the VITE url — it proxies /api through to FastAPI (see vite.config.ts).
# `npm run dev` on its own only starts Vite, so every /api call 404s until the
# Python side is up too; this script starts both and stops both together.
#
# For production there is only one process: `npm run build` compiles the UI into
# sparser/web/dist and `python -m sparser serve` serves API and UI on :8770.
set -euo pipefail
cd "$(dirname "$0")/.."

DB="${SPARSER_DB:-statements.db}"
PY="${PYTHON:-.venv/bin/python}"

# uvicorn's own "Address already in use" says nothing about who holds the port,
# and a stale backend from a previous run is the usual culprit.
if ss -ltn "sport = :8770" 2>/dev/null | grep -q LISTEN; then
  echo "Port 8770 is already in use — probably a backend from an earlier run:" >&2
  pgrep -af "sparser serve|sparser.api:app" >&2 || true
  echo "Stop it with:  pkill -f 'sparser serve'" >&2
  exit 1
fi

# Stop only the backend this invocation starts. `kill 0` is intentionally not
# used: it targets the whole process group and can kill the user's parent shell
# when an early check (such as the occupied port above) exits.
API_PID=""
cleanup() {
  trap - EXIT INT TERM
  if [ -n "$API_PID" ] && kill -0 "$API_PID" 2>/dev/null; then
    kill "$API_PID" 2>/dev/null || true
    # Uvicorn's reload supervisor can occasionally ignore TERM while a worker
    # is restarting. Give it two seconds, then stop only that exact PID.
    for _ in {1..20}; do
      kill -0 "$API_PID" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$API_PID" 2>/dev/null; then
      kill -KILL "$API_PID" 2>/dev/null || true
    fi
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "api  → http://127.0.0.1:8770/docs   (db: $DB)"
"$PY" -m sparser serve --db "$DB" --port 8770 --no-browser --reload &
API_PID=$!

cd webapp
[ -d node_modules ] || npm install
echo "ui   → http://127.0.0.1:5173   ← open this one"
npm run dev -- --port 5173

wait
