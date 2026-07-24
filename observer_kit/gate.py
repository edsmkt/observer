"""Observer Kit compliance gate for agent hooks.

Triggers on side-effect scripts (skill real trigger B) that are not already
under Observer Kit, when an agent writes, loads, or shells them.

This is a **compliance nudge**, not a security boundary. Detection is
regex-based: it can false-positive on innocent helpers (e.g. dataclass
``.create(``, ``dict.update(``) and can be bypassed by renamed wrappers or
non-Python side effects. Prefer ``start_observed_run`` + ``observer-kit run``;
use ``# observer: ignore`` only for intentional opt-outs. See README
"Side-effect compliance gate".

Exit codes (CLI):
  0 — allow (not side-effect, or already under Observer)
  1 — deny (side-effect without Observer harness / run wrapper)
  2 — usage / parse error

Claude Code PreToolUse: read event JSON on stdin; print permissionDecision JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


# --- Side-effect detection (skill trigger B) ---------------------------------

WRITE_HTTP = re.compile(
    r"""(?ix)
    \b(?:requests|httpx|aiohttp)\.(?:post|put|patch|delete)\b
    | \.request\s*\(\s*['\"](?:POST|PUT|PATCH|DELETE)\b
    | \bmethod\s*=\s*['\"](?:POST|PUT|PATCH|DELETE)['\"]
    """
)

WRITE_SDK = re.compile(
    r"""(?ix)
    \.(?:create|update|upsert|delete|destroy|insert|bulk_create|bulk_update|
        append_row|append_rows|update_row|write_row|send_message|notify)\s*\(
    | \b(?:update_crm|write_sheet|push_to_|send_email|send_sms|post_webhook)\b
    """
)

SQL_WRITE = re.compile(
    r"""(?ix)
    ['\"]\s*(?:INSERT|UPDATE|DELETE|UPSERT|MERGE)\b
    | \bexecute(?:many)?\s*\(\s*['\"][^'\"]*(?:INSERT|UPDATE|DELETE|UPSERT|MERGE)\b
    | \.execute(?:many)?\s*\(\s*f?['\"][^'\"]*(?:INSERT|UPDATE|DELETE|UPSERT|MERGE)\b
    """
)

ORM_WRITE = re.compile(
    r"""(?ix)
    \.(?:save|create|update|delete|bulk_create|bulk_update|add_all)\s*\(
    | \bsession\.(?:add|delete|commit|merge)\s*\(
    """
)

SINK_APPEND = re.compile(
    r"""(?ix)
    open\s*\([^)]*['\"]a['\"]
    | open\s*\([^)]*mode\s*=\s*['\"]a
    | \.to_csv\s*\(
    | \.to_excel\s*\(
    | workbooks?\.open|gspread|openpyxl|xlsxwriter
    | append_row|values\.append|spreadsheets\(\)\.values\(\)\.append
    """
)

WEBHOOK_MSG = re.compile(
    r"""(?ix)
    \b(?:webhook|slack|twilio|sendgrid|mailgun|resend|ses\.|smtp)\b
    | \.(?:chat_postMessage|files_upload)\s*\(
    | \bsend_(?:email|mail|message|sms|notification)\b
    | \bnotify_\w+\s*\(
    """
)

METERED = re.compile(
    r"""(?ix)
    \b(?:credits?|quota|token_budget|rate_limit|api_credits|openai|anthropic|
        completion|embeddings?|metered)\b
    .* \b(?:for|while)\b
    | \b(?:for|while)\b .* \b(?:credits?|quota|provider_calls?|api_call)\b
    | throttle\s*\(
    """
)

OBSERVER_HARNESS = re.compile(
    r"""(?ix)
    from\s+observer_kit(?:\.runguard)?\s+import
    | import\s+observer_kit(?:\.runguard)?
    | \bstart_observed_run\s*\(
    | from\s+runguard\s+import
    | import\s+runguard\b
    """
)

OBSERVER_RUN_CMD = re.compile(
    r"""(?ix)
    \bobserver-kit\s+run\b
    | \bpython(?:3)?\s+-m\s+observer_kit\s+run\b
    """
)

# Shell: extract a .py path being executed
SHELL_PY = re.compile(
    r"""(?ix)
    (?:^|[\s;&|])(?:python3?|py)\s+(?:-B\s+)?(?P<path>[^\s;&|'"]+\.py)\b
    | (?:^|[\s;&|])(?P<path2>\./[^\s;&|'"]+\.py)\b
    """
)

EXCLUDE_PATH = re.compile(
    r"""(?ix)
    (?:^|/)test_[^/]+\.py$
    | (?:^|/).+_test\.py$
    | (?:^|/)tests/
    | (?:^|/)conftest\.py$
    | (?:^|/)setup\.py$
    | observer_kit/
    | (?:^|/)__init__\.py$
    """
)

IGNORE_MARK = re.compile(r"(?im)^\s*#\s*observer:\s*ignore\b")


def _side_effect_hits(text: str) -> list[str]:
    hits: list[str] = []
    checks = (
        ("write_http", WRITE_HTTP),
        ("write_sdk", WRITE_SDK),
        ("sql_write", SQL_WRITE),
        ("orm_write", ORM_WRITE),
        ("sink_append", SINK_APPEND),
        ("webhook_msg", WEBHOOK_MSG),
        ("metered", METERED),
    )
    for name, pat in checks:
        if pat.search(text):
            hits.append(name)
    return hits


def has_observer_harness(text: str) -> bool:
    return bool(OBSERVER_HARNESS.search(text))


def shell_uses_observer_run(command: str) -> bool:
    return bool(OBSERVER_RUN_CMD.search(command or ""))


def extract_script_from_shell(command: str) -> str | None:
    if not command:
        return None
    m = SHELL_PY.search(command)
    if not m:
        return None
    return m.group("path") or m.group("path2")


def path_excluded(path: str) -> bool:
    return bool(EXCLUDE_PATH.search(path.replace("\\", "/")))


def assess_file(path: str, text: str | None = None) -> dict:
    """Assess a script path. Returns decision payload."""
    p = Path(path).expanduser()
    try:
        resolved = str(p.resolve())
    except OSError:
        resolved = path

    if path_excluded(resolved) or path_excluded(path):
        return {
            "action": "allow",
            "reason": "excluded_path",
            "path": resolved,
            "side_effects": [],
            "has_harness": False,
        }

    if text is None:
        if not p.is_file():
            return {
                "action": "allow",
                "reason": "file_missing",
                "path": resolved,
                "side_effects": [],
                "has_harness": False,
            }
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {
                "action": "allow",
                "reason": f"unreadable:{exc}",
                "path": resolved,
                "side_effects": [],
                "has_harness": False,
            }

    if IGNORE_MARK.search(text):
        return {
            "action": "allow",
            "reason": "observer_ignore_mark",
            "path": resolved,
            "side_effects": [],
            "has_harness": has_observer_harness(text),
        }

    # Only gate Python-like scripts for content B
    if not path.endswith(".py") and not resolved.endswith(".py"):
        return {
            "action": "allow",
            "reason": "not_python",
            "path": resolved,
            "side_effects": [],
            "has_harness": False,
        }

    hits = _side_effect_hits(text)
    harness = has_observer_harness(text)
    if not hits:
        return {
            "action": "allow",
            "reason": "no_side_effects",
            "path": resolved,
            "side_effects": [],
            "has_harness": harness,
        }
    if harness:
        return {
            "action": "allow",
            "reason": "has_observer_harness",
            "path": resolved,
            "side_effects": hits,
            "has_harness": True,
        }
    return {
        "action": "deny",
        "reason": "side_effects_without_observer",
        "path": resolved,
        "side_effects": hits,
        "has_harness": False,
        "remedy": (
            "This script performs external side effects without Observer Kit. "
            "Wire: from observer_kit.runguard import start_observed_run. "
            "Run sample via: observer-kit run --state-dir .observer -- "
            f"python3 {Path(resolved).name} --dry-run --limit 10. "
            "Orient with: observer-kit axi --state-dir .observer. "
            "Check: observer-kit lint " + resolved
        ),
    }


def assess_shell(command: str, cwd: str | None = None) -> dict:
    """Assess a shell command that may execute a side-effect script."""
    if shell_uses_observer_run(command):
        return {
            "action": "allow",
            "reason": "observer_kit_run_wrapper",
            "command": command,
            "path": None,
            "side_effects": [],
            "has_harness": True,
        }

    script = extract_script_from_shell(command)
    if not script:
        return {
            "action": "allow",
            "reason": "no_python_script_in_command",
            "command": command,
            "path": None,
            "side_effects": [],
            "has_harness": False,
        }

    path = Path(script)
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    result = assess_file(str(path))
    result["command"] = command
    if result["action"] == "deny":
        result["remedy"] = (
            "Side-effect script is not launched under Observer Kit. "
            f"Use: observer-kit run --state-dir .observer -- {command.strip()}. "
            "Ensure the script calls start_observed_run. "
            f"Orient: observer-kit axi --state-dir .observer. "
            f"Lint: observer-kit lint {result.get('path')}"
        )
    return result


def assess_hook_event(event: dict) -> dict:
    """Claude Code hook event → gate assessment."""
    tool = event.get("tool_name") or event.get("toolName") or ""
    tin = event.get("tool_input") or event.get("toolInput") or {}
    cwd = event.get("cwd") or os.getcwd()

    if tool in {"Write", "Edit", "MultiEdit"}:
        path = tin.get("file_path") or tin.get("path") or ""
        # Prefer content from write if present (not yet on disk for Write)
        content = tin.get("content") or tin.get("new_string")
        if tool == "Edit" and not content:
            # Edit may only send old/new fragments; read file if exists
            return assess_file(path)
        if content is not None and path:
            # For Write, assess intended content; for Edit with new_string alone,
            # still read full file if available and merge is hard — use content
            # only when Write, else full file.
            if tool == "Write":
                return assess_file(path, text=str(content))
            return assess_file(path)
        if path:
            return assess_file(path)
        return {"action": "allow", "reason": "no_path"}

    if tool == "Read":
        path = tin.get("file_path") or tin.get("path") or ""
        if path:
            return assess_file(path)
        return {"action": "allow", "reason": "no_path"}

    if tool == "Bash":
        command = tin.get("command") or ""
        return assess_shell(command, cwd=cwd)

    return {"action": "allow", "reason": f"unhandled_tool:{tool}"}


def claude_pretool_decision(event: dict) -> dict:
    """Return Claude Code PreToolUse decision object (or empty allow)."""
    result = assess_hook_event(event)
    if result.get("action") != "deny":
        return {}
    reason = result.get("remedy") or result.get("reason") or "Observer Kit required"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Observer Kit side-effect compliance gate (hook-friendly).",
    )
    parser.add_argument(
        "--hook",
        action="store_true",
        help="read Claude Code hook JSON from stdin; emit PreToolUse decision",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print assessment as JSON",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="script path to assess (CLI mode)",
    )
    parser.add_argument(
        "--command",
        help="shell command to assess (CLI mode)",
    )
    args = parser.parse_args(argv)

    if args.hook:
        try:
            event = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"gate: invalid hook JSON: {exc}", file=sys.stderr)
            return 2
        decision = claude_pretool_decision(event if isinstance(event, dict) else {})
        if decision:
            json.dump(decision, sys.stdout)
            sys.stdout.write("\n")
            # Exit 0 with deny JSON is the Claude Code pattern (see docs).
            return 0
        return 0

    if args.command:
        result = assess_shell(args.command)
    elif args.path:
        result = assess_file(args.path)
    else:
        parser.error("provide path, --command, or --hook")
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        action = result.get("action")
        print(f"{action}: {result.get('reason')}")
        if result.get("path"):
            print(f"path: {result['path']}")
        if result.get("side_effects"):
            print(f"side_effects: {', '.join(result['side_effects'])}")
        if result.get("remedy"):
            print(f"remedy: {result['remedy']}")

    return 1 if result.get("action") == "deny" else 0


if __name__ == "__main__":
    raise SystemExit(main())
