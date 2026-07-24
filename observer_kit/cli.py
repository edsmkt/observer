from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen
from pathlib import Path

from observer_kit._util import (
    lane_from_run_id as _lane_from_run_id,
    pid_alive as _pid_alive,
    timestamp as _timestamp,
)
from observer_kit.inventory import (
    DASHBOARD_META_NAME,
    _active_watchers,
    _dashboard_meta_path,
    _load_dashboard_meta,
    _read_json_file,
    _terminate_pid,
    dashboard_records,
    watcher_records,
)


# Package root (observer_kit/) and optional source checkout root (repo/).
PACKAGE_ROOT = Path(__file__).resolve().parent
ROOT = PACKAGE_ROOT.parent if (PACKAGE_ROOT.parent / "pyproject.toml").is_file() else PACKAGE_ROOT


def package_file(name: str) -> Path:
    """Resolve a file shipped with the installable package (runtime + templates)."""
    path = PACKAGE_ROOT / name
    if not path.exists():
        raise SystemExit(
            f"Observer Kit package file not found: {path}\n"
            "Reinstall the package: python -m pip install -e ."
        )
    return path




def copy_file(src: Path, dst: Path, force: bool) -> str:
    if dst.exists() and not force:
        return f"skip existing {dst}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return f"wrote {dst}"


def _runtime_module_path(module: str) -> Path:
    """Path to a package runtime module for subprocess entrypoints."""
    return package_file(f"{module}.py")


# Secrets via 1Password pointers: harness owns resolution; scripts never hold values.
# Not a sandbox — only a possession boundary when --secrets is used.
_OP_POINTER = re.compile(r"^op://\S+$")
# Match ApprovalRequired.exit_code in runguard — full-run without approval.
_SECRETS_APPROVAL_EXIT = 4
_SECRETS_USAGE_EXIT = 2


def _secrets_exit(message: str, code: int = _SECRETS_USAGE_EXIT) -> None:
    """Fail closed for secrets errors (stderr + explicit exit code)."""
    print(message, file=sys.stderr)
    raise SystemExit(code)


def load_secret_pointers(path: Path) -> list[str]:
    """Parse a secrets env file. Every value must be an ``op://`` pointer.

    Returns ordered unique env key names. Exits with code 2 on missing file,
    empty file, or any non-pointer value (keeps the file committable by
    construction).
    """
    path = path.expanduser()
    if not path.is_file():
        _secrets_exit(f"[observer] secrets: file not found: {path}")
    keys: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _secrets_exit(f"[observer] secrets: cannot read {path}: {exc}")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            _secrets_exit(
                f"[observer] secrets: line {lineno}: expected KEY=op://... "
                f"(got {raw!r})"
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            _secrets_exit(f"[observer] secrets: line {lineno}: missing key name")
        if not _OP_POINTER.match(value):
            _secrets_exit(
                f"[observer] secrets: line {lineno}: {key} must be an op:// "
                "pointer only — plain secrets are refused so the file stays "
                "committable"
            )
        keys.append(key)
    if not keys:
        _secrets_exit(f"[observer] secrets: no KEY=op:// entries in {path}")
    seen: set[str] = set()
    unique: list[str] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


def command_implies_dry_run(command: list[str]) -> bool:
    """True when argv looks like a dry-run / sample (secrets may resolve)."""
    for tok in command:
        low = str(tok).strip().lower()
        if low in {"--dry-run", "-n"}:
            return True
        if low.startswith("--dry-run="):
            val = low.split("=", 1)[1]
            if val not in {"0", "false", "no", "off"}:
                return True
    return False


def _secrets_approval_required() -> bool:
    """Same default as runguard: full-run approval on unless explicitly opted out."""
    raw = os.environ.get("OBSERVER_ALLOW_UNAPPROVED_FULL_RUN", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return False
    raw_req = os.environ.get("OBSERVER_REQUIRE_FULL_RUN_APPROVAL", "").strip().lower()
    if raw_req in {"0", "false", "no", "off"}:
        return False
    return True


def list_pending_full_run_approvals(state_dir: Path) -> list[str]:
    """Run ids with a valid unacked ``approve_full_run`` under *state_dir*."""
    state_dir = state_dir.expanduser().resolve()
    prev = os.environ.get("RUNGUARD_STATE_DIR")
    os.environ["RUNGUARD_STATE_DIR"] = str(state_dir)
    try:
        from observer_kit.runguard import pending_full_run_approvals

        candidates: set[str] = set()
        runs = state_dir / "runs"
        if runs.is_dir():
            for lane_dir in runs.iterdir():
                if lane_dir.is_dir() and not lane_dir.name.startswith("."):
                    candidates.add(f"runguard:{lane_dir.name}")
        # Root / legacy controls may tag a run id without a lane folder yet.
        root_controls = state_dir / "controls.jsonl"
        if root_controls.is_file():
            try:
                for line in root_controls.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("kind") != "approve_full_run":
                        continue
                    rid = rec.get("run")
                    if rid:
                        candidates.add(str(rid))
            except OSError:
                pass

        pending: list[str] = []
        for rid in sorted(candidates):
            try:
                if pending_full_run_approvals(rid):
                    pending.append(rid)
            except Exception:
                continue
        return pending
    finally:
        if prev is None:
            os.environ.pop("RUNGUARD_STATE_DIR", None)
        else:
            os.environ["RUNGUARD_STATE_DIR"] = prev


def ensure_secrets_approval(
    state_dir: Path,
    command: list[str],
    *,
    run_id: str | None = None,
) -> None:
    """Refuse to resolve secrets for an unapproved full run (exit 4).

    Dry-run samples may still receive credentials. Full runs (no ``--dry-run``)
    require a pending ``approve_full_run`` unless
    ``OBSERVER_ALLOW_UNAPPROVED_FULL_RUN=1``.
    """
    if command_implies_dry_run(command):
        return
    if not _secrets_approval_required():
        return
    pending = list_pending_full_run_approvals(state_dir)
    if run_id:
        if run_id in pending or any(
            p == run_id or p.endswith(f":{run_id}") for p in pending
        ):
            return
        # Precise check for the requested lane even if scan missed it.
        prev = os.environ.get("RUNGUARD_STATE_DIR")
        os.environ["RUNGUARD_STATE_DIR"] = str(state_dir.expanduser().resolve())
        try:
            from observer_kit.runguard import pending_full_run_approvals

            if pending_full_run_approvals(run_id):
                return
        finally:
            if prev is None:
                os.environ.pop("RUNGUARD_STATE_DIR", None)
            else:
                os.environ["RUNGUARD_STATE_DIR"] = prev
        _secrets_exit(
            f"[observer] secrets: full-run refused — no approve_full_run for "
            f"{run_id}. Review a dry-run sample, approve on the dashboard, then "
            f"retry (exit {_SECRETS_APPROVAL_EXIT}).",
            _SECRETS_APPROVAL_EXIT,
        )
    if pending:
        return
    _secrets_exit(
        "[observer] secrets: full-run refused — no pending approve_full_run in "
        f"{state_dir}. Run a --dry-run sample first (secrets allowed for samples), "
        "approve on the dashboard, then retry the full run "
        f"(exit {_SECRETS_APPROVAL_EXIT}). "
        "Or pass --run-id runguard:LANE after approval. "
        "Opt out: OBSERVER_ALLOW_UNAPPROVED_FULL_RUN=1.",
        _SECRETS_APPROVAL_EXIT,
    )


def _append_jsonl_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def record_secrets_injection(
    state_dir: Path,
    keys: list[str],
    secrets_path: Path,
    *,
    dry_run: bool,
    run_id: str | None = None,
) -> None:
    """Durable names-only audit: state-dir secrets_audit.jsonl + optional lane ledger."""
    from observer_kit._util import lane_from_run_id, timestamp as _ts

    rec = {
        "event": "secrets_injected",
        "ts": _ts(),
        "keys": list(keys),
        "source": str(secrets_path),
        "dry_run": bool(dry_run),
        "pid": os.getpid(),
    }
    if run_id:
        rec["run"] = str(run_id)
    state_dir = state_dir.expanduser().resolve()
    _append_jsonl_record(state_dir / "secrets_audit.jsonl", rec)
    if run_id:
        lane = lane_from_run_id(run_id)
        if lane:
            _append_jsonl_record(
                state_dir / "runs" / lane / "events.jsonl", rec
            )


def wrap_command_with_secrets(
    command: list[str], secrets_path: Path
) -> tuple[list[str], list[str]]:
    """Wrap *command* with ``op run --env-file`` so secrets never enter this process.

    Returns ``(wrapped_command, key_names)``. Requires the 1Password CLI on PATH.
    """
    secrets_path = secrets_path.expanduser().resolve()
    keys = load_secret_pointers(secrets_path)
    op_bin = shutil.which("op")
    if not op_bin:
        _secrets_exit(
            "[observer] secrets: 1Password CLI 'op' not on PATH. "
            "Install it, or omit --secrets."
        )
    wrapped = [
        op_bin,
        "run",
        "--env-file",
        str(secrets_path),
        "--",
        *command,
    ]
    return wrapped, keys


def cmd_init(args: argparse.Namespace) -> int:
    """Prepare project state. Product runtime comes from the installed package."""
    project = Path(args.project).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    messages: list[str] = []
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
    """Run the package-oriented acceptance suite (script-style tests)."""
    tests_dir = ROOT / "tests"
    if not tests_dir.is_dir():
        raise SystemExit(f"tests directory not found: {tests_dir}")
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
        [sys.executable, "-B", str(tests_dir / "test_axi.py")],
        [sys.executable, "-B", str(tests_dir / "test_agent_acceptance.py")],
        [sys.executable, "-B", str(tests_dir / "test_gate.py")],
        [sys.executable, "-B", str(tests_dir / "test_secrets.py")],
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


def cmd_validate_flow(args: argparse.Namespace) -> int:
    """Validate an Observer Flow JSON manifest (structural)."""
    from observer_kit.validate_flow import main as validate_main

    argv = [args.manifest]
    if args.json:
        argv.append("--json")
    return int(validate_main(argv))


def cmd_gate(args: argparse.Namespace) -> int:
    """Side-effect compliance gate (hook-friendly)."""
    from observer_kit.gate import main as gate_main

    argv: list[str] = []
    if getattr(args, "json", False):
        argv.append("--json")
    if getattr(args, "command", None):
        argv.extend(["--command", args.command])
    if getattr(args, "path", None):
        argv.append(args.path)
    return int(gate_main(argv))


def _chat_path(state_dir: Path, run_id: object | None = None) -> Path:
    """Per-lane chat file under runs/<lane>/; root for project-wide ``all``."""
    lane = _lane_from_run_id(run_id)
    if lane:
        return state_dir / "runs" / lane / "chat.jsonl"
    return state_dir / "chat.jsonl"



def _write_chat_message(
    state_dir: Path,
    run_id: object,
    text: str,
    *,
    anchor: str = "run",
    author: str = "agent",
    resolved: bool = False,
    fsync: bool = False,
) -> Path:
    """Append one chat JSONL record; return its path."""
    rec = {
        "ts": _timestamp(),
        "run": run_id,
        "anchor": anchor,
        "author": author,
        "text": text,
        "resolved": bool(resolved),
    }
    path = _chat_path(state_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if fsync:
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
    return path


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
    _write_chat_message(
        state_dir, args.run, text, anchor=args.anchor, resolved=bool(args.resolved),
    )
    try:
        _append_agent_status(state_dir, args.run, "idle")
    except OSError:
        pass
    print(f"replied to {args.run} {args.anchor}")
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
        _write_chat_message(
            state_dir, run_id, text, anchor=args.anchor,
            resolved=bool(args.resolved), fsync=True,
        )
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

    _append_agent_status(
        state_dir, "all" if all_runs else run_id, "listening", pid=os.getpid(),
    )

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
                    _append_agent_status(
                        state_dir, "all" if all_runs else run_id,
                        "listening", pid=os.getpid(),
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


def _lint_workflow_scripts(command: list[str]) -> int:
    """Lint Python workflow scripts in a run command; return lint exit code."""
    lint_script = package_file("lint_emit.py")
    scripts: list[Path] = []
    for i, tok in enumerate(command):
        # Common shapes: python3 workflow.py … / path/to/workflow.py
        if tok.endswith(".py") and not tok.startswith("-"):
            candidate = Path(tok).expanduser()
            if candidate.is_file():
                scripts.append(candidate.resolve())
                continue
        if tok in {"python", "python3", sys.executable} and i + 1 < len(command):
            nxt = command[i + 1]
            if nxt.endswith(".py") and not nxt.startswith("-"):
                candidate = Path(nxt).expanduser()
                if candidate.is_file():
                    scripts.append(candidate.resolve())
    # Deduplicate
    seen: set[str] = set()
    unique: list[Path] = []
    for path in scripts:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    for script in unique:
        print(f"[observer] lint {script}", flush=True)
        rc = subprocess.call([sys.executable, "-B", str(lint_script), str(script)])
        if rc:
            print(
                f"[observer] lint failed for {script} (rc={rc}). "
                "Fix findings or pass --no-lint / OBSERVER_NO_LINT=1 to skip.",
                file=sys.stderr,
                flush=True,
            )
            return rc
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("usage: observer-kit run [options] -- <command> [args...]")

    # Lint as hard gate (opt-out): agents skip optional lint under time pressure.
    no_lint = bool(getattr(args, "no_lint", False)) or os.environ.get(
        "OBSERVER_NO_LINT", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not no_lint:
        lint_rc = _lint_workflow_scripts(command)
        if lint_rc:
            return lint_rc

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

    # Credential possession boundary (opt-in): resolve op:// pointers only for
    # this child. Dry-run samples still get keys. Full runs require pending
    # approve_full_run before op is invoked (no credentials on exit 4).
    secrets_arg = getattr(args, "secrets", None)
    secrets_meta: dict | None = None
    if secrets_arg:
        secrets_path = Path(secrets_arg).expanduser().resolve()
        child_argv = list(command)
        dry_sample = command_implies_dry_run(child_argv)
        ensure_secrets_approval(
            state_dir,
            child_argv,
            run_id=getattr(args, "run_id", None) or None,
        )
        command, secret_keys = wrap_command_with_secrets(child_argv, secrets_path)
        env["OBSERVER_SECRETS"] = ",".join(secret_keys)
        record_secrets_injection(
            state_dir,
            secret_keys,
            secrets_path,
            dry_run=dry_sample,
            run_id=None,
        )
        secrets_meta = {
            "keys": secret_keys,
            "path": secrets_path,
            "dry_run": dry_sample,
            "lane_recorded": False,
        }
        print(
            f"[observer] secrets: injecting {', '.join(secret_keys)} via op run "
            f"(source={secrets_path})",
            flush=True,
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
                if secrets_meta and not secrets_meta.get("lane_recorded"):
                    record_secrets_injection(
                        state_dir,
                        secrets_meta["keys"],
                        secrets_meta["path"],
                        dry_run=bool(secrets_meta.get("dry_run")),
                        run_id=run_id,
                    )
                    secrets_meta["lane_recorded"] = True
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


def cmd_ps(args: argparse.Namespace) -> int:
    """List Observer dashboards and watchers (and flag orphans)."""
    unique_dirs = _resolve_state_dirs(args)
    scan = not bool(getattr(args, "no_scan", False))
    dashboards = dashboard_records(unique_dirs or None, scan_ports=scan)

    watchers: list[dict] = []
    for path in unique_dirs:
        watchers.extend(watcher_records(path))

    if not dashboards and not watchers:
        print("no observer dashboards or watchers found")
        if not unique_dirs:
            print("tip: pass a state dir to list watchers, e.g. observer-kit ps .observer")
        return 0

    def _flags(rec, *, watcher=False):
        flags = []
        if rec.get("orphan"):
            flags.append("ORPHAN")
        if watcher:
            if rec.get("independent"):
                flags.append("independent")
        else:
            if rec.get("parent_pid") is None and rec.get("pid_alive"):
                flags.append("independent")
            if not rec.get("pid_alive", True):
                flags.append("dead")
            if not rec.get("active", True):
                flags.append("inactive")
        return f" [{' '.join(flags)}]" if flags else ""

    if dashboards:
        print("dashboards:")
        for rec in dashboards:
            print(
                f"  port={rec.get('port')} pid={rec.get('pid')} "
                f"parent={rec.get('parent_pid') or 'independent'} "
                f"state={rec.get('state_dir')}{_flags(rec)}"
            )
    if watchers:
        print("watchers:")
        for rec in watchers:
            target = "all runs" if rec.get("mode") == "all" else rec.get("run")
            print(
                f"  pid={rec.get('pid')} parent={rec.get('parent_pid') or 'independent'} "
                f"target={target} state={rec.get('state_dir')}{_flags(rec, watcher=True)}"
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
    dashboards = dashboard_records(unique_dirs or None, scan_ports=True)
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
        watchers.extend(watcher_records(path))

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
    from observer_kit import detect_install_skew, version_info
    from observer_kit.axi import substrate_checks

    project = Path(args.project).expanduser().resolve()
    checks = substrate_checks(project, args.state_dir)
    ok = True
    for item in checks:
        ok = ok and item["ok"]
        print(f"{'OK ' if item['ok'] else 'ERR'} {item['label']}")

    ver = version_info()
    print(f"OK  package version {ver['version']} sha={ver['git_sha']}")
    print(f"    package path {ver['package_path']}")

    skew = detect_install_skew()
    if skew.get("install_skew"):
        print()
        print(f"WARN install_skew: true — {skew.get('reason')}")
        print(f"    PATH binary: {skew.get('path_binary')} version={skew.get('path_version')}")
        print(f"    package:     {skew.get('package_version')} @ {skew.get('package_path')}")
        print(f"    upgrade:     {skew.get('upgrade')}")
        print("    canonical probe: observer-kit axi help")
        print("                     python3 -m observer_kit axi help")
    else:
        print("OK  install_skew: false")

    if not ok:
        print()
        print(f"Run: observer-kit init {project}")
        print(f"And: {skew.get('upgrade') or 'python -m pip install -e .'}")
        return 1
    return 0


# --- AXI (agent eXperience interface) -----------------------------------------


def cmd_axi(args: argparse.Namespace) -> int:
    """Agent-ergonomic surface: TOON stdout, next-step help, no interactive prompts."""
    from observer_kit.axi import dispatch
    return dispatch(args)


def cmd_scaffold(args: argparse.Namespace) -> int:
    """Emit a minimal workflow.py + EXPLAIN seed agents can fill in."""
    dest = Path(args.dest).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = args.source or "source-id"
    key = args.key or "id"
    name = args.name or "workflow"
    paid = bool(getattr(args, "paid_provider", False))
    external = bool(getattr(args, "external_destination", False))
    body = f'''#!/usr/bin/env python3
"""Scaffolded Observer Kit workflow — fill source/transform/destination.

Operate mode: axi home → sample dry-run → review → approve → full-run.
Design mode: see pattern.md + EXPLAIN.md for locks, receipts, resume.
"""
from __future__ import annotations

import argparse
from observer_kit.runguard import (
    ApprovalRequired, RunPaused, input_snapshot, start_observed_run,
)


def parse_args():
    p = argparse.ArgumentParser()
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--full-run", action="store_true")
    p.add_argument("--limit", type=int, default=10)
    return p.parse_args()


def load_rows(limit: int | None):
    # TODO: load from {source!r}; key field {key!r}
    rows = [{{"{key}": "example-1", "name": "Example"}}]
    if limit is not None:
        rows = rows[: max(0, limit)]
    return rows


def transform(row: dict) -> dict:
    # TODO: call provider / map fields
    {"# throttle('provider-account', 5)  # paid provider — share one resource key" if paid else "# no paid provider flag"}
    return {{"name": row.get("name"), "status": "ok"}}


def main() -> int:
    args = parse_args()
    rows = load_rows(args.limit if args.dry_run else None)
    source = {source!r}
    try:
        run = start_observed_run(
            {name!r},
            source=source,
            input_snapshot=input_snapshot(source, records=rows),
            dry_run=args.dry_run,
            description="scaffolded workflow",
            todo=len(rows),
            progress_table="records",
            summary_metrics=[
                {{"key": "processed", "label": "processed"}},
            ],
        )
    except ApprovalRequired as exc:
        print(f"error: approval_required")
        print(f"  run: {{exc.run_id}}")
        return 4
    try:
        run.preview(rows[:5], estimates={{"writes": len(rows)}})
        for row in rows:
            run.check_controls()
            key = str(row[{key!r}])
            with run.step("transform", table="records", key=key, label=row.get("name")):
                result = transform(row)
                ticket = run.write_intent(key, payload=result)
                if run.dry_run:
                    from observer_kit.runguard import ledger
                    ledger(run.scope, "record", table="records", key=key,
                           **result, status="preview")
                elif ticket:
                    # TODO: durable sink write, then receipt
                    {"# external destination — write then fsync before receipt" if external else "# write to destination, then:"}
                    run.write_receipt(ticket, destination_id=key, verified=True,
                                      record_table="records", outcome="written",
                                      record_fields=result)
                run.count("processed")
                run.checkpoint("last_record", key)
            run.check_controls(after_record=True)
        run.success()
        return 0
    except RunPaused:
        raise
    except Exception as exc:
        run.fail(exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
'''
    if dest.exists() and not args.force:
        print(f"skip existing {dest} (pass --force to overwrite)")
        return 1
    dest.write_text(body, encoding="utf-8")
    print(f"wrote {dest}")
    explain = dest.parent / "EXPLAIN.md"
    if not explain.exists() or args.force:
        try:
            src = package_file("EXPLAIN.md")
            explain.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"wrote {explain}")
        except SystemExit:
            explain.write_text(
                f"# {name}\n\nSource: {source}\nKey: {key}\n",
                encoding="utf-8",
            )
            print(f"wrote {explain}")
    print()
    print("Minimum harness checklist:")
    print("  lock → schema_sample → dry-run limit → emit after persist →")
    print("  checkpoint → approval → full-run")
    print()
    print("Next:")
    print(f"  observer-kit lint {dest}")
    print(f"  observer-kit run --state-dir .observer -- python3 {dest} --dry-run --limit 5")
    print("  # review dashboard → Approve full run →")
    print(f"  observer-kit run --state-dir .observer -- python3 {dest} --full-run")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="observer-kit",
        description=(
            "Agents: use observer-kit axi help. Humans: observer-kit dashboard.\n"
            "Initialize and run Observer Kit guardrails for risky batch scripts."
        ),
        epilog="""example: observer-kit axi help""",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print package version + git sha and exit",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    axi = sub.add_parser(
        "axi",
        help="agent eXperience interface (TOON stdout, next-step help)",
        epilog="AXI is for agents: dense TOON on stdout, no interactive prompts.",
    )
    axi.add_argument("--state-dir", default=".observer", help="ledger/state directory")
    axi.add_argument("--port", type=int, default=8484, help="dashboard port for home view")
    axi_sub = axi.add_subparsers(dest="axi_command")

    def _axi_cmd(name: str, help_text: str, *, state: bool = False, run_id: bool = False,
                 limit: bool = False, since: bool = False) -> argparse.ArgumentParser:
        p = axi_sub.add_parser(name, help=help_text)
        if state:
            p.add_argument("--state-dir", default=".observer")
        if run_id:
            p.add_argument("--id", dest="id", required=True, help="run id, e.g. runguard:lane")
        if since:
            p.add_argument("--since", default=None, help="only notes after this ts")
        if limit:
            p.add_argument("--limit", type=int, default=20 if name == "attention" else 50)
        p.set_defaults(axi_command=name)
        return p

    _axi_cmd("home", "home view (default when no subcommand)")
    _axi_cmd("runs", "list runs in a state dir", state=True)
    _axi_cmd("run", "detail one run", state=True, run_id=True)
    _axi_cmd("attention", "rows with non-empty error", state=True, run_id=True, limit=True)
    _axi_cmd("sample-status", "dry-run / approval readiness", state=True, run_id=True)
    _axi_cmd("controls", "pending pause/stop/approve + ack", state=True, run_id=True)
    _axi_cmd("chat", "structured notes without long-poll", state=True, run_id=True,
             since=True, limit=True)
    axi_doc = _axi_cmd("doctor", "TOON doctor for a project")
    axi_doc.add_argument("project", nargs="?", default=".")
    axi_doc.add_argument("--state-dir", dest="state_dir_name", default=".observer")
    axi_ps = _axi_cmd("ps", "TOON process inventory", state=True)
    axi_ps.add_argument("--no-scan", action="store_true")
    _axi_cmd("help", "concise AXI command list")
    axi.set_defaults(func=cmd_axi, axi_command="home")

    init = sub.add_parser(
        "init",
        help="create .observer state home (package provides runtime)",
        epilog="""example: observer-kit init .""",
    )
    init.add_argument("project", nargs="?", default=".", help="target project directory")
    init.add_argument("--state-dir", default=".observer", help="state directory inside the project")
    init.add_argument("--force", action="store_true", help="overwrite existing files")
    init.add_argument("--no-explain", dest="explain", action="store_false",
                      help="do not copy EXPLAIN.md into the state dir")
    init.add_argument("--no-gitignore", dest="gitignore", action="store_false",
                      help="do not write a state-dir .gitignore")
    init.set_defaults(func=cmd_init, explain=True, gitignore=True)

    lint = sub.add_parser(
        "lint",
        help="static check for row liveness and durable emit patterns",
    )
    lint.add_argument("script", help="workflow script path")
    lint.set_defaults(func=cmd_lint)

    validate_flow = sub.add_parser(
        "validate-flow",
        help="validate an Observer Flow JSON manifest (structure + plan hash)",
        epilog="""examples:
  observer-kit validate-flow pipeline.flow.json
  observer-kit validate-flow pipeline.flow.json --json
""",
    )
    validate_flow.add_argument("manifest", help="path to pipeline.flow.json")
    validate_flow.add_argument(
        "--json", action="store_true", help="machine-readable output",
    )
    validate_flow.set_defaults(func=cmd_validate_flow)

    gate = sub.add_parser(
        "gate",
        help="side-effect compliance gate (force Observer Kit when needed)",
        epilog="""example: observer-kit gate path/to/script.py""",
    )
    gate.add_argument("path", nargs="?", help="script path to assess")
    gate.add_argument("--command", help="shell command to assess")
    gate.add_argument("--json", action="store_true", help="JSON assessment")
    gate.set_defaults(func=cmd_gate)

    dash = sub.add_parser(
        "dashboard",
        help="run the localhost dashboard",
        epilog="""example: observer-kit dashboard .observer""",
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
        epilog="""example: observer-kit ps""",
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
        epilog="""example: observer-kit stop --orphans""",
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
        epilog="""example: observer-kit watch .observer --all --follow""",
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
        epilog="""example:""",
    )
    reply.add_argument("state_dir", nargs="?", default=".observer", help="ledger/state directory")
    reply.add_argument("--run", required=True, help="run id to reply to")
    reply.add_argument("--anchor", default="run", help="dashboard anchor/cell id")
    reply.add_argument("--resolved", action="store_true", help="mark the note resolved")
    reply.add_argument("--text", required=True, help="reply text")
    reply.set_defaults(func=cmd_reply)

    poll = sub.add_parser(
        "poll",
        help="long-poll for dashboard notes (AXI-style agent respond loop)",
        epilog="""example: observer-kit poll .observer --run runguard:scroll-demo""",
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
        epilog="""example: observer-kit run --state-dir .observer -- python3 workflow.py --dry-run --limit 10""",
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
    run.add_argument(
        "--no-lint",
        action="store_true",
        help="skip static lint gate (default: lint workflow.py before run)",
    )
    run.add_argument(
        "--secrets",
        metavar="PATH",
        help=(
            "env file of KEY=op:// pointers only; wrap the child with "
            "`op run --env-file` so credentials exist only inside the harnessed "
            "process (opt-in possession boundary — not a sandbox). Full runs "
            "without --dry-run require pending approve_full_run before op runs"
        ),
    )
    run.add_argument(
        "--run-id",
        metavar="ID",
        help=(
            "optional runguard:LANE for secrets full-run approval preflight "
            "(defaults to any pending approve_full_run in the state dir)"
        ),
    )
    run.add_argument("command", nargs=argparse.REMAINDER,
                     help="command to run; put -- before the command")
    run.set_defaults(func=cmd_run)

    scaffold = sub.add_parser(
        "scaffold",
        help="emit a minimal workflow.py + EXPLAIN seed",
        epilog="""examples:
  observer-kit scaffold workflow --dest workflow.py --source sheet:leads --key id
  observer-kit scaffold workflow --dest enrich.py --paid-provider --external-destination
""",
    )
    scaffold.add_argument(
        "kind",
        nargs="?",
        default="workflow",
        choices=["workflow"],
        help="what to scaffold (only workflow today)",
    )
    scaffold.add_argument("--dest", required=True, help="output path for workflow.py")
    scaffold.add_argument("--source", default="source-id", help="source identity string")
    scaffold.add_argument("--key", default="id", help="record key field name")
    scaffold.add_argument("--name", default="workflow", help="start_observed_run name")
    scaffold.add_argument(
        "--paid-provider",
        action="store_true",
        help="comment in throttle() for shared provider pacing",
    )
    scaffold.add_argument(
        "--external-destination",
        action="store_true",
        help="comment durable sink write before receipt",
    )
    scaffold.add_argument("--force", action="store_true", help="overwrite existing files")
    scaffold.set_defaults(func=cmd_scaffold)

    test = sub.add_parser(
        "test",
        help="run runguard acceptance tests",
        epilog="""example:
  observer-kit test
""",
    )
    test.set_defaults(func=cmd_test)

    doctor = sub.add_parser(
        "doctor",
        help="check a project for Observer Kit basics",
        epilog="""example: observer-kit doctor .""",
    )
    doctor.add_argument("project", nargs="?", default=".", help="project directory")
    doctor.add_argument("--state-dir", default=".observer", help="state directory inside the project")
    doctor.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        from observer_kit import version_info

        info = version_info()
        print(
            f"observer-kit {info['version']} "
            f"(sha={info['git_sha']} path={info['package_path']})"
        )
        return 0
    # Note: subparser dest is "command", but `gate --command` and `run`'s
    # remainder also use that name — rely on func, not dest, for dispatch.
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
