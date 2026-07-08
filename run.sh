#!/bin/bash
# Portable launcher for the refcheck MCP server.
# Resolves everything relative to this script, so it works
# from any checkout location and for any user.
set -euo pipefail

# Directory containing this script, regardless of where it
# is invoked from.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

# Prefer the project virtual environment if present; otherwise
# fall back to whatever python3 is on PATH.
if [ -x ".venv/bin/python" ]; then
    python_bin="./.venv/bin/python"
else
    python_bin="python3"
fi

# Default Crossref email for the polite pool, overridable by
# the environment or a .env file loaded by the server.
export CROSSREF_EMAIL="${CROSSREF_EMAIL:-you@example.com}"

exec "$python_bin" -m refcheck
