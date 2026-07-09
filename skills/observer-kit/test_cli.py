#!/usr/bin/env python3
"""End-to-end acceptance tests for the public Observer Kit CLI."""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen


passed = failed = 0
REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_ENV = os.environ.copy()
if (REPO_ROOT / "observer_kit" / "__init__.py").is_file():
    # Source checkout: subprocesses intentionally run from a fresh target
    # project, so preserve the package import path explicitly.
    CLI_ENV["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + CLI_ENV.get("PYTHONPATH", "")


def ok(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    print(f"  {'PASS' if condition else 'FAIL'} {name}" + (f" - {detail}" if detail and not condition else ""))
    if condition:
        passed += 1
    else:
        failed += 1


def cli(*args: str, cwd: Path, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "observer_kit", *map(str, args)],
        cwd=cwd, env=CLI_ENV, capture_output=True, text=True, timeout=timeout,
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_dashboard(port: int) -> bool:
    for _ in range(60):
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=0.25) as response:
                return response.status == 200
        except OSError:
            time.sleep(0.1)
    return False


def api_json(port: int, path: str, payload: dict | None = None):
    url = f"http://127.0.0.1:{port}{path}"
    if payload is None:
        request = Request(url)
    else:
        request = Request(url, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=3) as response:
        return json.load(response)


WORKER = """import runguard
run = runguard.start_observed_run(
    'cli-smoke', source='cli-qa-source', dry_run=True,
    description='CLI end-to-end test', todo=1, progress_table='companies')
with run.step('inspect', table='companies', key='acme', domain='acme.example'):
    run.count('processed')
run.success(processed=1)
"""

FAILING_WORKER = """import runguard, sys
run = runguard.start_observed_run('cli-failure', source='cli-failure-source')
run.fail('intentional CLI acceptance failure')
sys.exit(7)
"""

LIVE_WATCH_WORKER = """import runguard, time
run = runguard.start_observed_run('live-watch', source='live-watch-source', description='Live watcher test')
time.sleep(3)
run.success()
"""


print("Testing Observer Kit CLI end to end\n")

with tempfile.TemporaryDirectory(prefix="observer-cli-") as tmp:
    project = Path(tmp) / "project"
    init = cli("init", str(project), "--force", cwd=Path.cwd())
    state = project / ".runguard"
    ok("init vendors workflow helpers", init.returncode == 0 and
       (project / "runguard.py").is_file() and (project / "watch_chat.py").is_file())
    ok("init creates a private state dir", (state / "EXPLAIN.md").is_file() and
       (state / ".gitignore").read_text(encoding="utf-8") == "*.lock\n*.throttle\n*.jsonl\n")

    doctor = cli("doctor", str(project), cwd=project)
    ok("doctor accepts a fresh project", doctor.returncode == 0, doctor.stdout + doctor.stderr)

    port = free_port()
    dashboard = subprocess.Popen(
        [sys.executable, "-B", "-m", "observer_kit", "dashboard", str(state), "--port", str(port)],
        cwd=project, env=CLI_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        ok("long-lived dashboard starts", wait_for_dashboard(port))

        run = cli(
            "run", "--state-dir", str(state), "--dashboard", "--port", str(port),
            "--exit-after-run", "--session", "auto", "--", sys.executable, "-B", "-c", WORKER,
            cwd=project,
        )
        combined = run.stdout + run.stderr
        marker = re.search(r"OBSERVER_RUN_STARTED\s+(runguard:[^\s]+)", combined)
        ok("run attaches instead of replacing the dashboard", run.returncode == 0 and
           "attached to existing dashboard" in combined, combined[-500:])
        ok("run discovers the ledger and starts a watcher", marker is not None and
           "watcher connected" in combined, combined[-500:])
        run_id = marker.group(1) if marker else "runguard:missing.jsonl"
        meta = next((item for item in api_json(port, "/api/runs") if item.get("id") == run_id), {})
        ok("dashboard exposes the completed auto-session lane", bool(meta) and not meta.get("live"), str(meta))

        note = api_json(port, "/api/chat", {
            "run": run_id,
            "anchor": "cell:companies::acme|companies::status",
            "text": "Please verify this row.",
        })
        ok("dashboard accepts an anchored operator note", note.get("ok") is True, str(note))
        watch = cli("watch", str(state), "--run", run_id, "--include-existing", "--timeout", "2", cwd=project)
        ok("run-scoped watcher emits the operator note", watch.returncode == 0 and
           "OBSERVER_CHAT_EVENT" in watch.stdout and "Please verify this row." in watch.stdout,
           watch.stdout + watch.stderr)

        control = api_json(port, "/api/control", {"run": run_id, "kind": "stop_after_record"})
        repeated_control = api_json(port, "/api/control", {"run": run_id, "kind": "stop_after_record"})
        controls = api_json(port, f"/api/control?run={run_id}")
        control_messages = [m for m in api_json(port, f"/api/chat?run={run_id}") if m.get("kind") == "control"]
        control_watch = cli("watch", str(state), "--run", run_id,
                            "--include-existing", "--timeout", "2", cwd=project)
        ok("dashboard deduplicates control requests, keeps them out of operator notes, and wakes the watcher",
           control.get("ok") is True and controls and controls[-1].get("kind") == "stop_after_record" and
           repeated_control.get("duplicate") is True and len([c for c in controls if c.get("kind") == "stop_after_record"]) == 1 and
           control_messages and control_messages[-1].get("author") == "system" and
           "Control request: stop after record" in control_watch.stdout,
           str(control) + str(repeated_control) + control_watch.stdout + control_watch.stderr)

        reply = cli("reply", str(state), "--run", run_id,
                    "--anchor", "cell:companies::acme|companies::status",
                    "--resolved", "--text", "Verified and retained.", cwd=project)
        thread = api_json(port, f"/api/chat?run={run_id}")
        ok("reply writes a resolved agent response", reply.returncode == 0 and any(
            m.get("author") == "agent" and m.get("resolved") and m.get("text") == "Verified and retained."
            for m in thread), str(thread))

        live_proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "observer_kit", "run", "--state-dir", str(state),
             "--dashboard", "--port", str(port), "--exit-after-run", "--",
             sys.executable, "-B", "-c", LIVE_WATCH_WORKER],
            cwd=project, env=CLI_ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert live_proc.stdout is not None
        live_lines: list[str] = []
        reader = threading.Thread(target=lambda: live_lines.extend(live_proc.stdout), daemon=True)
        reader.start()
        deadline = time.time() + 10
        while time.time() < deadline and (
            not any("watcher connected" in line for line in live_lines)
            or not any(line.startswith("OBSERVER_RUN_STARTED ") for line in live_lines)
        ):
            time.sleep(0.05)
        live_output = "".join(live_lines)
        live_marker = re.search(r"OBSERVER_RUN_STARTED\s+(runguard:[^\s]+)", live_output)
        if live_marker:
            api_json(port, "/api/chat", {
                "run": live_marker.group(1), "anchor": "run", "text": "Change the live run.",
            })
        live_proc.wait(timeout=10)
        reader.join(timeout=2)
        live_output = "".join(live_lines)
        ok("run watcher surfaces notes posted during the active child run",
           live_marker is not None and "OBSERVER_CHAT_EVENT" in live_output and "Change the live run." in live_output,
           live_output[-800:])

        failed_run = cli("run", "--state-dir", str(state), "--watch", "none", "--exit-after-run", "--",
                         sys.executable, "-B", "-c", FAILING_WORKER, cwd=project)
        failure_ledgers = list(state.glob("*cli-failure*.jsonl"))
        failure_events = []
        if failure_ledgers:
            failure_events = [json.loads(line) for line in failure_ledgers[0].read_text(encoding="utf-8").splitlines()]
        ok("run returns child failure and retains its terminal event", failed_run.returncode == 7 and
           failure_events and failure_events[-1].get("event") == "run_failed",
           failed_run.stdout + failed_run.stderr)
    finally:
        dashboard.terminate()
        try:
            dashboard.wait(timeout=3)
        except subprocess.TimeoutExpired:
            dashboard.kill()

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
