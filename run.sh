#!/bin/bash
export CROSSREF_EMAIL="${CROSSREF_EMAIL:-chois@umn.edu}"
exec /data2/chois/.claude/mcp-servers/refcheck/.venv/bin/python -m refcheck
