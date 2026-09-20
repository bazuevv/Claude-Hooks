#!/usr/bin/env bash
# Compatibility entry point; the Python hook handles both installation scopes.
HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
exec bash "$HOOK_DIR/_run.sh" bypass-check.py "$@"
