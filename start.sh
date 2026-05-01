#!/usr/bin/env bash
set -euo pipefail
exec uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}"
