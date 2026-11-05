#!/usr/bin/env bash
# Entry point for the BUILD office's release recipe: deploy/release.sh VERSION
set -euo pipefail
exec python3 "$(dirname "$0")/release.py" "$@"
