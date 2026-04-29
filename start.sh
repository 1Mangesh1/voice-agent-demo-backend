#!/usr/bin/env bash
# Single-process start for Render free tier (no background workers).
# Runs the LiveKit agent worker in the background, then FastAPI in the
# foreground so Render's port-binding healthcheck passes.
set -euo pipefail

# Forward signals so both processes shut down together.
trap 'kill -TERM $AGENT_PID 2>/dev/null; exit 0' TERM INT

python agent.py start &
AGENT_PID=$!

exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}"
