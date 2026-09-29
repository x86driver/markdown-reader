#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if command -v uv >/dev/null 2>&1; then
    exec uv run --locked markdown-finder "$@"
fi
if [[ -x .venv/bin/markdown-finder ]]; then
    exec .venv/bin/markdown-finder "$@"
fi
printf '%s\n' '請先安裝 uv 並執行 uv sync --locked，或在 .venv 安裝此專案。' >&2
exit 1
