#!/bin/sh
# Open the authenticated local dashboard from this source folder.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' 'This source-folder launcher needs uv. Use your existing ApplyPilot Python environment, or install uv first.' >&2
    exit 1
fi
exec uv run applypilot dashboard
