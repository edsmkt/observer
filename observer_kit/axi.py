"""Observer Kit AXI — agent-ergonomic CLI surface (TOON stdout).

Principles (subset of https://github.com/kunchenguid/axi):
token-efficient TOON, minimal fields, definitive empty states, structured exit
codes, next-step help[]. Human dashboard remains the visual review surface.

Exit codes (document + test):
  0 ok
  1 not found / failed check
  2 usage
  3 lock conflict
  4 approval missing
  130 interrupt
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.error import URLError
from urllib.request import urlopen

from observer_kit._util import pid_alive as _pid_alive
from observer_kit.inventory import dashboard_records, watcher_records

# Stable exit codes for agents scripting AXI.
EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2
EXIT_LOCK = 3
EXIT_APPROVAL = 4
EXIT_INTERRUPT = 130

# Terminal status enum only — never invent "done" / "complete" aliases.
TERMINAL_STATUSES = frozenset({
    'running', 'paused', 'success', 'failed', 'abandoned', 'unknown',
})

AXI_SCHEMA = 1


# --- TOON emission (stdlib, no dependency on toonformat package) ---------------

def _toon_scalar(value: Any) -> str:
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == '' or any(ch in text for ch in ':\n,"[]{}') or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


def toon_kv(key: str, value: Any) -> str:
    return f'{key}: {_toon_scalar(value)}'


def toon_table(name: str, rows: list[dict], columns: list[str]) -> str:
    """Emit TOON-style table: name[N]{cols}: then space-indented CSV-ish rows."""
    if not rows:
        return f'{name}[0]{{{",".join(columns)}}}:'
    lines = [f'{name}[{len(rows)}]{{{",".join(columns)}}}:']
    for row in rows:
        cells = []
        for col in columns:
            cells.append(_toon_scalar(row.get(col)))
        lines.append('  ' + ','.join(cells))
    return '\n'.join(lines)


def toon_help(items: Iterable[str]) -> str:
    items = [str(i) for i in items if i]
    if not items:
        return 'help[0]:'
    # Quoted list like no-mistakes
    body = ','.join(json.dumps(i, ensure_ascii=False) for i in items)
    return f'help[{len(items)}]: {body}'


def emit(*blocks: str) -> None:
    """Write TOON blocks to stdout (agents parse stdout; progress stays stderr)."""
    text = '\n'.join(b for b in blocks if b)
    if text:
        sys.stdout.write(text + '\n')
        sys.stdout.flush()


def emit_error(code: str, *, run: str | None = None, state_dir: str | None = None,
               help_items: Iterable[str] | None = None, **extra: Any) -> None:
    """Structured error on stdout (tracebacks belong on stderr only)."""
    blocks = [toon_kv('error', code)]
    if run:
        blocks.append(toon_kv('run', run))
    if state_dir:
        blocks.append(toon_kv('state_dir', state_dir))
    for key, value in extra.items():
        blocks.append(toon_kv(key, value))
    if help_items is not None:
        blocks.append(toon_help(help_items))
    emit(*blocks)


# --- Discovery helpers --------------------------------------------------------

ACTIVE_S = 120

# Real AXI verbs that ship with this package (help[] must only reference these).
AXI_COMMANDS = (
    'home',
    'runs',
    'run',
    'attention',
    'sample-status',
    'controls',
    'chat',
    'doctor',
    'ps',
    'help',
)


def _read_jsonl_tail(path: Path, max_lines: int = 80) -> list[dict]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-max_lines:]:
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


def _read_jsonl_all(path: Path, max_lines: int = 50_000) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    try:
        with path.open(encoding='utf-8', errors='replace') as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return []
    return out


def _first_run_started(path: Path) -> dict:
    for rec in _read_jsonl_tail(path, max_lines=5000):
        if (rec.get('event') or rec.get('action')) == 'run_started':
            return rec
    # fall back to first line
    rows = _read_jsonl_tail(path, max_lines=5000)
    return rows[0] if rows else {}


def _last_event(path: Path) -> dict:
    rows = _read_jsonl_tail(path, max_lines=40)
    return rows[-1] if rows else {}


def _events_path_for_lane(state_dir: Path, lane: str) -> Path | None:
    preferred = state_dir / 'runs' / lane / 'events.jsonl'
    if preferred.is_file():
        return preferred
    legacy = state_dir / f'{lane}.jsonl'
    if legacy.is_file():
        return legacy
    return None


def _lock_path_for_events(events_path: Path, state_dir: Path) -> Path | None:
    # Preferred: state/runs/<lane>/events.jsonl → state/<lane>.lock
    try:
        if events_path.name == 'events.jsonl' and events_path.parent.parent.name == 'runs':
            lane = events_path.parent.name
            return state_dir / f'{lane}.lock'
        if events_path.suffix == '.jsonl':
            return state_dir / f'{events_path.stem}.lock'
    except OSError:
        return None
    return None


def _is_live(events_path: Path, state_dir: Path, now: float) -> bool:
    last = _last_event(events_path)
    event = last.get('event') or last.get('action')
    if event in {'run_finished', 'run_failed', 'run_abandoned', 'run_paused'}:
        return False
    lock_path = _lock_path_for_events(events_path, state_dir)
    if lock_path and lock_path.is_file():
        try:
            lock = json.loads(lock_path.read_text(encoding='utf-8'))
            pid = int(lock.get('pid') or 0)
            if pid <= 0:
                return False
            return _pid_alive(pid)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    if last.get('pid') is not None and not _pid_alive(last.get('pid')):
        return False
    try:
        mtime = events_path.stat().st_mtime
    except OSError:
        return False
    return (now - mtime) < ACTIVE_S


def _normalize_status(raw: str, *, live: bool = False, reason: str | None = None) -> tuple[str, str | None]:
    """Map ledger events to the terminal status enum."""
    if raw in TERMINAL_STATUSES:
        return raw, reason
    if raw in {'started', 'running', 'sample_started', 'full_run_started'}:
        return ('running' if live or raw == 'running' else 'running'), reason
    if raw == 'run_finished' or raw == 'success' or raw == 'sample_finished':
        return 'success', reason
    if raw in {'run_failed', 'failed'}:
        return 'failed', reason
    if raw in {'run_abandoned', 'abandoned'}:
        return 'abandoned', reason
    if raw in {'run_paused', 'paused'}:
        return 'paused', reason
    if not raw:
        return 'unknown', reason or 'empty_ledger'
    return 'unknown', reason or f'unrecognized_event:{raw}'


def _terminal_status(events_path: Path, *, live: bool = False) -> tuple[str, str | None]:
    if not events_path.is_file():
        return 'unknown', 'missing_ledger'
    rows = _read_jsonl_tail(events_path, max_lines=200)
    if not rows:
        return 'unknown', 'empty_ledger'
    # Prefer true terminal events when present near the end.
    for rec in reversed(rows):
        event = rec.get('event') or rec.get('action') or ''
        if event == 'run_finished':
            status = str(rec.get('status') or 'success')
            if status in {'success', 'failed', 'abandoned', 'paused'}:
                return status, None
            return 'success', None
        if event == 'run_failed':
            return 'failed', None
        if event == 'run_abandoned':
            return 'abandoned', None
        if event == 'run_paused':
            return 'paused', None
    last = rows[-1]
    event = last.get('event') or last.get('action') or ''
    if live or event in {
        'run_started', 'sample_started', 'full_run_started', 'record',
        'checkpoint', 'metric', 'write_intent', 'write_receipt',
    }:
        return 'running', None
    return _normalize_status(str(event), live=live)


def _count_records(events_path: Path) -> int:
    n = 0
    if not events_path.is_file():
        return 0
    try:
        with events_path.open(encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if '"event": "record"' in line or '"event":"record"' in line:
                    n += 1
    except OSError:
        return 0
    return n


def _count_errors(events_path: Path) -> int:
    n = 0
    if not events_path.is_file():
        return 0
    try:
        with events_path.open(encoding='utf-8', errors='replace') as fh:
            for line in fh:
                if not line.strip():
                    continue
                if '"error"' not in line and '"status": "failed"' not in line and '"status":"failed"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get('error') or rec.get('status') == 'failed':
                    n += 1
    except OSError:
        return 0
    return n


def attention_rows(events_path: Path, *, limit: int = 20) -> list[dict]:
    """Rows with non-empty error (Attention tab for agents)."""
    rows: list[dict] = []
    if not events_path.is_file():
        return rows
    try:
        with events_path.open(encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line or '"error"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                err = rec.get('error')
                if err is None or err == '':
                    continue
                if rec.get('event') not in {
                    'record', 'run_failed', 'dead_letter', 'write_receipt', None,
                } and rec.get('event') not in {'record', 'run_failed', 'dead_letter'}:
                    # Keep record/dead_letter/run_failed; also accept any with error + key
                    if not rec.get('key') and rec.get('event') not in {
                        'run_failed', 'dead_letter', 'record',
                    }:
                        continue
                rows.append({
                    'key': str(rec.get('key') or rec.get('record_key') or rec.get('event') or ''),
                    'error': str(err)[:200],
                })
    except OSError:
        return []
    return rows[-limit:]


def list_runs(state_dir: Path) -> list[dict]:
    """Minimal run inventory for AXI (not a full dashboard list_runs port)."""
    state_dir = state_dir.expanduser().resolve()
    now = time.time()
    runs: list[dict] = []
    seen: set[str] = set()

    lanes = state_dir / 'runs'
    if lanes.is_dir():
        for lane_dir in sorted(lanes.iterdir()):
            if not lane_dir.is_dir():
                continue
            ev = lane_dir / 'events.jsonl'
            if not ev.is_file():
                continue
            real = str(ev.resolve())
            if real in seen:
                continue
            seen.add(real)
            started = _first_run_started(ev)
            if (started.get('event') or started.get('action')) != 'run_started':
                # still list ledgers with any events
                if not _read_jsonl_tail(ev, 1):
                    continue
            try:
                mtime = ev.stat().st_mtime
            except OSError:
                mtime = 0
            run_id = f'runguard:{lane_dir.name}'
            live = _is_live(ev, state_dir, now)
            status, status_reason = _terminal_status(ev, live=live)
            runs.append({
                'id': run_id,
                'lane': lane_dir.name,
                'live': live,
                'status': status,
                'status_reason': status_reason,
                'desc': str(
                    started.get('description')
                    or started.get('name')
                    or lane_dir.name
                )[:80],
                'records': _count_records(ev),
                'errors': _count_errors(ev),
                'mtime': int(mtime),
                'events_path': str(ev),
            })

    # Legacy flat *.jsonl at state root
    if state_dir.is_dir():
        for path in sorted(state_dir.glob('*.jsonl')):
            if path.name in {'chat.jsonl', 'controls.jsonl'}:
                continue
            real = str(path.resolve())
            if real in seen:
                continue
            seen.add(real)
            started = _first_run_started(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0
            live = _is_live(path, state_dir, now)
            status, status_reason = _terminal_status(path, live=live)
            runs.append({
                'id': f'runguard:{path.stem}',
                'lane': path.stem,
                'live': live,
                'status': status,
                'status_reason': status_reason,
                'desc': str(
                    started.get('description') or started.get('name') or path.stem
                )[:80],
                'records': _count_records(path),
                'errors': _count_errors(path),
                'mtime': int(mtime),
                'events_path': str(path),
            })

    runs.sort(key=lambda r: -int(r.get('mtime') or 0))
    return runs


def get_run(state_dir: Path, run_id: str) -> dict | None:
    for run in list_runs(state_dir):
        if run['id'] == run_id or run['lane'] == run_id:
            return run
        # allow bare lane / with runguard: prefix
        if run_id.startswith('runguard:') and run['id'] == run_id:
            return run
        bare = run_id.split(':', 1)[-1]
        if bare == run['lane'] or bare == run['id']:
            return run
    return None


def run_detail(state_dir: Path, run_id: str) -> dict | None:
    """Rich detail for axi run: last_event, attention, lock, dry_run, next."""
    run = get_run(state_dir, run_id)
    if not run:
        return None
    events_path = Path(str(run.get('events_path') or ''))
    last = _last_event(events_path) if events_path.is_file() else {}
    last_name = str(last.get('event') or last.get('action') or '')
    attention = attention_rows(events_path, limit=20) if events_path.is_file() else []
    lock_path = _lock_path_for_events(events_path, state_dir) if events_path.is_file() else None
    lock_pid = None
    lock_alive = False
    if lock_path and lock_path.is_file():
        try:
            lock = json.loads(lock_path.read_text(encoding='utf-8'))
            lock_pid = lock.get('pid')
            lock_alive = _pid_alive(lock_pid)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    # Scan for dry_run / sample / approval signals
    dry_run = None
    sample_done = False
    full_started = False
    approval_acked = False
    pending_writes = 0
    intents: set[str] = set()
    receipts: set[str] = set()
    if events_path.is_file():
        for rec in _read_jsonl_all(events_path):
            ev = rec.get('event')
            if ev == 'run_started' and 'dry_run' in rec:
                dry_run = bool(rec.get('dry_run'))
            if ev == 'sample_finished':
                sample_done = True
            if ev == 'full_run_started':
                full_started = True
            if ev == 'control_acknowledged' and rec.get('control') == 'approve_full_run':
                approval_acked = True
            if ev == 'write_intent':
                op = str(rec.get('operation_key') or rec.get('key') or '')
                if op:
                    intents.add(op)
            if ev in {'write_receipt', 'write_preview'}:
                op = str(rec.get('operation_key') or rec.get('key') or '')
                if op:
                    receipts.add(op)
    pending_writes = len(intents - receipts)
    detail = dict(run)
    detail.update({
        'last_event': last_name or 'none',
        'error_count': int(run.get('errors') or 0),
        'attention': attention,
        'lock_pid': lock_pid,
        'lock_alive': lock_alive,
        'dry_run': dry_run,
        'sample_finished': sample_done,
        'full_run_started': full_started,
        'approval_acked': approval_acked,
        'pending_writes': pending_writes,
        'unreceipted_intents': pending_writes,
    })
    return detail


def sample_status(state_dir: Path, run_id: str) -> dict | None:
    """Dry-run complete? stratified outcomes? approval recorded?"""
    run = get_run(state_dir, run_id)
    if not run:
        return None
    events_path = Path(str(run.get('events_path') or ''))
    events = _read_jsonl_all(events_path) if events_path.is_file() else []
    dry_runs = [e for e in events if e.get('event') == 'run_started' and e.get('dry_run') is True]
    sample_finished = any(e.get('event') == 'sample_finished' for e in events)
    dry_finished = any(
        e.get('event') == 'run_finished' and e.get('dry_run') is True for e in events
    )
    outcomes: dict[str, int] = {}
    for e in events:
        if e.get('event') != 'record':
            continue
        status = str(e.get('status') or 'unknown')
        outcomes[status] = outcomes.get(status, 0) + 1
    # Approval on controls channel
    controls_path = state_dir / 'runs' / run['lane'] / 'controls.jsonl'
    root_controls = state_dir / 'controls.jsonl'
    approvals = []
    for path in (controls_path, root_controls):
        if not path.is_file():
            continue
        for rec in _read_jsonl_all(path):
            if rec.get('kind') != 'approve_full_run':
                continue
            if rec.get('run') and rec.get('run') not in {run['id'], run['lane'], None, ''}:
                continue
            approvals.append(rec)
    acked = any(
        e.get('event') == 'control_acknowledged' and e.get('control') == 'approve_full_run'
        for e in events
    )
    return {
        'id': run['id'],
        'lane': run['lane'],
        'status': run['status'],
        'dry_run_started': bool(dry_runs),
        'sample_finished': sample_finished or dry_finished,
        'approval_recorded': bool(approvals) or acked,
        'approval_pending': bool(approvals) and not acked,
        'outcomes': outcomes,
        'records': run.get('records') or 0,
    }


def list_controls(state_dir: Path, run_id: str) -> list[dict]:
    run = get_run(state_dir, run_id)
    if not run:
        return []
    paths = [
        state_dir / 'runs' / run['lane'] / 'controls.jsonl',
        state_dir / 'controls.jsonl',
    ]
    # Ack state from ledger
    events_path = Path(str(run.get('events_path') or ''))
    acked_ids: set[str] = set()
    if events_path.is_file():
        for e in _read_jsonl_all(events_path):
            if e.get('event') == 'control_acknowledged' and e.get('control_id'):
                acked_ids.add(str(e['control_id']))
    out: list[dict] = []
    seen: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        for rec in _read_jsonl_all(path):
            if rec.get('run') and rec.get('run') not in {run['id'], run['lane']}:
                continue
            cid = str(rec.get('id') or '')
            if cid and cid in seen:
                continue
            if cid:
                seen.add(cid)
            out.append({
                'id': cid or 'unknown',
                'kind': rec.get('kind') or '',
                'ts': rec.get('ts') or '',
                'acked': cid in acked_ids if cid else False,
                'note': str(rec.get('note') or '')[:80],
            })
    return out


def list_chat(state_dir: Path, run_id: str, *, since: str | None = None,
              limit: int = 50) -> list[dict]:
    run = get_run(state_dir, run_id)
    lane = run['lane'] if run else run_id.split(':', 1)[-1]
    paths = [
        state_dir / 'runs' / lane / 'chat.jsonl',
        state_dir / 'chat.jsonl',
    ]
    rows: list[dict] = []
    for path in paths:
        if not path.is_file():
            continue
        for rec in _read_jsonl_all(path):
            if rec.get('run') and rec.get('run') not in {
                run_id, f'runguard:{lane}', lane, None, '',
            }:
                if run and rec.get('run') not in {run['id'], run['lane']}:
                    continue
            ts = str(rec.get('ts') or '')
            if since and ts and ts <= since:
                continue
            rows.append({
                'ts': ts,
                'author': rec.get('author') or '',
                'kind': rec.get('kind') or 'note',
                'anchor': rec.get('anchor') or 'run',
                'text': str(rec.get('text') or '')[:200],
            })
    return rows[-limit:]


def probe_dashboard(port: int = 8484) -> dict | None:
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/meta', timeout=0.35) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode('utf-8') or '{}')
    except (OSError, URLError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def real_help_commands(state_dir: str, *, live: int = 0, orphans: int = 0,
                       run_id: str | None = None) -> list[str]:
    """Copy-pastable commands that exist on this package surface only."""
    helps: list[str] = []
    if orphans:
        helps.append(f'observer-kit stop --sweep {state_dir}')
    if live and run_id:
        helps.append(f'observer-kit axi run --state-dir {state_dir} --id {run_id}')
        helps.append(f'observer-kit poll {state_dir} --run {run_id}')
        helps.append(
            f'observer-kit axi attention --state-dir {state_dir} --id {run_id}'
        )
    elif live:
        helps.append(f'observer-kit axi runs --state-dir {state_dir}')
        helps.append(f'observer-kit poll {state_dir} --all')
    else:
        helps.append(f'observer-kit axi runs --state-dir {state_dir}')
        helps.append(f'observer-kit dashboard {state_dir}')
        helps.append(f'observer-kit poll {state_dir} --all')
    if run_id:
        helps.append(
            f'observer-kit axi sample-status --state-dir {state_dir} --id {run_id}'
        )
    helps.append('observer-kit axi help')
    # Deduplicate preserving order
    seen: set[str] = set()
    out: list[str] = []
    for h in helps:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def default_help(state_dir: str, *, live: int = 0, orphans: int = 0) -> list[str]:
    return real_help_commands(state_dir, live=live, orphans=orphans)


def axi_help_catalog() -> list[str]:
    """Every axi subcommand that works — used by axi help and acceptance tests.

    Each item must be a concrete, copy-pastable command (no optional-bracket
    syntax). Placeholder tokens use runguard:LANE which agents substitute.
    """
    return [
        'observer-kit axi --state-dir .observer',
        'observer-kit axi runs --state-dir .observer',
        'observer-kit axi run --state-dir .observer --id runguard:LANE',
        'observer-kit axi attention --state-dir .observer --id runguard:LANE',
        'observer-kit axi sample-status --state-dir .observer --id runguard:LANE',
        'observer-kit axi controls --state-dir .observer --id runguard:LANE',
        'observer-kit axi chat --state-dir .observer --id runguard:LANE',
        'observer-kit axi doctor .',
        'observer-kit axi ps --state-dir .observer',
        'observer-kit axi help',
        'observer-kit poll .observer --all',
        'observer-kit stop --sweep .observer',
    ]




def substrate_checks(project: Path, state_name: str) -> list[dict]:
    """Shared doctor substrate for human CLI and axi doctor."""
    project = project.expanduser().resolve()
    state = project / state_name
    pkg = Path(__file__).resolve().parent
    return [
        {"check": "project_exists", "label": "project exists", "ok": project.exists()},
        {"check": "package_runguard", "label": "package runguard available",
         "ok": (pkg / "runguard.py").is_file()},
        {"check": "package_dashboard", "label": "package dashboard available",
         "ok": (pkg / "run_dashboard.py").is_file()},
        {"check": "package_dashboard_asset", "label": "package dashboard asset available",
         "ok": (pkg / "assets" / "dashboard.js").is_file()},
        {"check": "package_watcher", "label": "package watcher available",
         "ok": (pkg / "watch_chat.py").is_file()},
        {"check": "state_dir", "label": "state dir exists", "ok": state.exists()},
        {"check": "state_gitignore", "label": "state dir ignores local ledger data",
         "ok": (state / ".gitignore").exists()},
        {"check": "explain", "label": "operator explainer exists",
         "ok": (state / "EXPLAIN.md").exists()},
        {"check": "runs_home", "label": "runs/ home exists", "ok": (state / "runs").is_dir()},
    ]


# --- CLI dispatch (moved from cli.py) ----------------------------------------

def dispatch(args) -> int:
    """Agent-ergonomic surface: TOON stdout, next-step help, no interactive prompts."""
    action = getattr(args, "axi_command", None) or "home"
    state_dir = Path(getattr(args, "state_dir", ".observer") or ".observer")
    state_dir = state_dir.expanduser().resolve()

    if action == "home":
        return _axi_home(state_dir, port=getattr(args, "port", 8484))
    if action == "runs":
        return _axi_runs(state_dir)
    if action == "run":
        return _axi_run_detail(
            state_dir, getattr(args, "id", None) or getattr(args, "run_id", None)
        )
    if action == "attention":
        return _axi_attention(
            state_dir,
            getattr(args, "id", None),
            limit=int(getattr(args, "limit", 20) or 20),
        )
    if action == "sample-status":
        return _axi_sample_status(state_dir, getattr(args, "id", None))
    if action == "controls":
        return _axi_controls(state_dir, getattr(args, "id", None))
    if action == "chat":
        return _axi_chat(
            state_dir,
            getattr(args, "id", None),
            since=getattr(args, "since", None),
            limit=int(getattr(args, "limit", 50) or 50),
        )
    if action == "doctor":
        return _axi_doctor(
            Path(getattr(args, "project", ".") or "."),
            getattr(args, "state_dir_name", None) or state_dir.name,
        )
    if action == "ps":
        return _axi_ps(state_dir, scan=not getattr(args, "no_scan", False))
    if action == "help":
        return _axi_help()
    emit(
        toon_kv("error", f"unknown axi command: {action}"),
        toon_help(["observer-kit axi help"]),
    )
    return 2

def _axi_home(state_dir: Path, *, port: int = 8484) -> int:
    from observer_kit import detect_install_skew, version_info

    runs = list_runs(state_dir) if state_dir.is_dir() else []
    live_runs = [r for r in runs if r.get("live")]
    dashboards = dashboard_records(
        [state_dir] if state_dir.is_dir() else None, scan_ports=True
    )
    watchers = watcher_records(state_dir) if state_dir.is_dir() else []
    orphans = sum(1 for r in dashboards + watchers if r.get("orphan"))
    dash = probe_dashboard(port)
    if dash is None:
        for rec in dashboards:
            if rec.get("pid_alive") and rec.get("port"):
                dash = {
                    "port": rec.get("port"),
                    "state_dir": rec.get("state_dir"),
                    "pid": rec.get("pid"),
                }
                break

    ver = version_info()
    skew = detect_install_skew()
    blocks = [
        toon_kv("surface", "observer-axi"),
        toon_kv("axi_schema", AXI_SCHEMA),
        toon_kv("version", ver["version"]),
        toon_kv("git_sha", ver["git_sha"]),
        toon_kv(
            "state_dir",
            str(state_dir) if state_dir.is_dir() else f"missing:{state_dir}",
        ),
        toon_kv("state_ok", state_dir.is_dir()),
        toon_kv("runs", len(runs)),
        toon_kv("live", len(live_runs)),
        toon_kv("orphans", orphans),
        toon_kv("install_skew", bool(skew.get("install_skew"))),
        toon_kv(
            "dashboard",
            f"http://127.0.0.1:{dash.get('port')}/"
            if dash and dash.get("port")
            else "none",
        ),
    ]
    if skew.get("install_skew"):
        blocks.append(toon_kv("skew_reason", skew.get("reason") or "path/package mismatch"))
        blocks.append(toon_kv("upgrade", skew.get("upgrade") or "python3 -m pip install -e ."))

    if live_runs:
        blocks.append(
            toon_table(
                "live_runs",
                live_runs[:10],
                ["id", "status", "records", "desc"],
            )
        )
    elif runs:
        blocks.append(
            toon_table(
                "recent_runs",
                runs[:5],
                ["id", "live", "status", "records", "desc"],
            )
        )
    else:
        # Definitive empty state (not prose "No runs.")
        blocks.append(toon_table("recent_runs", [], ["id", "live", "status", "records", "desc"]))

    if orphans:
        blocks.append(toon_kv("orphan_note", f"{orphans} orphan process(es)"))

    live_id = live_runs[0]["id"] if live_runs else None
    helps = default_help(
        str(state_dir), live=len(live_runs), orphans=orphans
    )
    # real_help_commands accepts run_id via kwargs through default_help only if we pass it —
    # inject live run id for copy-pastable poll when live.
    if live_id:
        helps = real_help_commands(
            str(state_dir), live=len(live_runs), orphans=orphans, run_id=live_id
        )
    if not state_dir.is_dir():
        helps = [
            f"observer-kit init . --state-dir {state_dir.name}",
            skew.get("upgrade") or "python3 -m pip install -e .",
            "observer-kit axi doctor .",
        ]
    if skew.get("install_skew"):
        helps = [str(skew.get("upgrade") or "python3 -m pip install -e .")] + [
            h for h in helps if "pip install" not in h
        ]
        helps.append("python3 -m observer_kit axi help")
    blocks.append(toon_help(helps))
    emit(*blocks)
    return 0 if state_dir.is_dir() else 1


def _axi_runs(state_dir: Path) -> int:

    if not state_dir.is_dir():
        emit(
            toon_kv("error", "state_dir_missing"),
            toon_kv("state_dir", str(state_dir)),
            toon_help([f"observer-kit init . --state-dir {state_dir.name}"]),
        )
        return 1
    runs = list_runs(state_dir)
    emit(
        toon_kv("state_dir", str(state_dir)),
        toon_kv("count", len(runs)),
        toon_table("runs", runs, ["id", "live", "status", "records", "desc"]),
        toon_help(default_help(str(state_dir), live=sum(1 for r in runs if r.get("live")))),
    )
    return 0


def _axi_run_detail(state_dir: Path, run_id: str | None) -> int:

    if not run_id:
        emit(
            toon_kv("error", "run_id_required"),
            toon_kv("state_dir", str(state_dir)),
            toon_help([f"observer-kit axi runs --state-dir {state_dir}"]),
        )
        return 2
    if not state_dir.is_dir():
        emit(
            toon_kv("error", "state_dir_missing"),
            toon_kv("state_dir", str(state_dir)),
        )
        return 1
    detail = run_detail(state_dir, run_id)
    if not detail:
        emit(
            toon_kv("error", "run_not_found"),
            toon_kv("run", run_id),
            toon_kv("state_dir", str(state_dir)),
            toon_help([f"observer-kit axi runs --state-dir {state_dir}"]),
        )
        return 1
    attention = detail.get("attention") or []
    fields = [
        ("state_dir", str(state_dir)),
        ("id", detail["id"]),
        ("lane", detail["lane"]),
        ("live", detail["live"]),
        ("status", detail["status"]),
        ("records", detail["records"]),
        ("error_count", detail.get("error_count") or 0),
        ("last_event", detail.get("last_event") or "none"),
        ("desc", detail["desc"]),
        ("mtime", detail["mtime"]),
        ("dry_run", detail.get("dry_run")),
        ("sample_finished", bool(detail.get("sample_finished"))),
        ("full_run_started", bool(detail.get("full_run_started"))),
        ("lock_pid", detail.get("lock_pid")),
        ("lock_alive", bool(detail.get("lock_alive"))),
        ("pending_writes", detail.get("pending_writes") or 0),
        ("unreceipted_intents", detail.get("unreceipted_intents") or 0),
    ]
    if detail.get("status") == "unknown" and detail.get("status_reason"):
        fields.insert(5, ("status_reason", detail["status_reason"]))
    blocks = [toon_kv(k, v) for k, v in fields]
    blocks.append(toon_table("attention", attention, ["key", "error"]))
    blocks.append(toon_help(real_help_commands(
        str(state_dir), live=1 if detail.get("live") else 0, run_id=detail["id"],
    )))
    emit(*blocks)
    return 0


def _axi_attention(state_dir: Path, run_id: str | None, *, limit: int = 20) -> int:

    if not run_id:
        emit(
            toon_kv("error", "run_id_required"),
            toon_help([f"observer-kit axi runs --state-dir {state_dir}"]),
        )
        return 2
    if not state_dir.is_dir():
        emit(toon_kv("error", "state_dir_missing"), toon_kv("state_dir", str(state_dir)))
        return 1
    run = get_run(state_dir, run_id)
    if not run:
        emit(
            toon_kv("error", "run_not_found"),
            toon_kv("run", run_id),
            toon_help([f"observer-kit axi runs --state-dir {state_dir}"]),
        )
        return 1
    path = Path(str(run.get("events_path") or ""))
    rows = attention_rows(path, limit=limit) if path.is_file() else []
    emit(
        toon_kv("state_dir", str(state_dir)),
        toon_kv("id", run["id"]),
        toon_kv("count", len(rows)),
        toon_table("attention", rows, ["key", "error"]),
        toon_help([
            f"observer-kit axi run --state-dir {state_dir} --id {run['id']}",
            f"observer-kit poll {state_dir} --run {run['id']}",
        ]),
    )
    return 0


def _axi_sample_status(state_dir: Path, run_id: str | None) -> int:

    if not run_id:
        emit(
            toon_kv("error", "run_id_required"),
            toon_help([f"observer-kit axi runs --state-dir {state_dir}"]),
        )
        return 2
    if not state_dir.is_dir():
        emit(toon_kv("error", "state_dir_missing"), toon_kv("state_dir", str(state_dir)))
        return 1
    status = sample_status(state_dir, run_id)
    if not status:
        emit(
            toon_kv("error", "run_not_found"),
            toon_kv("run", run_id),
            toon_help([f"observer-kit axi runs --state-dir {state_dir}"]),
        )
        return 1
    outcome_rows = [
        {"status": k, "count": v} for k, v in sorted((status.get("outcomes") or {}).items())
    ]
    emit(
        toon_kv("state_dir", str(state_dir)),
        toon_kv("id", status["id"]),
        toon_kv("status", status["status"]),
        toon_kv("dry_run_started", status["dry_run_started"]),
        toon_kv("sample_finished", status["sample_finished"]),
        toon_kv("approval_recorded", status["approval_recorded"]),
        toon_kv("approval_pending", status["approval_pending"]),
        toon_kv("records", status["records"]),
        toon_table("outcomes", outcome_rows, ["status", "count"]),
        toon_help([
            f"observer-kit axi run --state-dir {state_dir} --id {status['id']}",
            f"observer-kit dashboard {state_dir}",
        ]),
    )
    return 0


def _axi_controls(state_dir: Path, run_id: str | None) -> int:

    if not run_id:
        emit(
            toon_kv("error", "run_id_required"),
            toon_help([f"observer-kit axi runs --state-dir {state_dir}"]),
        )
        return 2
    if not state_dir.is_dir():
        emit(toon_kv("error", "state_dir_missing"), toon_kv("state_dir", str(state_dir)))
        return 1
    run = get_run(state_dir, run_id)
    if not run:
        emit(
            toon_kv("error", "run_not_found"),
            toon_kv("run", run_id),
            toon_help([f"observer-kit axi runs --state-dir {state_dir}"]),
        )
        return 1
    rows = list_controls(state_dir, run_id)
    pending = [r for r in rows if not r.get("acked")]
    emit(
        toon_kv("state_dir", str(state_dir)),
        toon_kv("id", run["id"]),
        toon_kv("pending", len(pending)),
        toon_table("controls", rows, ["id", "kind", "acked", "ts", "note"]),
        toon_help([
            f"observer-kit axi run --state-dir {state_dir} --id {run['id']}",
            f"observer-kit dashboard {state_dir}",
        ]),
    )
    return 0


def _axi_chat(state_dir: Path, run_id: str | None, *, since: str | None = None,
              limit: int = 50) -> int:

    if not run_id:
        emit(
            toon_kv("error", "run_id_required"),
            toon_help([f"observer-kit axi runs --state-dir {state_dir}"]),
        )
        return 2
    if not state_dir.is_dir():
        emit(toon_kv("error", "state_dir_missing"), toon_kv("state_dir", str(state_dir)))
        return 1
    run = get_run(state_dir, run_id)
    rid = run["id"] if run else run_id
    rows = list_chat(state_dir, rid, since=since, limit=limit)
    emit(
        toon_kv("state_dir", str(state_dir)),
        toon_kv("id", rid),
        toon_kv("count", len(rows)),
        toon_table("chat", rows, ["ts", "author", "kind", "anchor", "text"]),
        toon_help([
            f"observer-kit poll {state_dir} --run {rid}",
            f"observer-kit reply {state_dir} --run {rid} --text \"...\"",
        ]),
    )
    return 0


def _axi_doctor(project: Path, state_name: str) -> int:
    from observer_kit import detect_install_skew, version_info

    project = project.expanduser().resolve()
    state = project / state_name
    ver = version_info()
    skew = detect_install_skew()
    checks = substrate_checks(project, state_name)
    ok = all(c["ok"] for c in checks)
    helps = (
        [f"observer-kit init {project}", str(skew.get("upgrade") or "python -m pip install -e .")]
        if not ok
        else [
            f"observer-kit axi --state-dir {state}",
            f"observer-kit dashboard {state}",
        ]
    )
    if skew.get("install_skew"):
        helps = [str(skew.get("upgrade")), "python3 -m observer_kit axi help"] + list(helps)
    emit(
        toon_kv("project", str(project)),
        toon_kv("state_dir", str(state)),
        toon_kv("version", ver["version"]),
        toon_kv("git_sha", ver["git_sha"]),
        toon_kv("install_skew", bool(skew.get("install_skew"))),
        toon_kv("upgrade", skew.get("upgrade") or ""),
        toon_kv("ok", ok),
        toon_table("checks", checks, ["check", "ok"]),
        toon_help(helps),
    )
    return 0 if ok else 1


def _axi_ps(state_dir: Path, *, scan: bool) -> int:

    dirs = [state_dir] if state_dir.is_dir() else []
    dashboards = dashboard_records(dirs or None, scan_ports=scan)
    watchers = watcher_records(state_dir) if state_dir.is_dir() else []
    dash_rows = [
        {
            "port": d.get("port"),
            "pid": d.get("pid"),
            "parent_alive": (
                _pid_alive(d.get("parent_pid")) if d.get("parent_pid") is not None else True
            ),
            "orphan": bool(d.get("orphan")),
            "state_dir": d.get("state_dir"),
        }
        for d in dashboards
    ]
    watch_rows = [
        {
            "pid": w.get("pid"),
            "parent_alive": (
                _pid_alive(w.get("parent_pid")) if w.get("parent_pid") is not None else True
            ),
            "orphan": bool(w.get("orphan")),
            "target": "all" if w.get("mode") == "all" else w.get("run"),
            "state_dir": w.get("state_dir"),
        }
        for w in watchers
    ]
    orphans = sum(1 for r in dash_rows + watch_rows if r.get("orphan"))
    emit(
        toon_kv("state_dir", str(state_dir)),
        toon_kv("dashboards", len(dash_rows)),
        toon_kv("watchers", len(watch_rows)),
        toon_kv("orphans", orphans),
        toon_table(
            "dashboard",
            dash_rows,
            ["port", "pid", "parent_alive", "orphan", "state_dir"],
        ),
        toon_table(
            "watcher",
            watch_rows,
            ["pid", "parent_alive", "orphan", "target", "state_dir"],
        ),
        toon_help(
            [f"observer-kit stop --sweep {state_dir}"]
            if orphans
            else [
                f"observer-kit axi --state-dir {state_dir}",
                f"observer-kit dashboard {state_dir}",
            ]
        ),
    )
    return 0


def _axi_help() -> int:
    from observer_kit import detect_install_skew, version_info

    ver = version_info()
    skew = detect_install_skew()
    blocks = [
        toon_kv("surface", "observer-axi"),
        toon_kv("axi_schema", AXI_SCHEMA),
        toon_kv("version", ver["version"]),
        toon_kv("git_sha", ver["git_sha"]),
        toon_kv("desc", "Agent eXperience Interface for Observer Kit"),
        toon_kv("install_skew", bool(skew.get("install_skew"))),
        toon_help(axi_help_catalog()),
    ]
    if skew.get("install_skew"):
        blocks.insert(-1, toon_kv("upgrade", skew.get("upgrade") or ""))
        blocks.insert(-1, toon_kv("skew_reason", skew.get("reason") or ""))
    emit(*blocks)
    return 0


