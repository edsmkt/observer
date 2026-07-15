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


def _pid_alive(pid: object) -> bool:
    try:
        p = int(pid)  # type: ignore[arg-type]
        if p <= 0:
            return False
        os.kill(p, 0)
        return True
    except (TypeError, ValueError, OSError):
        return False


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
