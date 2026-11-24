#!/usr/bin/env bash
# Entry point for the tag-driven release runner: deploy/release.sh VERSION
set -euo pipefail
exec python3 "$(dirname "$0")/release.py" "$@"
