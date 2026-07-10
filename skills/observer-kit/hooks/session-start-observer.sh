#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# SessionStart hook for Observer Kit — tells the agent about active dashboard
# watchers so it can poll for operator feedback.
#
# Deploy: add to .claude/hooks/ or .commandcode/settings.json:
#
#   {
#     "hooks": {
#       "SessionStart": [
#         { "hooks": [ { "type": "command", "command": "./.claude/hooks/session-start-observer.sh" } ] }
#       ]
#     }
#   }
#
# Without a session-start hook, the agent booting a new session has no way to
# know a watcher is already listening for dashboard notes.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Find running watch_chat.py processes and extract their run_id + state_dir.
WATCHERS=$(
  ps aux 2>/dev/null \
    | grep -v grep \
    | grep "watch_chat.py" \
    | sed -E 's/.*watch_chat\.py[[:space:]]+([^ ]+).*--state-dir[[:space:]]+([^ ]+).*/\1|\2/' \
    | sort -u \
    || true
)

if [ -z "$WATCHERS" ]; then
  CTX="No active Observer Kit dashboard watchers detected."
else
  NL=$'\n'
  LINES=""
  while IFS='|' read -r run_id state_dir; do
    run_id="${run_id%%[[:space:]]*}"
    state_dir="${state_dir%%[[:space:]]*}"
    LINES+="  - run: ${run_id}${NL}    state: ${state_dir}${NL}"
  done < <(echo "$WATCHERS")

  CTX="Active Observer Kit dashboard watchers:${NL}${LINES}${NL}"
  CTX+="To check for operator feedback:${NL}"
  CTX+="  monitor_events({ taskId: \"<watcher_task>\" })  # read new notes${NL}"
  CTX+="To send a reply:${NL}"
  CTX+="  observer-kit reply <state_dir> --run <run_id> --anchor <anchor> --text \"<reply>\"${NL}"
  CTX+="To inspect watcher ownership:${NL}"
  CTX+="  observer-kit watch <state_dir> --status${NL}"
  CTX+="New runs through observer-kit run create or reuse their watcher automatically.${NL}"
fi

jq -n --arg ctx "$CTX" '{
  suppressOutput: true,
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $ctx
  }
}'
