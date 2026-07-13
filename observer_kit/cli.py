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


# Package root (observer_kit/) and optional source checkout root (repo/).
PACKAGE_ROOT = Path(__file__).resolve().parent
ROOT = PACKAGE_ROOT.parent if (PACKAGE_ROOT.parent / "pyproject.toml").is_file() else PACKAGE_ROOT


def _skill_dir() -> Path:
    """Agent skill playbook directory (markdown/templates only — not product runtime)."""
    env_dir = os.environ.get("OBSERVER_KIT_SKILL_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    candidates = []
    if (ROOT / "pyproject.toml").is_file():
        candidates.append(ROOT / ".claude" / "skills" / "observer-kit")
        # Legacy layout kept as a fallback for older checkouts.
        candidates.append(ROOT / "skills" / "observer-kit")
    candidates.append(PACKAGE_ROOT / "_skills" / "observer-kit")
    return next((p for p in candidates if p.exists()), candidates[0] if candidates else PACKAGE_ROOT)


SKILL_DIR = _skill_dir()


def _timestamp() -> str:
    """UTC RFC 3339 with nanoseconds — matches runguard ordering/chat watermarks."""
    ns = time.time_ns()
    secs, nsec = divmod(ns, 1_000_000_000)
    base = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(secs))
    return f'{base}.{nsec:09d}Z'


def package_file(name: str) -> Path:
    """Resolve a file shipped with the installable package (runtime + templates)."""
    path = PACKAGE_ROOT / name
    if not path.exists():
        raise SystemExit(
            f"Observer Kit package file not found: {path}\n"
            "Reinstall the package: python -m pip install -e ."
        )
    return path


def skill_file(name: str) -> Path:
    """Resolve an agent skill playbook/template file (not product runtime)."""
    path = SKILL_DIR / name
    if path.exists():
        return path
    # Templates also ship in the package for init without a skill tree.
    pkg = PACKAGE_ROOT / name
    if pkg.exists():
        return pkg
    raise SystemExit(
        f"Observer Kit skill/template file not found: {name}\n"
        f"Looked under {SKILL_DIR} and {PACKAGE_ROOT}."
    )


def copy_file(src: Path, dst: Path, force: bool) -> str:
    if dst.exists() and not force:
        return f"skip existing {dst}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return f"wrote {dst}"


def _runtime_module_path(module: str) -> Path:
    """Path to a package runtime module for subprocess entrypoints."""
    return package_file(f"{module}.py")


def cmd_init(args: argparse.Namespace) -> int:
    """Prepare project state. Product runtime comes from the installed package."""
    project = Path(args.project).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    messages: list[str] = []
    # Optional legacy vendoring (deprecated): only with --vendor.
    if getattr(args, "vendor", False):
        messages.append(
            copy_file(package_file("runguard.py"), project / "runguard.py", args.force)
        )
        if args.watch:
            messages.append(
                copy_file(package_file("watch_chat.py"), project / "watch_chat.py", args.force)
            )
        messages.append(
            "note: --vendor copies are deprecated; prefer "
            "`from observer_kit.runguard import start_observed_run`"
        )
    state_dir = (project / args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    runs_dir = state_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    messages.append(f"ready {state_dir}")
    messages.append(f"ready {runs_dir}")
    if args.explain:
        messages.append(
            copy_file(package_file("EXPLAIN.md"), state_dir / "EXPLAIN.md", args.force)
        )
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
    print("  # In workflow.py: from observer_kit.runguard import start_observed_run")
    print("  python3 your_workflow.py --dry-run --limit 10")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    script = _runtime_module_path("run_dashboard")
    argv = [str(script), str(state_dir), "--port", str(args.port)]
    parent_pid = getattr(args, "parent_pid", None)
    if parent_pid is not None:
        argv.extend(["--parent-pid", str(parent_pid)])
    idle = getattr(args, "idle_timeout", None)
    if idle is not None and float(idle) > 0:
        argv.extend(["--idle-timeout", str(idle)])
    old_argv = sys.argv[:]
    try:
        sys.argv = argv
        try:
            runpy.run_path(str(script), run_name="__main__")
        except KeyboardInterrupt:
            print()
            return 130
    finally:
        sys.argv = old_argv
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Run the package-oriented acceptance suite."""
    tests_dir = ROOT / "tests"
    if not tests_dir.is_dir():
        # Installed wheel may not ship tests; fall back to source checkout only.
        raise SystemExit(f"tests directory not found: {tests_dir}")
    package_dir = str(PACKAGE_ROOT)
    # runguard subprocess tests use `import runguard` via import_shims.
    shim_dir = str(tests_dir / "import_shims")
    skill_dir = str(ROOT / ".claude" / "skills" / "observer-kit")
    flow_skill_dir = ROOT / ".claude" / "skills" / "observer-flow"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), shim_dir, env.get("PYTHONPATH", "")]
    )

    tests = [
        [sys.executable, "-B", str(tests_dir / "test_runguard.py"), shim_dir],
        [sys.executable, "-B", str(tests_dir / "test_data_movement.py"), shim_dir],
        [sys.executable, "-B", str(tests_dir / "test_lint_emit.py")],
        [sys.executable, "-B", str(tests_dir / "test_skill.py"), skill_dir],
        [sys.executable, "-B", str(tests_dir / "test_dashboard.py")],
        [sys.executable, "-B", str(tests_dir / "test_dashboard_browser.py")],
        [sys.executable, "-B", str(tests_dir / "test_cli.py")],
    ]
    if flow_skill_dir.is_dir():
        tests.extend([
            [sys.executable, "-B", str(tests_dir / "test_validate_flow.py"),
             str(flow_skill_dir)],
            [sys.executable, "-B", str(tests_dir / "test_flow_skill.py"),
             str(flow_skill_dir)],
        ])
    demo_test = ROOT / "examples" / "observer-flow-demo" / "test_flow_coordinator.py"
    if demo_test.is_file():
        tests.append([sys.executable, "-B", str(demo_test)])
    for cmd in tests:
        rc = subprocess.call(cmd, env=env)
        if rc:
            return rc
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    """Run the emission/durability static check on a workflow script."""
    script = Path(args.script).expanduser().resolve()
    if not script.is_file():
        raise SystemExit(f"not a file: {script}")
    lint = package_file("lint_emit.py")
    return subprocess.call([sys.executable, "-B", str(lint), str(script)])


def _lane_from_run_id(run_id: object) -> str:
    raw = str(run_id or "").strip()
    if not raw or raw == "all":
        return ""
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    raw = Path(raw).name
    if raw.endswith(".jsonl"):
        raw = raw[:-6]
    if not raw or raw in {".", ".."}:
        return ""
    return raw


def _chat_path(state_dir: Path, run_id: object | None = None) -> Path:
    """Per-lane chat file under runs/<lane>/; root for project-wide ``all``."""
    lane = _lane_from_run_id(run_id)
    if lane:
        return state_dir / "runs" / lane / "chat.jsonl"
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


_AGENT_STATUSES = frozenset({"listening", "responding", "idle"})


def _append_agent_status(
    state_dir: Path,
    run_id: str,
    status: str,
    *,
    pid: int | None = None,
) -> None:
    """Mark agent presence for the dashboard: listening | responding | idle.

    ``listening`` means a poll is waiting for the operator. ``responding`` means
    a note was delivered and the session is working. ``idle`` clears both.
    """
    if status not in _AGENT_STATUSES or not run_id:
        return
    labels = {
        "listening": "Agent is listening",
        "responding": "Agent is responding",
        "idle": "Agent idle",
    }
    rec = {
        "ts": _timestamp(),
        "run": str(run_id),
        "anchor": "run",
        "author": "system",
        "kind": "agent_status",
        "status": status,
        "text": labels[status],
    }
    if pid is not None:
        rec["pid"] = int(pid)
    path = _chat_path(state_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def _chat_sig(message: dict) -> str:
    return json.dumps(
        [message.get("ts"), message.get("run"), message.get("anchor"), message.get("text")],
        ensure_ascii=False,
        sort_keys=True,
    )


def _chat_read_paths(state_dir: Path, run_id: object | None = None, *, all_runs: bool = False) -> list[Path]:
    paths: list[Path] = []
    if all_runs or not run_id:
        paths.append(state_dir / "chat.jsonl")
        runs_root = state_dir / "runs"
        if runs_root.is_dir():
            for child in sorted(runs_root.iterdir()):
                if child.is_dir():
                    paths.append(child / "chat.jsonl")
    else:
        paths.append(_chat_path(state_dir, run_id))
        paths.append(state_dir / "chat.jsonl")
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in paths:
        key = path.resolve() if path.exists() else path
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def _load_chat_messages(
    state_dir: Path,
    run_id: object | None = None,
    *,
    all_runs: bool = False,
) -> list[dict]:
    out: list[dict] = []
    for path in _chat_read_paths(state_dir, run_id, all_runs=all_runs):
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    return out


def _wakes_agent(message: dict, run_id: str | None, all_runs: bool) -> bool:
    if message.get("author") != "user" and message.get("kind") != "control":
        return False
    if all_runs:
        return True
    return message.get("run") == run_id


def _stream_watcher_line(line: str, state_dir: Path | None = None) -> None:
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
    # User notes / control wakes mean the agent session is about to act — show spinner.
    if state_dir and run and (
        payload.get("author") == "user" or payload.get("kind") == "control"
    ):
        try:
            _append_agent_status(state_dir, str(run), "responding")
        except OSError:
            pass


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
        str(_runtime_module_path("watch_chat")),
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
            _stream_watcher_line(line, state_dir=state_dir)
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
    path = _chat_path(state_dir, args.run)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    try:
        _append_agent_status(state_dir, args.run, "idle")
    except OSError:
        pass
    print(f"replied to {args.run} {args.anchor}")
    return 0


def cmd_agent_status(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    if args.listening:
        status = "listening"
    elif args.responding:
        status = "responding"
    else:
        status = "idle"
    pid = os.getpid() if status == "listening" else None
    _append_agent_status(state_dir, args.run, status, pid=pid)
    print(f"agent status {status} for {args.run}")
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    """AXI-style long-poll: mark listening, block until a dashboard note, exit.

    Like ``lavish-axi poll``: leave this running (or re-run if the harness times
    out). Notes stay durable in chat.jsonl either way. On delivery, presence
    flips to responding so the dashboard spinner shows; reply then sets idle.
    """
    state_dir = Path(args.state_dir).expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    if args.all and args.run:
        raise SystemExit("choose --run or --all, not both")
    if not args.all and not args.run:
        raise SystemExit("--run is required unless --all is set")
    if args.reply and args.all:
        raise SystemExit("--reply requires --run (one lane)")

    run_id = None if args.all else str(args.run)
    all_runs = bool(args.all)

    if args.reply:
        text = args.reply.strip()
        if not text:
            raise SystemExit("--reply text must be non-empty")
        rec = {
            "ts": _timestamp(),
            "run": run_id,
            "anchor": args.anchor,
            "author": "agent",
            "text": text,
            "resolved": bool(args.resolved),
        }
        path = _chat_path(state_dir, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        print(f"replied to {run_id} {args.anchor}", flush=True)

    # Presence: listening while we wait. Clean up to idle on every exit path
    # except delivery (which becomes responding for the harness/agent turn).
    presence_run = run_id or "all"
    delivered = False

    def _mark_idle() -> None:
        if delivered:
            return
        try:
            if all_runs:
                # Clear the project-wide marker used when --all is set.
                _append_agent_status(state_dir, "all", "idle")
            elif run_id:
                _append_agent_status(state_dir, run_id, "idle")
        except OSError:
            pass

    if all_runs:
        _append_agent_status(state_dir, "all", "listening", pid=os.getpid())
    else:
        _append_agent_status(state_dir, run_id, "listening", pid=os.getpid())

    seen = set()
    if not args.include_existing:
        for message in _load_chat_messages(state_dir, run_id, all_runs=all_runs):
            if _wakes_agent(message, run_id, all_runs):
                seen.add(_chat_sig(message))

    poll = max(0.05, float(args.poll))
    deadline = (time.time() + float(args.timeout)) if args.timeout else None
    last_heartbeat = time.time()
    print(
        f"[observer] listening for dashboard notes"
        f" ({'all runs' if all_runs else run_id}); leave this poll running",
        file=sys.stderr,
        flush=True,
    )
    try:
        while True:
            fresh = []
            for message in _load_chat_messages(state_dir, run_id, all_runs=all_runs):
                if not _wakes_agent(message, run_id, all_runs):
                    continue
                sig = _chat_sig(message)
                if sig in seen:
                    continue
                seen.add(sig)
                fresh.append(message)
            if fresh:
                delivered = True
                # Flip presence so the UI shows work-in-progress until reply.
                targets = {str(m.get("run") or "") for m in fresh if m.get("run")}
                if not targets and run_id:
                    targets = {run_id}
                for target in sorted(targets):
                    if target:
                        _append_agent_status(state_dir, target, "responding")
                for message in fresh:
                    print(
                        "OBSERVER_CHAT_EVENT "
                        + json.dumps(message, ensure_ascii=False, default=str),
                        flush=True,
                    )
                    text = message.get("text", "")
                    print(
                        f"[observer] dashboard note for {message.get('run')} "
                        f"{message.get('anchor')}: {text}",
                        flush=True,
                    )
                # Compact next-step hints (AXI-style contextual disclosure).
                sample_run = next(
                    (str(m.get("run")) for m in fresh if m.get("run")),
                    run_id or "<run-id>",
                )
                print(
                    "help[3]: "
                    f"Inspect the run/ledger, then `observer-kit reply {state_dir} "
                    f"--run {sample_run} --text \"...\" [--resolved]`; "
                    f"re-run `observer-kit poll {state_dir} --run {sample_run}` "
                    "to keep listening; notes stay durable if the poll exits.",
                    flush=True,
                )
                if args.follow:
                    delivered = False  # keep listening after a batch
                    if all_runs:
                        _append_agent_status(
                            state_dir, "all", "listening", pid=os.getpid(),
                        )
                    elif run_id:
                        _append_agent_status(
                            state_dir, run_id, "listening", pid=os.getpid(),
                        )
                    continue
                return 0
            if deadline is not None and time.time() >= deadline:
                print("[observer] poll timed out with no new notes", file=sys.stderr, flush=True)
                return 0
            now = time.time()
            if now - last_heartbeat >= 30:
                print("[observer] still listening…", file=sys.stderr, flush=True)
                last_heartbeat = now
            time.sleep(poll)
    except KeyboardInterrupt:
        print("[observer] poll interrupted", file=sys.stderr, flush=True)
        return 130
    finally:
        _mark_idle()


def _dashboard_state_dir(port: int) -> Path | None:
    """Return the state directory a dashboard on ``port`` is serving, if known."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=0.35) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (OSError, URLError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    raw = payload.get("state_dir") or payload.get("runguard") or ""
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _dashboard_serves(state_dir: Path, port: int) -> bool:
    """True only when a dashboard on ``port`` is bound to this state directory.

    A bare ``/api/runs`` 200 is not enough: another project's dashboard may own
    the port while this run writes ledgers elsewhere (operator watches empty UI).
    """
    serving = _dashboard_state_dir(port)
    if serving is None:
        return False
    try:
        return serving == state_dir.expanduser().resolve()
    except OSError:
        return False


def _start_dashboard(
    state_dir: Path,
    port: int,
    *,
    parent_pid: int | None = None,
    idle_timeout: float | None = None,
) -> tuple[subprocess.Popen | None, bool]:
    state_dir = state_dir.expanduser().resolve()
    if _dashboard_serves(state_dir, port):
        return None, True
    # Port occupied by a different state dir — fail loud instead of attaching wrong.
    other = _dashboard_state_dir(port)
    if other is not None and other != state_dir:
        raise SystemExit(
            f"dashboard port {port} is already serving {other}, not {state_dir}.\n"
            "Pick --port for a free port, or stop the other dashboard first."
        )
    # Port may be free, or an old server without /api/meta — probe runs only as a
    # soft "something is up" signal; refuse attach without matching state.
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/runs", timeout=0.25) as response:
            if response.status == 200 and other is None:
                raise SystemExit(
                    f"dashboard already responds on port {port} but does not expose "
                    f"/api/meta state_dir; cannot verify it serves {state_dir}.\n"
                    "Upgrade the dashboard process or use a free --port."
                )
    except SystemExit:
        raise
    except (OSError, URLError):
        pass
    cmd = [
        sys.executable,
        str(_runtime_module_path("run_dashboard")),
        str(state_dir),
        "--port",
        str(port),
    ]
    # Bind dashboard lifetime to the launcher unless the operator asked to keep it.
    if parent_pid is not None:
        cmd.extend(["--parent-pid", str(parent_pid)])
    if idle_timeout is not None and float(idle_timeout) > 0:
        cmd.extend(["--idle-timeout", str(idle_timeout)])
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    for _ in range(40):
        if _dashboard_serves(state_dir, port):
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
            str(_runtime_module_path("watch_chat")),
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
                    _stream_watcher_line(watcher_line, state_dir=state_dir)

            watcher_thread = threading.Thread(target=pump_watcher, daemon=True)
            watcher_thread.start()

    if args.dashboard:
        # Bind child dashboard to this process unless --keep-dashboard (intentional orphan).
        dash_parent = None if args.keep_dashboard else os.getpid()
        dash_idle = getattr(args, "idle_timeout", None)
        dashboard_proc, dashboard_attached = _start_dashboard(
            state_dir,
            args.port,
            parent_pid=dash_parent,
            idle_timeout=dash_idle,
        )
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


DASHBOARD_META_NAME = ".observer-dashboard.json"
# Ports scanned by ``ps`` / ``stop`` when discovering live dashboards without a meta file.
_DASHBOARD_PORT_SCAN = range(8484, 8521)


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def _dashboard_meta_path(state_dir: Path) -> Path:
    return state_dir.expanduser().resolve() / DASHBOARD_META_NAME


def _load_dashboard_meta(state_dir: Path) -> dict | None:
    path = _dashboard_meta_path(state_dir)
    if not path.is_file():
        return None
    meta = _read_json_file(path)
    return meta or None


def _probe_dashboard_port(port: int) -> dict | None:
    """Return live dashboard info from /api/meta when something answers on port."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/meta", timeout=0.25) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (OSError, URLError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("state_dir") or payload.get("runguard") or ""
    if not raw:
        return None
    try:
        state = str(Path(raw).expanduser().resolve())
    except OSError:
        state = str(raw)
    pid = payload.get("pid")
    parent = payload.get("parent_pid")
    return {
        "kind": "dashboard",
        "source": "live",
        "port": int(payload.get("port") or port),
        "pid": pid,
        "parent_pid": parent,
        "state_dir": state,
        "idle_timeout": payload.get("idle_timeout"),
        "active": True,
        "orphan": bool(parent) and not _pid_alive(parent),
        "pid_alive": _pid_alive(pid) if pid is not None else True,
    }


def _dashboard_records(state_dirs: list[Path] | None, *, scan_ports: bool) -> list[dict]:
    """Inventory dashboards from meta files and/or live port probes."""
    found: dict[tuple, dict] = {}

    def _key(rec: dict) -> tuple:
        return (rec.get("port"), rec.get("state_dir"), rec.get("pid"))

    def _add(rec: dict) -> None:
        if not rec:
            return
        found[_key(rec)] = rec

    for state in state_dirs or []:
        meta = _load_dashboard_meta(state)
        if not meta:
            continue
        pid = meta.get("pid")
        parent = meta.get("parent_pid")
        alive = _pid_alive(pid) if pid is not None else False
        port = meta.get("port")
        live = _probe_dashboard_port(int(port)) if port is not None and alive else None
        rec = {
            "kind": "dashboard",
            "source": "meta",
            "port": port,
            "pid": pid,
            "parent_pid": parent,
            "state_dir": str(state.expanduser().resolve()),
            "idle_timeout": meta.get("idle_timeout"),
            "started": meta.get("started"),
            "active": bool(meta.get("active", True)) and alive,
            "pid_alive": alive,
            "orphan": bool(parent) and not _pid_alive(parent) and alive,
        }
        if live:
            rec["source"] = "meta+live"
            rec["orphan"] = live.get("orphan", rec["orphan"])
            rec["pid"] = live.get("pid") or rec["pid"]
        _add(rec)

    if scan_ports:
        for port in _DASHBOARD_PORT_SCAN:
            live = _probe_dashboard_port(port)
            if live:
                _add(live)

    return sorted(
        found.values(),
        key=lambda r: (int(r.get("port") or 0), str(r.get("state_dir") or "")),
    )


def _watcher_records(state_dir: Path) -> list[dict]:
    out = []
    for watcher in _active_watchers(state_dir):
        parent = watcher.get("parent_pid")
        pid = watcher.get("pid")
        dead_parent = (
            parent is not None and not _pid_alive(parent) and _pid_alive(pid)
        )
        out.append({
            "kind": "watcher",
            "pid": pid,
            "parent_pid": parent,
            "mode": watcher.get("mode"),
            "run": watcher.get("run"),
            "key": watcher.get("key"),
            "started": watcher.get("started"),
            "state_dir": str(state_dir.expanduser().resolve()),
            "pid_alive": _pid_alive(pid),
            # Orphan = still running after the owning parent died.
            "orphan": dead_parent,
            "independent": parent is None,
        })
    return out


def _terminate_pid(pid: object, *, wait: float = 2.0) -> str:
    """SIGTERM then SIGKILL a process. Returns action label."""
    try:
        pid_i = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "skip"
    if pid_i <= 0 or not _pid_alive(pid_i):
        return "dead"
    try:
        os.kill(pid_i, 15)  # SIGTERM
    except OSError:
        return "error"
    deadline = time.time() + wait
    while time.time() < deadline:
        if not _pid_alive(pid_i):
            return "terminated"
        time.sleep(0.05)
    try:
        os.kill(pid_i, 9)  # SIGKILL
    except OSError:
        return "error"
    return "killed"


def cmd_ps(args: argparse.Namespace) -> int:
    """List Observer dashboards and watchers (and flag orphans)."""
    unique_dirs = _resolve_state_dirs(args)
    scan = not bool(getattr(args, "no_scan", False))
    dashboards = _dashboard_records(unique_dirs or None, scan_ports=scan)

    watchers: list[dict] = []
    for path in unique_dirs:
        watchers.extend(_watcher_records(path))

    if not dashboards and not watchers:
        print("no observer dashboards or watchers found")
        if not unique_dirs:
            print("tip: pass a state dir to list watchers, e.g. observer-kit ps .observer")
        return 0

    if dashboards:
        print("dashboards:")
        for rec in dashboards:
            flags = []
            if rec.get("orphan"):
                flags.append("ORPHAN")
            if rec.get("parent_pid") is None and rec.get("pid_alive"):
                flags.append("independent")
            if not rec.get("pid_alive", True):
                flags.append("dead")
            if not rec.get("active", True):
                flags.append("inactive")
            flag_s = f" [{' '.join(flags)}]" if flags else ""
            print(
                f"  port={rec.get('port')} pid={rec.get('pid')} "
                f"parent={rec.get('parent_pid') or 'independent'} "
                f"state={rec.get('state_dir')}{flag_s}"
            )
    if watchers:
        print("watchers:")
        for rec in watchers:
            flags = []
            if rec.get("orphan"):
                flags.append("ORPHAN")
            if rec.get("independent"):
                flags.append("independent")
            flag_s = f" [{' '.join(flags)}]" if flags else ""
            target = "all runs" if rec.get("mode") == "all" else rec.get("run")
            print(
                f"  pid={rec.get('pid')} parent={rec.get('parent_pid') or 'independent'} "
                f"target={target} state={rec.get('state_dir')}{flag_s}"
            )
    orphans = sum(1 for r in dashboards + watchers if r.get("orphan"))
    if orphans:
        print(
            f"{orphans} orphan(s) — reaping: observer-kit stop --orphans"
            + (f" {unique_dirs[0]}" if unique_dirs else "")
        )
    return 0


def _resolve_state_dirs(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    if getattr(args, "state_dir", None):
        paths.append(Path(args.state_dir).expanduser().resolve())
    for extra in getattr(args, "state_dirs", None) or []:
        paths.append(Path(extra).expanduser().resolve())
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _should_stop_process(
    rec: dict,
    *,
    stop_all: bool,
    independent: bool,
    orphans_only: bool,
) -> bool:
    if not rec.get("pid_alive", True) and not _pid_alive(rec.get("pid")):
        return False
    if stop_all:
        return True
    if rec.get("orphan"):
        return True
    if independent and rec.get("parent_pid") is None and _pid_alive(rec.get("pid")):
        return True
    if orphans_only:
        return False
    return False


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop Observer dashboards/watchers (orphans by default, or --all)."""
    unique_dirs = _resolve_state_dirs(args)
    stop_all = bool(getattr(args, "all", False))
    independent = bool(getattr(args, "independent", False)) or bool(
        getattr(args, "sweep", False)
    )
    # Default mode: dead-parent orphans. --sweep also drops independent processes.
    orphans_only = not stop_all
    port_filter = getattr(args, "port", None)
    dry_run = bool(getattr(args, "dry_run", False))

    # Discover dashboards via port scan always; filter by state dir when given.
    dashboards = _dashboard_records(unique_dirs or None, scan_ports=True)
    if unique_dirs:
        want = {str(p) for p in unique_dirs}
        dashboards = [
            d for d in dashboards
            if str(Path(str(d.get("state_dir") or ".")).expanduser().resolve()) in want
        ]
    if port_filter is not None:
        dashboards = [d for d in dashboards if int(d.get("port") or -1) == int(port_filter)]

    watchers: list[dict] = []
    for path in unique_dirs:
        watchers.extend(_watcher_records(path))

    targets: list[tuple[str, dict]] = []
    for rec in dashboards:
        if _should_stop_process(
            rec, stop_all=stop_all, independent=independent, orphans_only=orphans_only,
        ):
            targets.append(("dashboard", rec))
    for rec in watchers:
        if _should_stop_process(
            rec, stop_all=stop_all, independent=independent, orphans_only=orphans_only,
        ):
            targets.append(("watcher", rec))

    seen_pids: set[tuple[str, int]] = set()
    unique_targets: list[tuple[str, dict]] = []
    for kind, rec in targets:
        try:
            pid_i = int(rec.get("pid"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        key = (kind, pid_i)
        if key in seen_pids:
            continue
        seen_pids.add(key)
        unique_targets.append((kind, rec))

    if not unique_targets:
        hint = " (no orphans; use --all, --independent, or --port)" if orphans_only else ""
        print(f"nothing to stop{hint}")
        return 0

    stopped = 0
    for kind, rec in unique_targets:
        pid = rec.get("pid")
        if kind == "dashboard":
            label = (
                f"dashboard pid={pid} port={rec.get('port')} state={rec.get('state_dir')}"
            )
        else:
            target = rec.get("run") or rec.get("mode")
            label = f"watcher pid={pid} target={target} state={rec.get('state_dir')}"
        if dry_run:
            print(f"would stop {label}")
            stopped += 1
            continue
        action = _terminate_pid(pid)
        print(f"{action} {label}")
        if action in {"terminated", "killed", "dead"}:
            stopped += 1
            if kind == "dashboard" and rec.get("state_dir"):
                meta_path = _dashboard_meta_path(Path(str(rec["state_dir"])))
                if meta_path.is_file():
                    meta = _read_json_file(meta_path)
                    if meta.get("pid") in (None, pid):
                        meta = dict(
                            meta,
                            active=False,
                            stopped=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        )
                        try:
                            meta_path.write_text(
                                json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n",
                                encoding="utf-8",
                            )
                        except OSError:
                            pass
    print(f"stop complete ({stopped} process(es))")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    project = Path(args.project).expanduser().resolve()
    checks = []
    checks.append(("project exists", project.exists()))
    checks.append(("package runguard available", package_file("runguard.py").exists()))
    checks.append(("package dashboard available", package_file("run_dashboard.py").exists()))
    checks.append(("package dashboard asset available", package_file("assets/dashboard.js").exists()))
    checks.append(("package watcher available", package_file("watch_chat.py").exists()))
    checks.append(("state dir exists", (project / args.state_dir).exists()))
    checks.append(
        ("state dir ignores local ledger data",
         (project / args.state_dir / ".gitignore").exists())
    )
    checks.append(
        ("operator explainer exists",
         (project / args.state_dir / "EXPLAIN.md").exists())
    )
    checks.append(("runs/ home exists", (project / args.state_dir / "runs").is_dir()))

    ok = True
    for label, passed in checks:
        ok = ok and passed
        print(f"{'OK ' if passed else 'ERR'} {label}")

    # Deprecated vendored copies: warn but do not fail.
    vendored = []
    if (project / "runguard.py").exists():
        vendored.append("runguard.py")
    if (project / "watch_chat.py").exists():
        vendored.append("watch_chat.py")
    if vendored:
        print()
        print(
            "WARN deprecated vendored product files in project: "
            + ", ".join(vendored)
        )
        print(
            "     Prefer package imports: "
            "from observer_kit.runguard import start_observed_run"
        )
        print("     Remove the copies after migrating workflows.")

    if not ok:
        print()
        print(f"Run: observer-kit init {project}")
        print("And: python -m pip install -e .   # package provides runtime")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="observer-kit",
        description="Initialize and run Observer Kit guardrails for risky batch scripts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  observer-kit init .
  observer-kit dashboard .observer
  observer-kit poll .observer --run runguard:my-run
  observer-kit watch .observer --all --follow
  observer-kit run --state-dir .observer -- python3 workflow.py --dry-run --limit 10
  observer-kit ps .observer
  observer-kit stop --sweep .observer
  observer-kit test
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init",
        help="create .observer state home (package provides runtime)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  observer-kit init .
  observer-kit init ./my-project --force

next:
  observer-kit dashboard ./my-project/.observer
  observer-kit watch ./my-project/.observer --all --follow
  # from observer_kit.runguard import start_observed_run
""",
    )
    init.add_argument("project", nargs="?", default=".", help="target project directory")
    init.add_argument("--state-dir", default=".observer", help="state directory inside the project")
    init.add_argument("--force", action="store_true", help="overwrite existing files")
    init.add_argument("--no-explain", dest="explain", action="store_false",
                      help="do not copy EXPLAIN.md into the state dir")
    init.add_argument("--no-gitignore", dest="gitignore", action="store_false",
                      help="do not write a state-dir .gitignore")
    init.add_argument(
        "--vendor",
        action="store_true",
        help="(deprecated) copy runguard.py/watch_chat.py into the project",
    )
    init.add_argument("--no-watch", dest="watch", action="store_false",
                      help="with --vendor, skip watch_chat.py")
    init.set_defaults(func=cmd_init, explain=True, gitignore=True, watch=True, vendor=False)

    lint = sub.add_parser(
        "lint",
        help="static check for row liveness and durable emit patterns",
    )
    lint.add_argument("script", help="workflow script path")
    lint.set_defaults(func=cmd_lint)

    dash = sub.add_parser(
        "dashboard",
        help="run the localhost dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  observer-kit dashboard .observer
  observer-kit dashboard ./my-project/.observer --port 8485

  # Bind lifetime to this shell (exits when the shell dies):
  observer-kit dashboard .observer --parent-pid $$

  # Auto-exit after 30 minutes with no browser/API traffic:
  observer-kit dashboard .observer --idle-timeout 1800

For long-lived monitoring, keep this server running and launch pipelines in
separate shells with observer-kit run --state-dir <same-dir> ...
Inventory/reap: observer-kit ps ; observer-kit stop --orphans
""",
    )
    dash.add_argument("state_dir", nargs="?", default=".observer", help="ledger/state directory")
    dash.add_argument("--port", type=int, default=8484, help="dashboard port")
    dash.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help="exit when this PID dies (prevents orphan dashboards)",
    )
    dash.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        help="exit after N seconds with no HTTP traffic (0/omit = no idle exit). "
             "Also set OBSERVER_DASHBOARD_IDLE.",
    )
    dash.set_defaults(func=cmd_dashboard)

    ps = sub.add_parser(
        "ps",
        help="list Observer dashboards and watchers (flags orphans)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  observer-kit ps
  observer-kit ps .observer
  observer-kit ps .observer /tmp/other/.observer
""",
    )
    ps.add_argument(
        "state_dir",
        nargs="?",
        default=None,
        help="state directory (lists watchers; optional)",
    )
    ps.add_argument(
        "state_dirs",
        nargs="*",
        default=[],
        help="additional state directories",
    )
    ps.add_argument(
        "--no-scan",
        action="store_true",
        help="do not scan ports 8484-8520 for live dashboards",
    )
    ps.set_defaults(func=cmd_ps)

    stop = sub.add_parser(
        "stop",
        help="stop Observer dashboards/watchers (orphans by default)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # Reap dashboards/watchers whose parent PID is dead:
  observer-kit stop --orphans
  observer-kit stop --orphans .observer

  # Stop everything for a state dir (including intentional long-lived):
  observer-kit stop --all .observer

  # Stop one port:
  observer-kit stop --port 8485 --all

  # Dry run:
  observer-kit stop --orphans --dry-run
""",
    )
    stop.add_argument(
        "state_dir",
        nargs="?",
        default=None,
        help="state directory (required to stop watchers)",
    )
    stop.add_argument(
        "state_dirs",
        nargs="*",
        default=[],
        help="additional state directories",
    )
    stop.add_argument(
        "--orphans",
        action="store_true",
        default=True,
        help="stop processes whose parent PID is dead (default)",
    )
    stop.add_argument(
        "--all",
        action="store_true",
        help="stop all matching dashboards/watchers (not only orphans)",
    )
    stop.add_argument(
        "--independent",
        action="store_true",
        help="also stop processes started without --parent-pid",
    )
    stop.add_argument(
        "--sweep",
        action="store_true",
        help="end-of-session cleanup: orphans + independent (battery-friendly)",
    )
    stop.add_argument("--port", type=int, help="only dashboards on this port")
    stop.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be stopped without killing",
    )
    stop.set_defaults(func=cmd_stop)

    watch = sub.add_parser(
        "watch",
        help="bridge dashboard chat to stdout for the active harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # Preferred with a long-lived dashboard: bridge notes from any run.
  observer-kit watch .observer --all --follow

  # Scoped bridge for one run id.
  observer-kit watch .observer --run runguard:my-run --follow

The watcher is transport only. It emits OBSERVER_CHAT_EVENT lines; the harness
session remains responsible for inspecting data, editing scripts, rerunning,
and replying.

Long-lived followers register ownership in the state directory. The same run
reuses one watcher, different run IDs remain independent, and --all is the
single-session project-wide mode.
""",
    )
    watch.add_argument("state_dir", nargs="?", default=".observer", help="ledger/state directory")
    watch.add_argument("--run", help="run id, e.g. runguard:my-run")
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
  observer-kit reply .observer \\
    --run runguard:my-run \\
    --anchor 'cell:companies::acme|companies::status' \\
    --resolved \\
    --text "Fixed the parser and reran the sample."
""",
    )
    reply.add_argument("state_dir", nargs="?", default=".observer", help="ledger/state directory")
    reply.add_argument("--run", required=True, help="run id to reply to")
    reply.add_argument("--anchor", default="run", help="dashboard anchor/cell id")
    reply.add_argument("--resolved", action="store_true", help="mark the note resolved")
    reply.add_argument("--text", required=True, help="reply text")
    reply.set_defaults(func=cmd_reply)

    agent_status = sub.add_parser(
        "agent-status",
        help="mark agent presence: listening, responding, or idle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  observer-kit agent-status .observer --run runguard:my-run --listening
  observer-kit agent-status .observer --run runguard:my-run --responding
  observer-kit agent-status .observer --run runguard:my-run --idle
""",
    )
    agent_status.add_argument("state_dir", nargs="?", default=".observer",
                              help="ledger/state directory")
    agent_status.add_argument("--run", required=True, help="run id shown in the dashboard")
    mode = agent_status.add_mutually_exclusive_group(required=True)
    mode.add_argument("--listening", action="store_true",
                      help="show that a poll is waiting for operator notes")
    mode.add_argument("--responding", action="store_true",
                      help="show the agent-responding spinner")
    mode.add_argument("--idle", action="store_true",
                      help="clear listening/responding presence")
    agent_status.set_defaults(func=cmd_agent_status)

    poll = sub.add_parser(
        "poll",
        help="long-poll for dashboard notes (AXI-style agent respond loop)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # Leave running while the operator reviews the dashboard.
  observer-kit poll .observer --run runguard:scroll-demo

  # After acting, reply and listen again (Lavish --agent-reply pattern).
  observer-kit poll .observer --run runguard:scroll-demo \\
    --reply "Fixed the parser and reran the sample." --resolved

  # Project-wide bridge for one long-lived agent session.
  observer-kit poll .observer --all

Poll marks the run as listening so the dashboard shows agent presence. When a
user note or control arrives it prints OBSERVER_CHAT_EVENT lines, flips to
responding, and exits (unless --follow). Re-run poll after you reply. Notes are
never lost if the poll times out — re-run with --include-existing if needed.
""",
    )
    poll.add_argument("state_dir", nargs="?", default=".observer",
                      help="ledger/state directory")
    poll.add_argument("--run", help="run id, e.g. runguard:my-run")
    poll.add_argument("--all", action="store_true",
                      help="listen for notes on any run in this state dir")
    poll.add_argument("--poll", type=float, default=1.0,
                      help="poll interval in seconds (default 1)")
    poll.add_argument("--timeout", type=float, default=0,
                      help="seconds to wait; 0 = wait forever")
    poll.add_argument("--include-existing", action="store_true",
                      help="also surface notes already in chat.jsonl")
    poll.add_argument("--follow", action="store_true",
                      help="keep listening after each batch (stream)")
    poll.add_argument("--reply", help="post an agent reply before listening")
    poll.add_argument("--anchor", default="run",
                      help="anchor for --reply (default run)")
    poll.add_argument("--resolved", action="store_true",
                      help="mark --reply as resolved")
    poll.set_defaults(func=cmd_poll)

    run = sub.add_parser(
        "run",
        help="run a command with Observer Kit state and watcher plumbing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  # Dry-run sample in the current source lane.
  observer-kit run --state-dir .observer -- python3 workflow.py --dry-run --limit 10

  # Retry/fix/continue the same source data in the same dashboard run.
  observer-kit run --state-dir .observer --session july-import \\
    -- python3 workflow.py --full-run

  # Start a separate comparison or new batch run.
  observer-kit run --state-dir .observer --session auto \\
    -- python3 workflow.py --full-run

  # Quick demo: launch a temporary dashboard with this command.
  observer-kit run --state-dir .observer --dashboard \\
    -- python3 workflow.py --dry-run --limit 10

For persistent monitoring, prefer:
  observer-kit dashboard .observer
  observer-kit watch .observer --all --follow
  observer-kit run --state-dir .observer --session source-id -- ...

Session rule:
  same source retry/adaptation = reuse the same session or omit --session
  add enrichment to existing rows = reuse the same session, table, and keys
  clean comparison/new batch   = use --session auto or a new session name
""",
    )
    run.add_argument("--state-dir", default=".observer", help="ledger/state directory")
    run.add_argument("--dashboard", action="store_true", help="start a dashboard for this run")
    run.add_argument("--port", type=int, default=8484, help="dashboard port")
    run.add_argument("--keep-dashboard", action="store_true",
                     help="leave the dashboard process running after the command exits "
                          "(does not bind --parent-pid; prefer observer-kit stop later)")
    run.add_argument("--idle-timeout", type=float, default=None,
                     help="when starting a dashboard, exit after N idle seconds")
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
    doctor.add_argument("--state-dir", default=".observer", help="state directory inside the project")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
