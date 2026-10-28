#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/yandex-calendar-mcp" "$@"
