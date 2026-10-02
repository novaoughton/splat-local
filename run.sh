#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[[ -f env.sh ]] && source env.sh
exec "${UV_PROJECT_ENVIRONMENT:-.venv}/bin/python" -m uvicorn server.main:app --host 127.0.0.1 --port "${PORT:-8000}"
