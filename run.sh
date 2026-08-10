#!/usr/bin/env bash
# Start the compression server. Creates the virtualenv on first run.
set -euo pipefail

cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-4321}"

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -r requirements.txt
fi

if ! command -v gs >/dev/null 2>&1; then
  echo "Note: Ghostscript not found - PDF compression will use the weaker Python fallback."
  echo "      Install it with:  sudo apt install ghostscript"
fi

echo "Serving on http://${HOST}:${PORT}"
exec ./.venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
