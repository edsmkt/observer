#!/usr/bin/env python3
"""Tests for observer-kit run --secrets (op:// pointer possession boundary)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

passed = failed = 0
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
ENV = os.environ.copy()
ENV["PYTHONPATH"] = os.pathsep.join([str(REPO), ENV.get("PYTHONPATH", "")])
ENV["OBSERVER_NO_LINT"] = "1"
ENV["OBSERVER_ALLOW_UNAPPROVED_FULL_RUN"] = "1"

from observer_kit.cli import (  # noqa: E402
    command_implies_dry_run,
    ensure_secrets_approval,
    load_secret_pointers,
    record_secrets_injection,
    wrap_command_with_secrets,
)


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


def expect_exit(fn, *args, **kwargs) -> tuple[int, str]:
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            fn(*args, **kwargs)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        msg = err.getvalue()
        if not isinstance(exc.code, int) and exc.code:
            msg = (msg + " " + str(exc.code)).strip()
        return code, msg
    return 0, err.getvalue()


print("Testing observer-kit run --secrets\n")

with tempfile.TemporaryDirectory(prefix="observer-secrets-") as tmp:
    root = Path(tmp)
    good = root / "secrets.env"
    good.write_text(
        "# pointers only\n"
        "HUBSPOT_TOKEN=op://vault/hubspot/credential\n"
        "CLAY_API_KEY=op://vault/clay/api-key\n"
        "HUBSPOT_TOKEN=op://vault/hubspot/credential\n",  # duplicate key
        encoding="utf-8",
    )
    keys = load_secret_pointers(good)
    ok(
        "load_secret_pointers accepts op:// values and dedupes keys",
        keys == ["HUBSPOT_TOKEN", "CLAY_API_KEY"],
        str(keys),
    )

    quoted = root / "quoted.env"
    quoted.write_text(
        'FOO="op://v/i/f"\nBAR=\'op://v/i/b\'\n',
        encoding="utf-8",
    )
    ok(
        "load_secret_pointers allows quoted op:// pointers",
        load_secret_pointers(quoted) == ["FOO", "BAR"],
    )

    plain = root / "plain.env"
    plain.write_text("HUBSPOT_TOKEN=sk-live-not-a-pointer\n", encoding="utf-8")
    code, msg = expect_exit(load_secret_pointers, plain)
    ok(
        "load_secret_pointers refuses plain secret values with exit 2",
        code == 2 and "op://" in msg and "committable" in msg,
        f"code={code} msg={msg}",
    )

    empty = root / "empty.env"
    empty.write_text("# only comments\n\n", encoding="utf-8")
    code, msg = expect_exit(load_secret_pointers, empty)
    ok(
        "load_secret_pointers refuses empty pointer files with exit 2",
        code == 2 and "no KEY=op://" in msg,
        f"code={code} msg={msg}",
    )

    missing = root / "missing.env"
    code, msg = expect_exit(load_secret_pointers, missing)
    ok(
        "load_secret_pointers fails on missing file with exit 2",
        code == 2 and "not found" in msg,
        f"code={code} msg={msg}",
    )

    bad_line = root / "bad.env"
    bad_line.write_text("not-an-assignment\n", encoding="utf-8")
    code, msg = expect_exit(load_secret_pointers, bad_line)
    ok(
        "load_secret_pointers rejects lines without KEY= with exit 2",
        code == 2 and "expected KEY=" in msg,
        f"code={code} msg={msg}",
    )

    ok(
        "command_implies_dry_run detects --dry-run",
        command_implies_dry_run(["python3", "w.py", "--dry-run", "--limit", "10"])
        and not command_implies_dry_run(["python3", "w.py", "--full-run"]),
    )

    audit_state = root / "audit-state"
    audit_state.mkdir()
    record_secrets_injection(
        audit_state,
        ["HUBSPOT_TOKEN"],
        good,
        dry_run=True,
        run_id="runguard:demo-lane",
    )
    audit_lines = (audit_state / "secrets_audit.jsonl").read_text(encoding="utf-8").splitlines()
    lane_lines = (
        audit_state / "runs" / "demo-lane" / "events.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    audit_rec = json.loads(audit_lines[0])
    lane_rec = json.loads(lane_lines[0])
    ok(
        "record_secrets_injection writes names-only audit + lane ledger events",
        audit_rec.get("event") == "secrets_injected"
        and audit_rec.get("keys") == ["HUBSPOT_TOKEN"]
        and "sk-" not in audit_lines[0]
        and "op://" not in json.dumps(audit_rec.get("keys"))
        and lane_rec.get("run") == "runguard:demo-lane"
        and lane_rec.get("keys") == ["HUBSPOT_TOKEN"],
        audit_lines[0] + " | " + lane_lines[0],
    )

    # wrap without op on PATH
    empty_bin = root / "empty-bin"
    empty_bin.mkdir()
    saved_path = os.environ.get("PATH")
    os.environ["PATH"] = str(empty_bin)
    try:
        code, msg = expect_exit(wrap_command_with_secrets, ["python3", "w.py"], good)
        ok(
            "wrap_command_with_secrets fails closed when op missing with exit 2",
            code == 2 and "op" in msg and "PATH" in msg,
            f"code={code} msg={msg}",
        )
    finally:
        if saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved_path

    # Fake op: inject a value for HUBSPOT_TOKEN, then exec the rest after --
    fake_bin = root / "fake-bin"
    fake_bin.mkdir()
    fake_op = fake_bin / "op"
    fake_op.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [[ "${1:-}" != "run" ]]; then echo "fake-op: expected run" >&2; exit 2; fi\n'
        "shift\n"
        'env_file=""\n'
        'while [[ $# -gt 0 ]]; do\n'
        '  case "$1" in\n'
        '    --env-file) env_file="$2"; shift 2 ;;\n'
        '    --) shift; break ;;\n'
        '    *) shift ;;\n'
        "  esac\n"
        "done\n"
        'if [[ -z "$env_file" || ! -f "$env_file" ]]; then\n'
        '  echo "fake-op: missing --env-file" >&2; exit 2\n'
        "fi\n"
        "# Prove we received the env-file; inject a test value for the first key.\n"
        "export HUBSPOT_TOKEN=test-token-from-op\n"
        "export CLAY_API_KEY=test-clay-from-op\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    fake_op.chmod(fake_op.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    os.environ["PATH"] = str(fake_bin) + os.pathsep + (saved_path or os.defpath)
    try:
        wrapped, wrap_keys = wrap_command_with_secrets(["echo", "hi"], good)
        ok(
            "wrap_command_with_secrets prefixes op run --env-file --",
            wrap_keys == ["HUBSPOT_TOKEN", "CLAY_API_KEY"]
            and wrapped[0].endswith("/op")
            and wrapped[1:5] == ["run", "--env-file", str(good.resolve()), "--"]
            and wrapped[5:] == ["echo", "hi"],
            str(wrapped),
        )

        # End-to-end: observer-kit run --secrets with fake op
        project = root / "project"
        project.mkdir()
        state = project / ".observer"
        state.mkdir()
        (state / "runs").mkdir()
        secrets = state / "secrets.env"
        secrets.write_text(
            "HUBSPOT_TOKEN=op://vault/hubspot/credential\n"
            "CLAY_API_KEY=op://vault/clay/api-key\n",
            encoding="utf-8",
        )
        child = (
            "import os; "
            "print('TOKEN=' + (os.environ.get('HUBSPOT_TOKEN') or '')); "
            "print('CLAY=' + (os.environ.get('CLAY_API_KEY') or '')); "
            "print('MARKER=' + (os.environ.get('OBSERVER_SECRETS') or ''))"
        )
        run_env = ENV.copy()
        run_env["PATH"] = str(fake_bin) + os.pathsep + run_env.get("PATH", "")
        # Strip any real tokens from the parent so we only see op injection.
        run_env.pop("HUBSPOT_TOKEN", None)
        run_env.pop("CLAY_API_KEY", None)

        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "observer_kit",
                "run",
                "--state-dir",
                str(state),
                "--watch",
                "none",
                "--exit-after-run",
                "--secrets",
                str(secrets),
                "--",
                sys.executable,
                "-B",
                "-c",
                child,
            ],
            cwd=project,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = proc.stdout + proc.stderr
        ok(
            "run --secrets wraps child and injects names-only OBSERVER_SECRETS",
            proc.returncode == 0
            and "secrets: injecting HUBSPOT_TOKEN, CLAY_API_KEY" in out
            and "TOKEN=test-token-from-op" in out
            and "CLAY=test-clay-from-op" in out
            and "MARKER=HUBSPOT_TOKEN,CLAY_API_KEY" in out,
            out[-800:],
        )

        # Without --secrets, child does not get fake tokens (and no wrap)
        proc2 = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "observer_kit",
                "run",
                "--state-dir",
                str(state),
                "--watch",
                "none",
                "--exit-after-run",
                "--",
                sys.executable,
                "-B",
                "-c",
                child,
            ],
            cwd=project,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out2 = proc2.stdout + proc2.stderr
        ok(
            "run without --secrets does not inject secrets",
            proc2.returncode == 0
            and "secrets: injecting" not in out2
            and "TOKEN=" in out2
            and "TOKEN=test-token-from-op" not in out2
            and "MARKER=" in out2
            and "MARKER=HUBSPOT_TOKEN" not in out2,
            out2[-800:],
        )

        # Plain secrets file via CLI exits non-zero
        bad_secrets = state / "bad-secrets.env"
        bad_secrets.write_text("HUBSPOT_TOKEN=literal-secret\n", encoding="utf-8")
        proc3 = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "observer_kit",
                "run",
                "--state-dir",
                str(state),
                "--watch",
                "none",
                "--exit-after-run",
                "--secrets",
                str(bad_secrets),
                "--",
                sys.executable,
                "-B",
                "-c",
                "print('should-not-run')",
            ],
            cwd=project,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out3 = proc3.stdout + proc3.stderr
        ok(
            "run --secrets with plain values exits 2 before launching child",
            proc3.returncode == 2
            and "op://" in out3
            and "should-not-run" not in out3,
            f"rc={proc3.returncode} {out3[-500:]}",
        )

        audit = state / "secrets_audit.jsonl"
        ok(
            "run --secrets writes durable secrets_audit.jsonl (names only)",
            audit.is_file()
            and "secrets_injected" in audit.read_text(encoding="utf-8")
            and "HUBSPOT_TOKEN" in audit.read_text(encoding="utf-8")
            and "test-token-from-op" not in audit.read_text(encoding="utf-8"),
            audit.read_text(encoding="utf-8") if audit.is_file() else "missing",
        )

        # Full-run without approval must not materialize credentials (exit 4).
        strict_env = run_env.copy()
        strict_env.pop("OBSERVER_ALLOW_UNAPPROVED_FULL_RUN", None)
        strict_env["OBSERVER_REQUIRE_FULL_RUN_APPROVAL"] = "1"
        proc4 = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "observer_kit",
                "run",
                "--state-dir",
                str(state),
                "--watch",
                "none",
                "--exit-after-run",
                "--secrets",
                str(secrets),
                "--",
                sys.executable,
                "-B",
                "-c",
                "import os; print('LEAK=' + (os.environ.get('HUBSPOT_TOKEN') or ''))",
            ],
            cwd=project,
            env=strict_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out4 = proc4.stdout + proc4.stderr
        ok(
            "full-run --secrets without approval exits 4 before op injects credentials",
            proc4.returncode == 4
            and "approve_full_run" in out4
            and "LEAK=" not in out4
            and "test-token-from-op" not in out4,
            f"rc={proc4.returncode} {out4[-600:]}",
        )

        # Dry-run still gets secrets under strict approval policy.
        proc5 = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "observer_kit",
                "run",
                "--state-dir",
                str(state),
                "--watch",
                "none",
                "--exit-after-run",
                "--secrets",
                str(secrets),
                "--",
                sys.executable,
                "-B",
                "-c",
                "import os; print('TOKEN=' + (os.environ.get('HUBSPOT_TOKEN') or ''))",
                "--dry-run",
            ],
            cwd=project,
            env=strict_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out5 = proc5.stdout + proc5.stderr
        ok(
            "dry-run --secrets still injects credentials under approval policy",
            proc5.returncode == 0 and "TOKEN=test-token-from-op" in out5,
            f"rc={proc5.returncode} {out5[-600:]}",
        )

        # Unit: ensure_secrets_approval with a posted control.
        from observer_kit.runguard import post_control

        os.environ["RUNGUARD_STATE_DIR"] = str(state)
        try:
            post_control("runguard:approved-lane", "approve_full_run", note="ok")
        finally:
            pass
        try:
            ensure_secrets_approval(
                state,
                ["python3", "w.py", "--full-run"],
                run_id="runguard:approved-lane",
            )
            approved_ok = True
            approved_msg = ""
        except SystemExit as exc:
            approved_ok = False
            approved_msg = str(exc)
        ok(
            "ensure_secrets_approval allows full-run when approve_full_run is pending",
            approved_ok,
            approved_msg,
        )
    finally:
        if saved_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved_path


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
