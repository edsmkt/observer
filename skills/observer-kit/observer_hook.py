#!/usr/bin/env python3
"""Claude Code PostToolUse hook (Bash matcher) — reminds the agent to start a run's
chat watcher when a run begins.

runguard.ledger(scope, 'run_started', ...) prints `OBSERVER_RUN_STARTED <run_id>` to
stderr. This hook reads the PostToolUse JSON on stdin, and if that marker is in the
tool output, injects a reminder telling the agent to start the RUN-SCOPED watcher
(`watch_chat.py <run_id>`) — so operator dashboard notes reach that session and no other.

Wire it in settings.json (see the observer-kit SKILL). Silent (exit 0, no output) when
no marker is present, so it's cheap on every Bash call.

Caveat: PostToolUse fires when the Bash call returns, so it catches FOREGROUND launches
reliably; a background launch's marker may not be in the immediate output. The SKILL's
"start the watcher on launch" instruction remains the primary path; this is a backstop.
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
    f"A run just started: {rid}. To receive the operator's dashboard notes for THIS run "
    f"(and not wake other sessions), start its run-scoped watcher now — set up a Monitor on "
    f"`python3 watch_chat.py {rid}` (add --state-dir <ledger dir> if it isn't ./.runguard). "
    f"Skip if you already started a watcher for this run."
)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))
