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

if ! command -v wslpath >/dev/null 2>&1; then
    printf '%s\n' "ERROR: required command 'wslpath' was not found in PATH."
    exit 127
fi

if ! command -v python.exe >/dev/null 2>&1; then
    printf '%s\n' "ERROR: required Windows python.exe was not found in PATH."
    exit 127
fi

WINDOWS_PYTHON="$(readlink -f -- "$(command -v python.exe)")"
case "$WINDOWS_PYTHON" in
    /mnt/[a-zA-Z]/*) ;;
    *)
        printf '%s\n' "ERROR: required Windows python.exe is invalid."
        exit 127
        ;;
esac

WINDOWS_PYTHON_PATH="$(wslpath -w "$WINDOWS_PYTHON")"
if [[ "${#WINDOWS_PYTHON_PATH}" -lt 4 \
    || ! "${WINDOWS_PYTHON_PATH:0:1}" =~ ^[a-zA-Z]$ \
    || "${WINDOWS_PYTHON_PATH:1:1}" != ":" \
    || "${WINDOWS_PYTHON_PATH:2:1}" != "\\" ]]; then
    printf '%s\n' "ERROR: required Windows python.exe is invalid."
    exit 127
fi

if [[ "$(head -c 2 -- "$WINDOWS_PYTHON")" != "MZ" ]]; then
    printf '%s\n' "ERROR: required Windows python.exe is invalid."
    exit 127
fi

if ! PYTHON_PROBE="$("$WINDOWS_PYTHON" -S -c 'import sys; print(sys.platform); print(sys.version_info.major); print(sys.version_info.minor)')"; then
    printf '%s\n' "ERROR: required Windows python.exe is invalid."
    exit 127
fi
PYTHON_PROBE="${PYTHON_PROBE//$'\r'/}"
mapfile -t PYTHON_FIELDS <<< "$PYTHON_PROBE"
if [[ "${#PYTHON_FIELDS[@]}" -ne 3 \
    || "${PYTHON_FIELDS[0]}" != "win32" \
    || ! "${PYTHON_FIELDS[1]}" =~ ^[0-9]+$ \
    || ! "${PYTHON_FIELDS[2]}" =~ ^[0-9]+$ \
    || "${PYTHON_FIELDS[1]}" -lt 3 \
    || ( "${PYTHON_FIELDS[1]}" -eq 3 && "${PYTHON_FIELDS[2]}" -lt 11 ) ]]; then
    printf '%s\n' "ERROR: required Windows python.exe is invalid."
    exit 127
fi

"$PROJECT_ROOT/.venv/bin/xrag" --root "$PROJECT_ROOT" dashboard update --no-publish

WINDOWS_WRAPPER="$(wslpath -w "$PROJECT_ROOT/scripts/publish-dashboard.py")"
if [[ "${#WINDOWS_WRAPPER}" -lt 4 \
    || ! "${WINDOWS_WRAPPER:0:1}" =~ ^[a-zA-Z]$ \
    || "${WINDOWS_WRAPPER:1:1}" != ":" \
    || "${WINDOWS_WRAPPER:2:1}" != "\\" ]]; then
    printf '%s\n' "ERROR: dashboard publisher path is invalid."
    exit 127
fi
exec "$WINDOWS_PYTHON" -S "$WINDOWS_WRAPPER"
