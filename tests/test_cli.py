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
REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPO_ROOT / ".claude" / "skills" / "observer-kit"
PACKAGE_ROOT = REPO_ROOT / "observer_kit"
CLI_ENV = os.environ.copy()
SOURCE_PACKAGE = REPO_ROOT / "observer_kit" / "__init__.py"
SOURCE_CHECKOUT = (REPO_ROOT / "pyproject.toml").is_file()
PACKAGE_ROOT = REPO_ROOT / "observer_kit"
IMPORT_SHIMS = REPO_ROOT / "tests" / "import_shims"
if SOURCE_PACKAGE.is_file():
    # Source checkout: package + legacy `import runguard` shim for worker snippets.
    CLI_ENV["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(IMPORT_SHIMS), CLI_ENV.get("PYTHONPATH", "")]
    )
# CLI e2e focuses on plumbing; approval gate has dedicated tests.
CLI_ENV["OBSERVER_ALLOW_UNAPPROVED_FULL_RUN"] = "1"
CLI_ENV["OBSERVER_NO_LINT"] = "1"

cli_probe = subprocess.run(
    [sys.executable, "-B", "-c", "import observer_kit"],
    env=CLI_ENV, capture_output=True, text=True, timeout=10,
)
if cli_probe.returncode != 0:
    if SOURCE_CHECKOUT:
        print("FAIL Observer Kit CLI package is missing from its source checkout")
        print(cli_probe.stderr.strip())
        sys.exit(1)
    print("Testing Observer Kit CLI end to end\n")
    print("  SKIP Python CLI is a separate install; bundled skill scripts are available.")
    sys.exit(0)


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


def wait_for_watch_status(state: Path, cwd: Path, terms: tuple[str, ...], timeout: float = 4) -> str:
    deadline = time.time() + timeout
    output = ""
    while time.time() < deadline:
        status = cli("watch", str(state), "--status", cwd=cwd)
        output = status.stdout + status.stderr
        if all(term in output for term in terms):
            return output
        time.sleep(0.05)
    return output


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
    state = project / ".observer"
    ok("init does not vendor product runtime by default",
       init.returncode == 0
       and not (project / "runguard.py").exists()
       and not (project / "watch_chat.py").exists(),
       init.stdout + init.stderr)
    ok("init creates a private state dir",
       (state / "EXPLAIN.md").is_file()
       and (state / "runs").is_dir()
       and (state / ".gitignore").read_text(encoding="utf-8")
       == "*.lock\n*.throttle\n*.jsonl\n")
    ok("init EXPLAIN template matches package template",
       (state / "EXPLAIN.md").read_bytes()
       == (PACKAGE_ROOT / "EXPLAIN.md").read_bytes())

    doctor = cli("doctor", str(project), cwd=project)
    ok("doctor accepts a fresh project", doctor.returncode == 0, doctor.stdout + doctor.stderr)

    port = free_port()
    dashboard = subprocess.Popen(
        [sys.executable, "-B", "-m", "observer_kit", "dashboard", str(state), "--port", str(port)],
        cwd=project, env=CLI_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        ok("long-lived dashboard starts", wait_for_dashboard(port))
        with urlopen(f"http://127.0.0.1:{port}/assets/dashboard.js", timeout=3) as response:
            dashboard_js = response.read()
            dashboard_js_type = response.headers.get_content_type()
        expected_dashboard_js = (PACKAGE_ROOT / "assets" / "dashboard.js").read_bytes()
        ok("CLI dashboard serves the bundled JavaScript asset byte-for-byte",
           dashboard_js_type == "application/javascript" and dashboard_js == expected_dashboard_js)

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
        run_id = marker.group(1) if marker else "runguard:missing"
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
        failure_ledgers = list(state.glob("runs/*cli-failure*/events.jsonl")) + list(
            state.glob("*cli-failure*.jsonl"))
        failure_events = []
        if failure_ledgers:
            failure_events = [json.loads(line) for line in failure_ledgers[0].read_text(encoding="utf-8").splitlines()]
        ok("run returns child failure and retains its terminal event", failed_run.returncode == 7 and
           failure_events and failure_events[-1].get("event") == "run_failed",
           failed_run.stdout + failed_run.stderr)

        watcher_cmd = [sys.executable, "-B", "-m", "observer_kit", "watch", str(state),
                       "--follow", "--poll", "0.05"]
        watch_a = subprocess.Popen(
            [*watcher_cmd, "--run", "runguard:lease-a"], cwd=project, env=CLI_ENV,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        watch_b = subprocess.Popen(
            [*watcher_cmd, "--run", "runguard:lease-b"], cwd=project, env=CLI_ENV,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            status = wait_for_watch_status(
                state, project, ("runguard:lease-a", "runguard:lease-b"))
            ok("different run IDs retain independent watcher ownership",
               "runguard:lease-a" in status and "runguard:lease-b" in status,
               status)

            duplicate = cli("watch", str(state), "--run", "runguard:lease-a",
                            "--follow", "--poll", "0.05", cwd=project, timeout=5)
            ok("a second watcher for the same run is refused",
               duplicate.returncode == 3 and "WATCHER ALREADY ACTIVE" in duplicate.stderr,
               duplicate.stdout + duplicate.stderr)

            overlapping_all = cli("watch", str(state), "--all", "--follow", "--poll", "0.05",
                                  cwd=project, timeout=5)
            ok("an all-run watcher refuses overlapping run-scoped ownership",
               overlapping_all.returncode == 3 and "WATCHER ALREADY ACTIVE" in overlapping_all.stderr,
               overlapping_all.stdout + overlapping_all.stderr)
        finally:
            watch_a.terminate()
            watch_b.terminate()
            watch_a.wait(timeout=3)
            watch_b.wait(timeout=3)
        cleared = wait_for_watch_status(state, project, ("no active watchers",))
        ok("watcher children exit when their CLI parent exits", "no active watchers" in cleared, cleared)

        all_watcher = subprocess.Popen(
            [*watcher_cmd, "--all"], cwd=project, env=CLI_ENV,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            all_status = wait_for_watch_status(state, project, ("all runs",))
            ok("one all-run watcher owns a single-session project bridge",
               "all runs" in all_status, all_status)

            covered_run = cli("run", "--state-dir", str(state), "--", sys.executable, "-B", "-c",
                              WORKER, cwd=project, timeout=10)
            covered_output = covered_run.stdout + covered_run.stderr
            ok("run reuses a covering watcher and exits with its worker",
               covered_run.returncode == 0 and "reusing active all-run watcher" in covered_output and
               "review bridge is still active" not in covered_output,
               covered_output[-800:])
        finally:
            all_watcher.terminate()
            all_watcher.wait(timeout=3)
        final_status = wait_for_watch_status(state, project, ("no active watchers",))
        ok("all-run watcher child exits with its CLI parent",
           "no active watchers" in final_status, final_status)

        # AXI-style poll: listening presence, deliver note, flip to responding.
        poll_run = "runguard:poll-demo"
        poll_proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "observer_kit", "poll", str(state),
             "--run", poll_run, "--poll", "0.1"],
            cwd=project, env=CLI_ENV,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            listening = False
            for _ in range(40):
                chat_path = state / "runs" / "poll-demo" / "chat.jsonl"
                if not chat_path.is_file():
                    chat_path = state / "chat.jsonl"
                if chat_path.is_file():
                    rows = [
                        json.loads(line) for line in chat_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    if any(
                        row.get("kind") == "agent_status"
                        and row.get("status") == "listening"
                        and row.get("run") == poll_run
                        for row in rows
                    ):
                        listening = True
                        break
                time.sleep(0.05)
            ok("poll marks the run as listening", listening)

            # Post a user note while the poll is waiting.
            def lane_chat(run_name: str) -> Path:
                path = state / "runs" / run_name / "chat.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                return path

            def load_chat(run_name: str) -> list[dict]:
                rows = []
                for path in (state / "runs" / run_name / "chat.jsonl", state / "chat.jsonl"):
                    if not path.is_file():
                        continue
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            rows.append(json.loads(line))
                return rows

            with lane_chat("poll-demo").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": "2099-01-01T00:00:00.000000001Z",
                    "run": poll_run,
                    "anchor": "run",
                    "author": "user",
                    "text": "poll me please",
                }) + "\n")
            try:
                out, err = poll_proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                poll_proc.kill()
                out, err = poll_proc.communicate(timeout=3)
            ok("poll delivers the note and exits",
               poll_proc.returncode == 0
               and "OBSERVER_CHAT_EVENT" in out
               and "poll me please" in out,
               out + err)
            chat_rows = load_chat("poll-demo")
            statuses = [
                row.get("status") for row in chat_rows
                if row.get("kind") == "agent_status" and row.get("run") == poll_run
            ]
            ok("poll flips presence to responding after delivery",
               statuses and statuses[-1] == "responding", str(statuses))

            reply = cli(
                "reply", str(state), "--run", poll_run,
                "--text", "heard you", "--resolved", cwd=project,
            )
            ok("reply clears presence to idle",
               reply.returncode == 0 and "replied to" in reply.stdout)
            chat_rows = load_chat("poll-demo")
            statuses = [
                row.get("status") for row in chat_rows
                if row.get("kind") == "agent_status" and row.get("run") == poll_run
            ]
            ok("reply leaves agent status idle",
               statuses and statuses[-1] == "idle", str(statuses))
        finally:
            if poll_proc.poll() is None:
                poll_proc.terminate()
                poll_proc.wait(timeout=3)

        # poll --reply posts first, then listens (Lavish --agent-reply pattern).
        reply_poll = cli(
            "poll", str(state), "--run", "runguard:reply-poll",
            "--reply", "pre-reply", "--resolved", "--timeout", "0.3", "--poll", "0.1",
            cwd=project, timeout=5,
        )
        ok("poll --reply writes the agent message before waiting",
           reply_poll.returncode == 0 and "replied to" in reply_poll.stdout,
           reply_poll.stdout + reply_poll.stderr)
        reply_rows = []
        for path in (state / "runs" / "reply-poll" / "chat.jsonl", state / "chat.jsonl"):
            if path.is_file():
                reply_rows.extend(
                    json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
        ok("poll --reply durable agent text is in chat",
           any(row.get("author") == "agent" and row.get("text") == "pre-reply" for row in reply_rows))

        # /api/meta exposes state_dir so attach refuses a foreign dashboard.
        meta = api_json(port, "/api/meta")
        ok("dashboard /api/meta reports the served state directory",
           Path(meta.get("state_dir") or "").resolve() == state.resolve(),
           str(meta))
        ok("dashboard /api/meta exposes process ownership fields",
           meta.get("pid") is not None and "parent_pid" in meta and "idle_timeout" in meta,
           str(meta))
        meta_file = state / ".observer-dashboard.json"
        ok("dashboard writes ownership meta file",
           meta_file.is_file() and "pid" in meta_file.read_text(encoding="utf-8"),
           str(meta_file))

        ps = cli("ps", str(state), cwd=project, timeout=10)
        ok("ps lists the live dashboard",
           ps.returncode == 0 and "dashboards:" in ps.stdout and str(port) in ps.stdout,
           ps.stdout + ps.stderr)

        other_state = Path(tempfile.mkdtemp(prefix="observer-other-state-"))
        try:
            # Port is already owned by `state`; attaching with a different dir must fail.
            mismatch = cli(
                "run", "--state-dir", str(other_state), "--dashboard", "--port", str(port),
                "--", sys.executable, "-B", "-c", "print('should-not-run')",
                cwd=project, timeout=10,
            )
            ok("run --dashboard refuses a port serving a different state dir",
               mismatch.returncode != 0
               and "already serving" in (mismatch.stdout + mismatch.stderr),
               mismatch.stdout + mismatch.stderr)
        finally:
            import shutil
            shutil.rmtree(other_state, ignore_errors=True)
    finally:
        dashboard.terminate()
        try:
            dashboard.wait(timeout=3)
        except subprocess.TimeoutExpired:
            dashboard.kill()


# --- Process ownership: parent-pid, ps, stop ---------------------------------
print("\nProcess ownership (parent-pid / ps / stop)")
with tempfile.TemporaryDirectory(prefix="observer-cli-lifecycle-") as tmp:
    project = Path(tmp)
    state = project / ".observer"
    state.mkdir()
    port = free_port()

    # Dashboard bound to a short-lived parent exits when that parent dies.
    launcher = subprocess.Popen(
        [
            sys.executable, "-B", "-c",
            "import os, subprocess, sys, time\n"
            f"env = dict(os.environ); env['PYTHONPATH'] = {CLI_ENV.get('PYTHONPATH', '')!r}\n"
            "child = subprocess.Popen(\n"
            "    [sys.executable, '-B', '-m', 'observer_kit', 'dashboard',\n"
            f"     {str(state)!r}, '--port', '{port}', '--parent-pid', str(os.getpid())],\n"
            "    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
            ")\n"
            "time.sleep(1.5)\n"
            # parent exits → child should shut down via lifecycle watch
        ],
        env=CLI_ENV,
    )
    ok("parent-bound dashboard launcher starts", wait_for_dashboard(port))
    launcher.wait(timeout=10)
    gone = True
    for _ in range(40):
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=0.2) as response:
                if response.status == 200:
                    gone = False
                    time.sleep(0.15)
                    continue
        except OSError:
            gone = True
            break
    ok("dashboard exits when --parent-pid dies", gone)

    # Independent dashboard + stop --sweep
    port2 = free_port()
    dash = subprocess.Popen(
        [sys.executable, "-B", "-m", "observer_kit", "dashboard",
         str(state), "--port", str(port2)],
        cwd=project, env=CLI_ENV,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ok("independent dashboard starts for stop test", wait_for_dashboard(port2))
        dry = cli("stop", "--sweep", "--dry-run", "--port", str(port2), str(state),
                  cwd=project, timeout=10)
        ok("stop --sweep --dry-run reports the independent dashboard",
           dry.returncode == 0 and "would stop" in dry.stdout and str(port2) in dry.stdout,
           dry.stdout + dry.stderr)
        stopped = cli("stop", "--sweep", "--port", str(port2), str(state),
                      cwd=project, timeout=15)
        ok("stop --sweep terminates the independent dashboard",
           stopped.returncode == 0 and "stop complete" in stopped.stdout,
           stopped.stdout + stopped.stderr)
        dead = True
        for _ in range(30):
            try:
                with urlopen(f"http://127.0.0.1:{port2}/api/runs", timeout=0.2) as response:
                    if response.status == 200:
                        dead = False
                        time.sleep(0.1)
                        continue
            except OSError:
                dead = True
                break
        ok("swept dashboard no longer serves HTTP", dead)
    finally:
        if dash.poll() is None:
            dash.terminate()
            try:
                dash.wait(timeout=2)
            except subprocess.TimeoutExpired:
                dash.kill()


print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
