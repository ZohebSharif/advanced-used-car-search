#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT"

INSTALL_BROWSER=false
case "${1:-}" in
  "") ;;
  --browser) INSTALL_BROWSER=true ;;
  -h|--help)
    echo "Usage: ./setup.sh [--browser]"
    echo "Install locked dependencies and run the offline demo; no API key needed."
    echo "Add --browser to install Chromium for optional live research."
    exit 0
    ;;
  *) echo "Unknown option: $1. Use ./setup.sh --help." >&2; exit 2 ;;
esac
if [[ $# -gt 1 ]]; then
  echo "Too many arguments. Use ./setup.sh --help." >&2
  exit 2
fi

command -v uv >/dev/null 2>&1 || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
else
  echo "Keeping existing .env"
fi

mkdir -p logs evidence reports
uv sync --locked
uv run --no-sync python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "Python 3.11+ is required")'
uv run --no-sync python -c 'from pathlib import Path; import advanced_used_car_search.config as config; source=(Path.cwd() / "package" / "advanced_used_car_search" / "config.py").resolve(); installed=Path(config.__file__).resolve(); assert installed != source; assert installed.read_bytes() == source.read_bytes()'

if [[ "$INSTALL_BROWSER" == true ]]; then
  uv run --no-sync playwright install chromium
fi

MODEL_ENABLED=false uv run --no-sync advanced-used-car-search dry-run
echo
echo "Setup complete. Your sample report is ready:"
echo "  uv run advanced-used-car-search report --mode fixture"
echo "For live research, install Chromium with ./setup.sh --browser, then run:"
echo "  uv run advanced-used-car-search doctor"
