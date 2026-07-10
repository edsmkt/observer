from __future__ import annotations

import argparse
import json
import os
import runpy
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _skill_dir() -> Path:
    env_dir = os.environ.get("OBSERVER_KIT_SKILL_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    candidates = [
        ROOT / "skills" / "observer-kit",          # source checkout / editable install
        Path(sys.prefix) / "skills" / "observer-kit",  # wheel data_files install
        Path(sys.base_prefix) / "skills" / "observer-kit",
    ]
    return next((p for p in candidates if p.exists()), candidates[0])


SKILL_DIR = _skill_dir()


def _timestamp() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def skill_file(name: str) -> Path:
    path = SKILL_DIR / name
    if not path.exists():
        raise SystemExit(
            f"Observer Kit file not found: {path}\n"
            "Set OBSERVER_KIT_SKILL_DIR to the directory containing runguard.py "
            "and run_dashboard.py."
        )
    return path


def copy_file(src: Path, dst: Path, force: bool) -> str:
    if dst.exists() and not force:
        return f"skip existing {dst}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return f"wrote {dst}"


def cmd_init(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    messages = [
        copy_file(skill_file("runguard.py"), project / "runguard.py", args.force),
    ]
    if args.watch:
        messages.append(copy_file(skill_file("watch_chat.py"), project / "watch_chat.py", args.force))
    state_dir = (project / args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    messages.append(f"ready {state_dir}")
    if args.explain:
        messages.append(copy_file(skill_file("EXPLAIN.md"), state_dir / "EXPLAIN.md", args.force))
    if args.gitignore:
        gitignore = state_dir / ".gitignore"
        if not gitignore.exists() or args.force:
            gitignore.write_text("*.lock\n*.throttle\n*.jsonl\n", encoding="utf-8")
            messages.append(f"wrote {gitignore}")
    print("\n".join(messages))
    print()
    print("Next:")
    print(f"  observer-kit dashboard {state_dir}")
    print(f"  observer-kit watch {state_dir} --run <run-id>")
    print("  python3 your_workflow.py --dry-run --limit 10")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    script = skill_file("run_dashboard.py")
    old_argv = sys.argv[:]
    try:
        sys.argv = [str(script), str(state_dir), "--port", str(args.port)]
        try:
            runpy.run_path(str(script), run_name="__main__")
        except KeyboardInterrupt:
            print()
            return 130
    finally:
        sys.argv = old_argv
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    tests = [
        [sys.executable, str(skill_file("test_runguard.py")), str(SKILL_DIR)],
        [sys.executable, str(skill_file("test_data_movement.py")), str(SKILL_DIR)],
        [sys.executable, str(skill_file("test_lint_emit.py"))],
        [sys.executable, str(skill_file("test_skill.py")), str(SKILL_DIR)],
        [sys.executable, str(skill_file("test_dashboard.py"))],
        [sys.executable, str(skill_file("test_cli.py"))],
    ]
    flow_skill_dir = SKILL_DIR.parent / "observer-flow"
    if flow_skill_dir.is_dir():
        tests.extend([
            [sys.executable, str(flow_skill_dir / "test_validate_flow.py"), str(flow_skill_dir)],
            [sys.executable, str(flow_skill_dir / "test_skill.py"), str(flow_skill_dir)],
        ])
    for cmd in tests:
        rc = subprocess.call(cmd)
        if rc:
            return rc
    return 0


def _chat_path(state_dir: Path) -> Path:
    return state_dir / "chat.jsonl"


def _pid_alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _active_watchers(state_dir: Path) -> list[dict]:
    watchers = []
    for path in state_dir.glob(".observer-watcher-*.lock"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if meta.get("active") and _pid_alive(meta.get("pid")):
            watchers.append(meta)
    return sorted(watchers, key=lambda item: str(item.get("started", "")))


def _covering_watcher(state_dir: Path, run_id: str) -> dict | None:
    run_key = f"run:{run_id}"
    for watcher in _active_watchers(state_dir):
        if watcher.get("key") in (run_key, "all"):
            return watcher
    return None


def _print_watcher_status(state_dir: Path) -> int:
    watchers = _active_watchers(state_dir)
    if not watchers:
        print(f"no active watchers for {state_dir}")
        return 0
    for watcher in watchers:
        target = "all runs" if watcher.get("mode") == "all" else watcher.get("run")
        parent = watcher.get("parent_pid") or "independent"
        print(f"active pid={watcher.get('pid')} parent={parent} target={target} "
              f"started={watcher.get('started')}")
    return 0


def _load_chat(path: Path) -> list[dict]:
    if not path.exists():
        return []
    messages = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages


def _chat_signature(message: dict) -> str:
    return json.dumps(
        [
            message.get("ts"),
            message.get("run"),
            message.get("anchor"),
            message.get("author"),
            message.get("text"),
        ],
        ensure_ascii=False,
        sort_keys=True,
    )


def _wakes_watcher(message: dict) -> bool:
    return message.get("author") == "user" or message.get("kind") == "control"


def _stream_watcher_line(line: str) -> None:
    line = line.strip()
    if not line:
        return
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        print(line, flush=True)
        return
    print("OBSERVER_CHAT_EVENT " + json.dumps(payload, ensure_ascii=False), flush=True)
    run = payload.get("run", "")
    anchor = payload.get("anchor", "")
    text = payload.get("text", "")
    if text:
        print(f"[observer] dashboard note for {run} {anchor}: {text}", flush=True)


def cmd_watch(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    if args.status:
        return _print_watcher_status(state_dir)

    if not args.run:
        if not args.all:
            raise SystemExit("--run is required unless --all is set")
    cmd = [
        sys.executable,
        str(skill_file("watch_chat.py")),
        "--state-dir",
        str(state_dir),
        "--poll",
        str(args.poll),
        "--parent-pid",
        str(os.getpid()),
    ]
    if args.all:
        cmd.append("--all")
    else:
        cmd.insert(2, args.run)
    if args.follow:
        cmd.append("--follow")
    if args.timeout:
        cmd.extend(["--timeout", str(args.timeout)])
    if args.include_existing:
        cmd.append("--include-existing")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            _stream_watcher_line(line)
    except KeyboardInterrupt:
        proc.terminate()
        return 130
    return proc.wait()


def cmd_reply(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    text = args.text.strip()
    if not text:
        raise SystemExit("reply text is required")
    rec = {
        "ts": _timestamp(),
        "run": args.run,
        "anchor": args.anchor,
        "author": "agent",
        "text": text,
        "resolved": bool(args.resolved),
    }
    with _chat_path(state_dir).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    print(f"replied to {args.run} {args.anchor}")
    return 0


def _dashboard_responds(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=0.25) as response:
            return response.status == 200
    except (OSError, URLError):
        return False


def _start_dashboard(state_dir: Path, port: int) -> tuple[subprocess.Popen | None, bool]:
    if _dashboard_responds(port):
        return None, True
    proc = subprocess.Popen(
        [sys.executable, str(skill_file("run_dashboard.py")), str(state_dir), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    for _ in range(30):
        if _dashboard_responds(port):
            return proc, False
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    proc.terminate()
    raise SystemExit(f"Observer dashboard did not start on http://localhost:{port}/")


def _start_watcher(state_dir: Path, run_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(skill_file("watch_chat.py")),
            run_id,
            "--state-dir",
            str(state_dir),
            "--follow",
            "--parent-pid",
            str(os.getpid()),
            # The run marker and watcher process are separate processes. Include
            # a note that lands in that small startup window instead of losing it.
            "--include-existing",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("usage: observer-kit run [options] -- <command> [args...]")

    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    dashboard_proc = None
    watcher_proc = None
    watcher_thread = None
    run_id_holder = {"run_id": None}
    lock = threading.Lock()

    def ensure_watcher(run_id: str) -> None:
        nonlocal watcher_proc, watcher_thread
        if args.watch == "none":
            return
        with lock:
            if watcher_proc is not None:
                return
            covering = _covering_watcher(state_dir, run_id)
            if covering:
                target = "all-run" if covering.get("mode") == "all" else "run-scoped"
                print(f"[observer] reusing active {target} watcher pid {covering.get('pid')}",
                      flush=True)
                return
            watcher_proc = _start_watcher(state_dir, run_id)
            print(f"[observer] watcher connected for {run_id}", flush=True)
            assert watcher_proc.stdout is not None

            def pump_watcher() -> None:
                assert watcher_proc is not None and watcher_proc.stdout is not None
                for watcher_line in watcher_proc.stdout:
                    _stream_watcher_line(watcher_line)

            watcher_thread = threading.Thread(target=pump_watcher, daemon=True)
            watcher_thread.start()

    if args.dashboard:
        dashboard_proc, dashboard_attached = _start_dashboard(state_dir, args.port)
        state = "attached to existing dashboard" if dashboard_attached else "dashboard started"
        print(f"[observer] {state}: http://localhost:{args.port}/", flush=True)

    env = os.environ.copy()
    env["RUNGUARD_STATE_DIR"] = str(state_dir)
    env["PYTHONUNBUFFERED"] = "1"
    if args.session:
        env["RUNGUARD_SESSION"] = (
            f"auto-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{os.getpid()}"
            if args.session == "auto" else args.session
        )
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    def pump_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()

    def pump_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if line.startswith("OBSERVER_RUN_STARTED "):
                run_id = line.strip().split(maxsplit=1)[1]
                run_id_holder["run_id"] = run_id
                ensure_watcher(run_id)
            sys.stderr.write(line)
            sys.stderr.flush()

    out_thread = threading.Thread(target=pump_stdout)
    err_thread = threading.Thread(target=pump_stderr)
    out_thread.start()
    err_thread.start()

    try:
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        rc = 130
    out_thread.join()
    err_thread.join()

    if run_id_holder["run_id"] is None and args.watch != "none":
        print("[observer] no OBSERVER_RUN_STARTED marker was seen; watcher was not started", file=sys.stderr)

    if not args.exit_after_run and (watcher_proc is not None or dashboard_proc is not None):
        print("[observer] run exited; review bridge is still active. Press Ctrl-C to stop.", flush=True)
        try:
            while True:
                if watcher_proc is not None and watcher_proc.poll() is not None:
                    print("[observer] watcher exited", flush=True)
                    break
                if dashboard_proc is not None and dashboard_proc.poll() is not None:
                    print("[observer] dashboard exited", flush=True)
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            print()

    if watcher_proc is not None:
        watcher_proc.terminate()
        try:
            watcher_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            watcher_proc.kill()
    if dashboard_proc is not None:
        if args.keep_dashboard:
            print(f"[observer] dashboard still running at http://localhost:{args.port}/", flush=True)
        else:
            dashboard_proc.terminate()
            try:
                dashboard_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                dashboard_proc.kill()
    return rc


def cmd_doctor(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    checks = []
    checks.append(("project exists", project.exists()))
    checks.append(("runguard.py vendored", (project / "runguard.py").exists()))
    checks.append(("watch_chat.py vendored", (project / "watch_chat.py").exists()))
    checks.append(("state dir exists", (project / args.state_dir).exists()))
    checks.append(("state dir ignores local ledger data", (project / args.state_dir / ".gitignore").exists()))
    checks.append(("operator explainer exists", (project / args.state_dir / "EXPLAIN.md").exists()))
    checks.append(("dashboard available", skill_file("run_dashboard.py").exists()))
    checks.append(("watcher available", skill_file("watch_chat.py").exists()))
    checks.append(("tests available", skill_file("test_runguard.py").exists()))

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(f"{'OK ' if passed else 'ERR'} {label}")
    if not ok:
        print()
        print(f"Run: observer-kit init {project}")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="observer-kit",
        description="Initialize and run Observer Kit guardrails for risky batch scripts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  observer-kit init .
  observer-kit dashboard .runguard
  observer-kit watch .runguard --all --follow
  observer-kit run --state-dir .runguard -- python3 workflow.py --dry-run --limit 10
  observer-kit test
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init",
        help="vendor runguard.py and create a state dir",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  observer-kit init .
  observer-kit init ./my-project --force

next:
  observer-kit dashboard ./my-project/.runguard
  observer-kit watch ./my-project/.runguard --all --follow
""",
    )
    init.add_argument("project", nargs="?", default=".", help="target project directory")
    init.add_argument("--state-dir", default=".runguard", help="state directory inside the project")
    init.add_argument("--force", action="store_true", help="overwrite existing files")
    init.add_argument("--no-explain", dest="explain", action="store_false",
                      help="do not copy EXPLAIN.md into the state dir")
    init.add_argument("--no-gitignore", dest="gitignore", action="store_false",
                      help="do not write a state-dir .gitignore")
    init.add_argument("--no-watch", dest="watch", action="store_false",
                      help="do not vendor watch_chat.py next to runguard.py")
    init.set_defaults(func=cmd_init, explain=True, gitignore=True, watch=True)

    dash = sub.add_parser(
        "dashboard",
        help="run the localhost dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  observer-kit dashboard .runguard
  observer-kit dashboard ./my-project/.runguard --port 8485

For long-lived monitoring, keep this server running and launch pipelines in
separate shells with observer-kit run --state-dir <same-dir> ...
""",
    )
    dash.add_argument("state_dir", nargs="?", default=".runguard", help="ledger/state directory")
    dash.add_argument("--port", type=int, default=8484, help="dashboard port")
    dash.set_defaults(func=cmd_dashboard)

    watch = sub.add_parser(
        "watch",
        help="bridge dashboard chat to stdout for the active harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # Preferred with a long-lived dashboard: bridge notes from any run.
  observer-kit watch .runguard --all --follow

  # Scoped bridge for one run id.
  observer-kit watch .runguard --run runguard:my-run.jsonl --follow

The watcher is transport only. It emits OBSERVER_CHAT_EVENT lines; the harness
session remains responsible for inspecting data, editing scripts, rerunning,
and replying.

Long-lived followers register ownership in the state directory. The same run
reuses one watcher, different run IDs remain independent, and --all is the
single-session project-wide mode.
""",
    )
    watch.add_argument("state_dir", nargs="?", default=".runguard", help="ledger/state directory")
    watch.add_argument("--run", help="run id, e.g. runguard:my-run.jsonl")
    watch.add_argument("--all", action="store_true", help="bridge dashboard chat for all runs")
    watch.add_argument("--poll", type=float, default=2.0, help="poll interval in seconds")
    watch.add_argument("--follow", action="store_true", help="keep streaming dashboard notes")
    watch.add_argument("--timeout", type=float, default=0, help="0 = wait forever")
    watch.add_argument("--include-existing", action="store_true",
                       help="emit existing user notes instead of only new notes")
    watch.add_argument("--status", action="store_true",
                       help="list active watcher ownership for this state directory")
    watch.set_defaults(func=cmd_watch)

    reply = sub.add_parser(
        "reply",
        help="write an agent reply into dashboard chat",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""example:
  observer-kit reply .runguard \\
    --run runguard:my-run.jsonl \\
    --anchor 'cell:companies::acme|companies::status' \\
    --resolved \\
    --text "Fixed the parser and reran the sample."
""",
    )
    reply.add_argument("state_dir", nargs="?", default=".runguard", help="ledger/state directory")
    reply.add_argument("--run", required=True, help="run id to reply to")
    reply.add_argument("--anchor", default="run", help="dashboard anchor/cell id")
    reply.add_argument("--resolved", action="store_true", help="mark the note resolved")
    reply.add_argument("--text", required=True, help="reply text")
    reply.set_defaults(func=cmd_reply)

    run = sub.add_parser(
        "run",
        help="run a command with Observer Kit state and watcher plumbing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # Dry-run sample in the current source lane.
  observer-kit run --state-dir .runguard -- python3 workflow.py --dry-run --limit 10

  # Retry/fix/continue the same source data in the same dashboard run.
  observer-kit run --state-dir .runguard --session july-import \\
    -- python3 workflow.py --full-run

  # Start a separate comparison or new batch run.
  observer-kit run --state-dir .runguard --session auto \\
    -- python3 workflow.py --full-run

  # Quick demo: launch a temporary dashboard with this command.
  observer-kit run --state-dir .runguard --dashboard \\
    -- python3 workflow.py --dry-run --limit 10

For persistent monitoring, prefer:
  observer-kit dashboard .runguard
  observer-kit watch .runguard --all --follow
  observer-kit run --state-dir .runguard --session source-id -- ...

Session rule:
  same source retry/adaptation = reuse the same session or omit --session
  add enrichment to existing rows = reuse the same session, table, and keys
  clean comparison/new batch   = use --session auto or a new session name
""",
    )
    run.add_argument("--state-dir", default=".runguard", help="ledger/state directory")
    run.add_argument("--dashboard", action="store_true", help="start a dashboard for this run")
    run.add_argument("--port", type=int, default=8484, help="dashboard port")
    run.add_argument("--keep-dashboard", action="store_true",
                     help="leave the dashboard process running after the command exits")
    run.add_argument("--exit-after-run", action="store_true",
                     help="stop dashboard/watch plumbing as soon as the command exits")
    run.add_argument("--watch", choices=["stdout", "none"], default="stdout",
                     help="where dashboard chat events should be bridged")
    run.add_argument("--session", help="set RUNGUARD_SESSION; use 'auto' for a timestamped run lane")
    run.add_argument("command", nargs=argparse.REMAINDER,
                     help="command to run; put -- before the command")
    run.set_defaults(func=cmd_run)

    test = sub.add_parser(
        "test",
        help="run runguard acceptance tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""example:
  observer-kit test
""",
    )
    test.set_defaults(func=cmd_test)

    doctor = sub.add_parser(
        "doctor",
        help="check a project for Observer Kit basics",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  observer-kit doctor .
  observer-kit doctor ./my-project

Doctor checks the substrate only: vendored runguard.py/watch_chat.py, state dir,
and available dashboard/tests. It does not prove a workflow uses the guardrails
correctly.
""",
    )
    doctor.add_argument("project", nargs="?", default=".", help="project directory")
    doctor.add_argument("--state-dir", default=".runguard", help="state directory inside the project")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
