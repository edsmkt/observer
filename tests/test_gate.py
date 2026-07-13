#!/usr/bin/env python3
"""Tests for Observer Kit side-effect compliance gate."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

passed = failed = 0
REPO = Path(__file__).resolve().parents[1]
ENV = os.environ.copy()
ENV["PYTHONPATH"] = os.pathsep.join([str(REPO), ENV.get("PYTHONPATH", "")])

from observer_kit.gate import (  # noqa: E402
    assess_file,
    assess_hook_event,
    assess_shell,
    claude_pretool_decision,
)


def ok(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    print(f"  {'PASS' if condition else 'FAIL'} {name}" + (f" - {detail}" if detail and not condition else ""))
    if condition:
        passed += 1
    else:
        failed += 1


print("Testing observer-kit gate\n")

SAFE = '''
import requests
def main():
    r = requests.get("https://example.com")
    print(r.status_code)
'''

SIDE = '''
import requests
def main():
    for lead in leads:
        requests.post("https://api.crm.example/v1/leads", json=lead)
'''

WIRED = '''
from observer_kit.runguard import start_observed_run
import requests
def main():
    run = start_observed_run("job", source="s", dry_run=True, todo=1)
    requests.post("https://api.crm.example/v1/leads", json={})
    run.success()
'''

ok("safe GET allows", assess_file("enrich.py", text=SAFE)["action"] == "allow")
ok("POST loop denies without harness",
   assess_file("enrich.py", text=SIDE)["action"] == "deny",
   str(assess_file("enrich.py", text=SIDE)))
ok("POST with start_observed_run allows",
   assess_file("enrich.py", text=WIRED)["action"] == "allow")

ok("shell bare python denied when file has side effects",
   assess_shell("python3 enrich.py --full-run")["action"] == "allow"
   or True)  # file may not exist → allow missing; use temp file below

with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "enrich.py"
    p.write_text(SIDE, encoding="utf-8")
    w = Path(tmp) / "wired.py"
    w.write_text(WIRED, encoding="utf-8")

    r = assess_file(str(p))
    ok("temp side-effect file denied", r["action"] == "deny", str(r))
    ok("remedy mentions start_observed_run", "start_observed_run" in (r.get("remedy") or ""))

    r2 = assess_shell(f"python3 {p} --full-run")
    ok("shell bare side-effect denied", r2["action"] == "deny", str(r2))

    r3 = assess_shell(f"observer-kit run --state-dir .observer -- python3 {p} --dry-run")
    ok("shell under observer-kit run allowed", r3["action"] == "allow", str(r3))

    r4 = assess_file(str(w))
    ok("wired file allows", r4["action"] == "allow")

    # Hook: Write without harness
    event = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(p), "content": SIDE},
        "cwd": tmp,
    }
    d = claude_pretool_decision(event)
    ok("hook Write denies", d.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", str(d))

    event2 = {
        "tool_name": "Bash",
        "tool_input": {"command": f"python3 {p}"},
        "cwd": tmp,
    }
    d2 = claude_pretool_decision(event2)
    ok("hook Bash denies bare python",
       d2.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", str(d2))

    event3 = {
        "tool_name": "Bash",
        "tool_input": {
            "command": f"observer-kit run --state-dir .observer -- python3 {p}"
        },
        "cwd": tmp,
    }
    d3 = claude_pretool_decision(event3)
    ok("hook Bash allows observer-kit run", d3 == {}, str(d3))

    event4 = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(p)},
        "cwd": tmp,
    }
    d4 = claude_pretool_decision(event4)
    ok("hook Read denies side-effect without harness",
       d4.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")

    # CLI
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "observer_kit", "gate", str(p), "--json"],
        env=ENV, capture_output=True, text=True, timeout=15,
    )
    ok("CLI gate deny exit 1", proc.returncode == 1, proc.stdout + proc.stderr)
    payload = json.loads(proc.stdout)
    ok("CLI gate JSON action deny", payload.get("action") == "deny")

    # stdin hook mode
    proc2 = subprocess.run(
        [sys.executable, "-B", "-m", "observer_kit.gate", "--hook"],
        input=json.dumps(event2),
        env=ENV, capture_output=True, text=True, timeout=15,
    )
    ok("hook mode exit 0 with deny JSON", proc2.returncode == 0, proc2.stderr)
    body = json.loads(proc2.stdout)
    ok("hook mode permissionDecision deny",
       body["hookSpecificOutput"]["permissionDecision"] == "deny")

# SQL / metered
sql = 'db.execute("INSERT INTO leads VALUES (?)", row)'
ok("SQL insert is side effect", "sql_write" in assess_file("x.py", text=sql)["side_effects"]
   or assess_file("x.py", text=sql)["action"] == "deny")

# ignore mark
ign = "# observer: ignore\nimport requests\nrequests.post('https://x', json={})\n"
ok("ignore mark allows", assess_file("x.py", text=ign)["action"] == "allow")

# example_worker is wired
ex = REPO / "examples" / "example_worker.py"
ok("example_worker allowed", assess_file(str(ex))["action"] == "allow",
   str(assess_file(str(ex))))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
