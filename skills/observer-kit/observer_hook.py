#!/usr/bin/env python3
"""Claude Code PostToolUse hook (Bash matcher) — reports watcher ownership when
a run begins.

runguard.ledger(scope, 'run_started', ...) prints `OBSERVER_RUN_STARTED <run_id>` to
stderr. This hook reads the PostToolUse JSON on stdin, and if that marker is in the
tool output, it reminds the agent that `observer-kit run` creates or reuses the
run-scoped watcher and gives the direct-launch fallback.

Wire it in settings.json (see the observer-kit SKILL). Silent (exit 0, no output) when
no marker is present, so it's cheap on every Bash call.

Caveat: PostToolUse fires when the Bash call returns, so it catches foreground launches
reliably; a background launch's marker may arrive later.
"""
import sys
import json
import re

try:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
except Exception:
    sys.exit(0)

# The marker will be somewhere in the tool output regardless of exact field name.
blob = json.dumps(data, ensure_ascii=False)
m = re.search(r'OBSERVER_RUN_STARTED (runguard:[^\s"\\]+)', blob)
if not m:
    sys.exit(0)

rid = m.group(1)
msg = (
    f"A run just started: {rid}. `observer-kit run` creates or reuses one run-scoped "
    f"watcher automatically. Inspect ownership with `observer-kit watch <state-dir> --status`. "
    f"For a direct worker launch with no watcher owner, start one Monitor on "
    f"`python3 watch_chat.py {rid} --state-dir <state-dir> --follow`."
)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))
