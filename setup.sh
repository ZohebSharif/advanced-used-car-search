#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT"

command -v uv >/dev/null 2>&1 || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
else
  echo "Keeping existing .env"
fi

mkdir -p logs evidence reports
uv sync --frozen --extra dev --reinstall-package lexus-hunter
uv run --no-sync python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required")'

if ! uv run python -c 'from pathlib import Path; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); ok=Path(p.chromium.executable_path).exists(); p.stop(); raise SystemExit(0 if ok else 1)'; then
  uv run playwright install chromium
fi

MODEL_ENABLED=false uv run lexus-hunter dry-run >/dev/null
echo "Setup complete. Run: uv run lexus-hunter doctor"
