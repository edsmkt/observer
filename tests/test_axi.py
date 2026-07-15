#!/usr/bin/env python3
"""Acceptance tests for observer-kit axi (agent eXperience interface)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

passed = failed = 0
REPO = Path(__file__).resolve().parents[1]
ENV = os.environ.copy()
ENV["PYTHONPATH"] = os.pathsep.join([str(REPO), ENV.get("PYTHONPATH", "")])


def ok(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    print(f"  {'PASS' if condition else 'FAIL'} {name}" + (f" - {detail}" if detail and not condition else ""))
    if condition:
        passed += 1
    else:
        failed += 1


def axi(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "observer_kit", "axi", *map(str, args)],
        cwd=cwd or REPO,
        env=ENV,
        capture_output=True,
        text=True,
        timeout=30,
    )


print("Testing observer-kit axi\n")

# Help / home without state
h = axi("help")
ok("axi help exits 0", h.returncode == 0, h.stderr)
ok("axi help is TOON", "surface:" in h.stdout and "help[" in h.stdout, h.stdout[:200])

home_missing = axi("--state-dir", "/tmp/observer-axi-missing-xyz")
ok("axi home missing state exits 1", home_missing.returncode == 1)
ok("axi home missing emits error-ish state_ok false", "state_ok: false" in home_missing.stdout, home_missing.stdout)

with tempfile.TemporaryDirectory(prefix="observer-axi-") as tmp:
    root = Path(tmp)
    state = root / ".observer"
    state.mkdir()
    (state / "runs" / "demo-lane").mkdir(parents=True)
    ev = state / "runs" / "demo-lane" / "events.jsonl"
    started = {
        "event": "run_started",
        "description": "AXI demo run",
        "todo": 2,
    }
    rec = {"event": "record", "table": "t", "key": "1", "status": "ok"}
    finished = {"event": "run_finished", "status": "success"}
    with ev.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(started) + "\n")
        fh.write(json.dumps(rec) + "\n")
        fh.write(json.dumps(finished) + "\n")
    # dead lock so live is false after terminal
    (state / "demo-lane.lock").write_text(
        json.dumps({"pid": 1, "scope": "demo-lane"}), encoding="utf-8"
    )

    home = axi("--state-dir", str(state))
    ok("axi home on state exits 0", home.returncode == 0, home.stderr)
    ok("axi home reports runs count", "runs: 1" in home.stdout, home.stdout)
    ok("axi home includes help", "help[" in home.stdout)

    runs = axi("runs", "--state-dir", str(state))
    ok("axi runs lists the lane", runs.returncode == 0 and "runguard:demo-lane" in runs.stdout, runs.stdout)
    ok("axi runs table header", "runs[1]{" in runs.stdout or "runs[1]" in runs.stdout, runs.stdout)

    detail = axi("run", "--state-dir", str(state), "--id", "runguard:demo-lane")
    ok("axi run detail", detail.returncode == 0 and "status: success" in detail.stdout, detail.stdout)
    ok("axi run record count", "records: 1" in detail.stdout, detail.stdout)
    ok("axi run has last_event", "last_event:" in detail.stdout, detail.stdout)
    ok("axi run has attention table", "attention[0]{" in detail.stdout or "attention[" in detail.stdout, detail.stdout)
    ok("axi run has state_dir", "state_dir:" in detail.stdout, detail.stdout)

    att = axi("attention", "--state-dir", str(state), "--id", "runguard:demo-lane")
    ok("axi attention exits 0", att.returncode == 0, att.stderr)
    ok("axi attention empty table", "attention[0]{" in att.stdout, att.stdout)

    sample = axi("sample-status", "--state-dir", str(state), "--id", "runguard:demo-lane")
    ok("axi sample-status exits 0", sample.returncode == 0, sample.stderr)
    ok("axi sample-status has approval_recorded", "approval_recorded:" in sample.stdout, sample.stdout)

    controls = axi("controls", "--state-dir", str(state), "--id", "runguard:demo-lane")
    ok("axi controls exits 0", controls.returncode == 0, controls.stderr)
    ok("axi controls table", "controls[0]{" in controls.stdout or "controls[" in controls.stdout, controls.stdout)

    chat = axi("chat", "--state-dir", str(state), "--id", "runguard:demo-lane")
    ok("axi chat exits 0", chat.returncode == 0, chat.stderr)
    ok("axi chat empty table", "chat[0]{" in chat.stdout, chat.stdout)

    missing = axi("run", "--state-dir", str(state), "--id", "runguard:nope")
    ok("axi run missing exits 1", missing.returncode == 1)
    ok("axi run missing error", "error:" in missing.stdout)

    # doctor on temp project
    (state / ".gitignore").write_text("*\n", encoding="utf-8")
    (state / "EXPLAIN.md").write_text("# demo\n", encoding="utf-8")
    doc = axi("doctor", str(root), "--state-dir", ".observer")
    ok("axi doctor ok", doc.returncode == 0 and "ok: true" in doc.stdout, doc.stdout)
    ok("axi doctor reports install_skew field", "install_skew:" in doc.stdout, doc.stdout)
    ok("axi doctor reports version", "version:" in doc.stdout, doc.stdout)

    ps = axi("ps", "--state-dir", str(state), "--no-scan")
    ok("axi ps exits 0", ps.returncode == 0, ps.stderr)
    ok("axi ps TOON orphans field", "orphans:" in ps.stdout, ps.stdout)
    ok("axi ps state_dir field", "state_dir:" in ps.stdout, ps.stdout)

# help catalog includes new verbs
h2 = axi("help")
ok("axi help lists attention", "attention" in h2.stdout, h2.stdout)
ok("axi help lists sample-status", "sample-status" in h2.stdout, h2.stdout)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
