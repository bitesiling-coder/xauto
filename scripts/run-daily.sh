#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

export PATH="$HOME/.local/bin:$PATH"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

mkdir -p -- "$PROJECT_ROOT/logs"
exec >> "$PROJECT_ROOT/logs/scheduler.log" 2>&1
printf '\n=== X-RAG scheduled collection start: %s ===\n' "$(date --iso-8601=seconds)"

if ! command -v opencli >/dev/null 2>&1; then
    printf '%s\n' "ERROR: required command 'opencli' was not found in PATH."
    exit 127
fi

if ! command -v python.exe >/dev/null 2>&1; then
    printf '%s\n' "ERROR: required command 'python.exe' was not found in PATH."
    exit 127
fi

if ! command -v wslpath >/dev/null 2>&1; then
    printf '%s\n' "ERROR: required command 'wslpath' was not found in PATH."
    exit 127
fi

"$PROJECT_ROOT/.venv/bin/xrag" --root "$PROJECT_ROOT" dashboard update --no-publish

WINDOWS_ROOT="$(wslpath -w "$PROJECT_ROOT")"
WINDOWS_WRAPPER="$(wslpath -w "$PROJECT_ROOT/scripts/publish-dashboard.py")"
exec python.exe "$WINDOWS_WRAPPER" --root "$WINDOWS_ROOT"
