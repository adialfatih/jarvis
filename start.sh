#!/usr/bin/env bash
cd "$(dirname "$0")/backend"
PORT="${JARVIS_PORT:-8300}"
exec ./.venv/bin/uvicorn main:app --host 0.0.0.0 --port "$PORT"
