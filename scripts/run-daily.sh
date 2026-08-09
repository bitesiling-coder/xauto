#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

mkdir -p -- "$PROJECT_ROOT/logs"
exec "$PROJECT_ROOT/.venv/bin/xrag" --root "$PROJECT_ROOT" collect --all >> "$PROJECT_ROOT/logs/scheduler.log" 2>&1
