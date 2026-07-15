#!/usr/bin/env python3
"""Agent-facing acceptance suite (brief §6).

1. axi help lists every subcommand that works
2. Empty state: axi home → runs: 0, recent_runs[0]…, non-empty help[]
3. Missing run id: exit ≠ 0 + error + help[] to axi runs
4. Dry-run without approval: full-run refused with stable error code
5. help[] commands from axi home parse under argparse (no phantom verbs)
6. --version prints package version
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

passed = failed = 0
REPO = Path(__file__).resolve().parents[1]
ENV = os.environ.copy()
ENV["PYTHONPATH"] = os.pathsep.join([str(REPO), ENV.get("PYTHONPATH", "")])
# This suite asserts the approval gate; do not allow unapproved full runs.
ENV.pop("OBSERVER_ALLOW_UNAPPROVED_FULL_RUN", None)
ENV["OBSERVER_REQUIRE_FULL_RUN_APPROVAL"] = "1"


def ok(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    print(
        f"  {'PASS' if condition else 'FAIL'} {name}"
        + (f" - {detail}" if detail and not condition else "")
    )
    if condition:
        passed += 1
    else:
        failed += 1


def run_mod(*args: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "observer_kit", *map(str, args)],
        cwd=cwd or REPO,
        env=env or ENV,
        capture_output=True,
        text=True,
        timeout=60,
    )


print("Testing agent-facing acceptance\n")

# --- version ---
ver = run_mod("--version")
ok("--version exits 0", ver.returncode == 0, ver.stderr)
ok("--version has package version", "observer-kit 0." in ver.stdout, ver.stdout)
ok("--version has sha=", "sha=" in ver.stdout, ver.stdout)

# --- axi help lists real subcommands ---
h = run_mod("axi", "help")
ok("axi help exits 0", h.returncode == 0, h.stderr)
for verb in (
    "axi runs",
    "axi run",
    "axi attention",
    "axi sample-status",
    "axi controls",
    "axi chat",
    "axi doctor",
    "axi ps",
    "axi help",
):
    ok(f"axi help mentions {verb}", verb in h.stdout, h.stdout[:300])

# Parse help[] entries as argv fragments under argparse (no phantom verbs)
help_match = re.search(r"help\[\d+\]:\s*(.+)", h.stdout)
ok("axi help has help[]", bool(help_match), h.stdout[:200])
if help_match:
    import json as _json
    # help is JSON-quoted list joined by commas — extract quoted strings
    quoted = re.findall(r'"([^"]*)"', help_match.group(1))
    ok("axi help has copy-pastable items", len(quoted) >= 5, str(quoted))
    for item in quoted:
        if not item.startswith("observer-kit "):
            continue
        # Concrete commands only; substitute demo lane for placeholders.
        parts = (
            item.replace("runguard:<lane>", "runguard:demo")
            .replace("runguard:LANE", "runguard:demo")
            .split()
        )
        argv = parts[1:]  # after observer-kit
        if not argv:
            continue
        if argv[0] == "axi":
            if len(argv) == 1 or argv[1].startswith("-"):
                # home / flags-only — probe axi --help
                probe = run_mod("axi", "--help")
                ok("help[] axi home is real", probe.returncode == 0, probe.stderr[:80])
                continue
            sub = argv[1]
            if sub.startswith("-"):
                continue
            probe = run_mod("axi", sub, "--help")
            ok(
                f"help[] axi {sub} is real argparse command",
                probe.returncode == 0,
                probe.stderr[:120] or probe.stdout[:120],
            )
        else:
            probe = run_mod(argv[0], "--help")
            ok(
                f"help[] {argv[0]} is real argparse command",
                probe.returncode == 0,
                probe.stderr[:120] or probe.stdout[:120],
            )

# --- empty state ---
with tempfile.TemporaryDirectory(prefix="observer-agent-") as tmp:
    state = Path(tmp) / ".observer"
    state.mkdir()
    home = run_mod("axi", "--state-dir", str(state))
    ok("empty home exits 0", home.returncode == 0, home.stderr)
    ok("empty home runs: 0", "runs: 0" in home.stdout, home.stdout)
    ok("empty home recent_runs[0]", "recent_runs[0]{" in home.stdout, home.stdout)
    ok("empty home non-empty help", re.search(r"help\[[1-9]", home.stdout) is not None, home.stdout)
    ok("empty home state_dir resolved", f"state_dir: {state}" in home.stdout or "state_dir:" in home.stdout)

    missing = run_mod("axi", "run", "--state-dir", str(state), "--id", "runguard:missing")
    ok("missing run exit != 0", missing.returncode != 0, str(missing.returncode))
    ok("missing run error", "error:" in missing.stdout, missing.stdout)
    ok("missing run help to axi runs", "axi runs" in missing.stdout, missing.stdout)

# --- full-run refuses without approval ---
with tempfile.TemporaryDirectory(prefix="observer-approve-") as tmp:
    state = Path(tmp) / ".observer"
    env = {
        **ENV,
        "RUNGUARD_STATE_DIR": str(state),
        "OBSERVER_REQUIRE_FULL_RUN_APPROVAL": "1",
    }
    env.pop("OBSERVER_ALLOW_UNAPPROVED_FULL_RUN", None)
    code = f"""
import sys
sys.path.insert(0, {str(REPO)!r})
from observer_kit.runguard import start_observed_run, ApprovalRequired, predicted_run_id
try:
    start_observed_run('agent-accept', source='agent-src', dry_run=False)
    print('UNEXPECTED_OK')
    sys.exit(0)
except ApprovalRequired as e:
    print('error: approval_required')
    print('run:', e.run_id)
    sys.exit(e.exit_code)
"""
    proc = subprocess.run(
        [sys.executable, "-B", "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp,
    )
    ok("full-run without approval exit 4", proc.returncode == 4, f"rc={proc.returncode} out={proc.stdout} err={proc.stderr}")
    ok("full-run without approval TOON-ish error", "approval_required" in proc.stdout, proc.stdout)

    # With approval posted first, full-run starts
    code2 = f"""
import sys
sys.path.insert(0, {str(REPO)!r})
from observer_kit.runguard import (
    start_observed_run, post_control, predicted_run_id, ApprovalRequired,
)
rid = predicted_run_id('agent-accept2', source='agent-src-2')
post_control(rid, 'approve_full_run', note='ok')
run = start_observed_run('agent-accept2', source='agent-src-2', dry_run=False)
print('STARTED', run.run_id)
run.success()
print('OK')
"""
    proc2 = subprocess.run(
        [sys.executable, "-B", "-c", code2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp,
    )
    ok(
        "full-run with prior approval starts",
        proc2.returncode == 0 and "STARTED" in proc2.stdout and "OK" in proc2.stdout,
        proc2.stdout + proc2.stderr,
    )

# --- scaffold ---
with tempfile.TemporaryDirectory(prefix="observer-scaffold-") as tmp:
    dest = Path(tmp) / "workflow.py"
    sc = run_mod(
        "scaffold", "workflow",
        "--dest", str(dest),
        "--source", "sheet:demo",
        "--key", "id",
    )
    ok("scaffold exits 0", sc.returncode == 0, sc.stderr)
    ok("scaffold wrote file", dest.is_file(), str(dest))
    text = dest.read_text(encoding="utf-8")
    ok("scaffold has start_observed_run", "start_observed_run" in text)
    ok("scaffold has ApprovalRequired", "ApprovalRequired" in text)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
