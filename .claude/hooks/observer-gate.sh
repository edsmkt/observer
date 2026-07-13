#!/usr/bin/env bash
# Claude Code PreToolUse hook: force Observer Kit on side-effect scripts
# that are not already under the harness / observer-kit run.
#
# stdin: Claude Code hook JSON
# stdout: optional PreToolUse deny decision JSON
set -euo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-}"
if [[ -z "$ROOT" ]]; then
  ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
fi

export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -B -m observer_kit.gate --hook
