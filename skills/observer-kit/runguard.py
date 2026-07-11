"""Run-exclusivity locks + local run ledgers for spending/mutating batch scripts.

Prevents a whole class of batch-job failures: a process nobody realizes is still
running gets a second start, the two double-spend or corrupt shared state, and a
panicked "cleanup" makes it worse. Two primitives:

  acquire_lock(scope) — an OS advisory lock per resource scope. A second process
                        on the same scope HARD-REFUSES while the first holds the
                        lock (SystemExit). Same-PID re-acquire is a no-op
                        (re-entrant). The OS releases a crashed process's lock,
                        so recovery is "just re-run", never "clean up".
  ledger(scope, event, **fields) — append-only JSONL audit file per run:
                        what was attempted, what happened, what it cost.
                        Also the data feed for run_dashboard.py.

  throttle(resource, per_second) — CROSS-PROCESS rate limiter (POSIX flock).
                        Call it before every request to a shared API: all
                        concurrent runs on this machine collectively stay at
                        per_second, first-come-first-served. Lets you run
                        multiple datasets in parallel without multiplying the
                        request rate against one provider account.

Scopes are independent: a 'sourcing' run never blocks a 'crm-write' run.
Parallel datasets: parameterize the scope — acquire_lock(f'enrich-{table}') —
so the same table refuses twice while different tables run side by side.
Only do this when the datasets are PROVABLY disjoint (no shared records), and
throttle() every shared API. If the provider charges per result with a
per-record cap, remember: the in-flight ≤ need invariant only holds within one
process — overlapping datasets in two processes can double-spend.

For new scripts, prefer the boring wrapper:

  run = start_observed_run('enrich-leads', dry_run=args.dry_run)
  with run.step('enrich_lead', table='companies', key=lead.id):
      ...spend or write...
      run.count('leads_enriched')
  run.success()

It still uses the same lock, ledger, dashboard feed, and state dir below.

State dir: $RUNGUARD_STATE_DIR, else ./.observer next to this file. All
processes that should coordinate must use the SAME state dir.

Layout (one continuous lane per workstream):

  .observer/
    EXPLAIN.md              # optional project template seed (copied into new lanes)
    runs/
      <lane>/               # one folder per continuous resume surface
        events.jsonl        # append-only ledger for this lane
        EXPLAIN.md          # operational card for this process
        chat.jsonl          # operator notes for this lane
        controls.jsonl      # pause/stop/approve for this lane
    *.lock / *.throttle     # shared machine coordination at the root only

Legacy flat ledgers and root chat/controls files are still read when present so
older projects keep their history. Locks and throttles stay at the root because
they coordinate across processes and lanes on one machine.
"""
from __future__ import annotations

import atexit
import hashlib
import fcntl
import json
import math
import os
import re
import signal
import sys
import time
import weakref

_STATE_DIR = os.environ.get('RUNGUARD_STATE_DIR') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '.observer')

_held: dict[str, tuple[str, int, int]] = {}  # name -> (path, fd, refcount)
_ledgers: dict[str, str] = {}
_step_sequences: dict[str, int] = {}
_open_runs: weakref.WeakSet = weakref.WeakSet()
_signal_handlers_installed = False
_SAFE_COMPONENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
_CREDENTIAL_FIELD = re.compile(
    r'^(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|'
    r'password|passwd|secret|client[_-]?secret|cookie|set[_-]?cookie)$',
    re.IGNORECASE,
)
# Counter / checkpoint names that would clobber terminal ledger fields if merged.
_RESERVED_RUN_FIELDS = frozenset({
    'status', 'event', 'ts', 'attempt', 'dry_run', 'error', 'reason',
    'checkpoints', 'name', 'source', 'control', 'control_id',
})


class RunPaused(RuntimeError):
    """A deliberate safety pause, not a failed attempt.

    Let this escape the work loop. Do not catch it and call ``run.fail()``: the
    ledger already has an explicit ``run_paused`` terminal event.
    """


class PendingWrite(RuntimeError):
    """A prior write intent has no receipt, so a duplicate write is unsafe."""


def _install_signal_handlers() -> None:
    """Close open runs on SIGTERM/SIGINT so ledgers get a terminal event.

    Default signal termination skips ``atexit``. Install once per process; handlers
    abandon every open ``ObservedRun``, then re-raise the signal.
    """
    global _signal_handlers_installed
    if _signal_handlers_installed:
        return
    if os.environ.get('RUNGUARD_NO_SIGNAL_HANDLERS') == '1':
        return

    def _handle(signum, frame):  # noqa: ARG001 — signal API
        for run in list(_open_runs):
            try:
                if not getattr(run, 'closed', True):
                    run._abandon_if_open()
            except Exception:
                pass
        try:
            signal.signal(signum, signal.SIG_DFL)
        except (ValueError, OSError):
            pass
        try:
            os.kill(os.getpid(), signum)
        except OSError:
            raise SystemExit(128 + int(signum))

    for signum in (getattr(signal, 'SIGTERM', None), getattr(signal, 'SIGINT', None)):
        if signum is None:
            continue
        try:
            # Do not override custom handlers installed by the host harness.
            current = signal.getsignal(signum)
            if current in (signal.SIG_DFL, signal.default_int_handler):
                signal.signal(signum, _handle)
        except (ValueError, OSError):
            # Not in main thread, or signals unsupported.
            pass
    _signal_handlers_installed = True


def _timestamp() -> str:
    """UTC RFC 3339 timestamp with nanoseconds for stable ordering.

    Second-only stamps made same-second chat and ledger events reorder or drop
    under ``after_ts`` filters (for example ``wait_for_feedback``).
    """
    ns = time.time_ns()
    secs, nsec = divmod(ns, 1_000_000_000)
    base = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(secs))
    return f'{base}.{nsec:09d}Z'


def _ts_order_key(ts: object) -> str:
    """Normalize RFC3339 stamps so second-only and nanosecond forms compare safely.

    Lexicographic compare of ``…17Z`` vs ``…17.859Z`` is not chronological.
    Pad or trim the fractional second to nine digits before ``after_ts`` filters.
    """
    raw = str(ts or '').strip()
    if not raw:
        return ''
    body = raw[:-1] if raw.endswith('Z') else raw
    if '.' not in body:
        return f'{body}.000000000Z' if raw.endswith('Z') else f'{body}.000000000'
    head, frac = body.split('.', 1)
    frac = re.sub(r'\D', '', frac)[:9].ljust(9, '0')
    suffix = 'Z' if raw.endswith('Z') else ''
    return f'{head}.{frac}{suffix}'


def _safe_component(value: object, fallback: str) -> str:
    """Turn a human scope/session/resource into one safe, stable filename part."""
    raw = str(value or '').strip()
    if _SAFE_COMPONENT.fullmatch(raw):
        return raw
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', raw).strip('.-') or fallback
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]
    return f'{slug[:80]}--{digest}'


def _state_path(component: object, suffix: str, fallback: str) -> str:
    os.makedirs(_STATE_DIR, exist_ok=True)
    return os.path.join(_STATE_DIR, f'{_safe_component(component, fallback)}{suffix}')


def _session_slug() -> str:
    """Normalized RUNGUARD_SESSION value, or empty when using the default lane."""
    session = os.environ.get('RUNGUARD_SESSION')
    return _safe_component(session, 'session') if session else ''


def _lane_cache_key(scope: str) -> str:
    """Key ledger path cache by session + scope so multi-lane in-process use is safe."""
    session = _session_slug()
    return f'{session}\0{scope}' if session else scope


def _lane_slug(scope: str) -> str:
    """Stable directory name for one continuous lane (session + scope)."""
    scope_name = _safe_component(scope, 'scope')
    session_name = _session_slug()
    return f'{session_name}-{scope_name}' if session_name else scope_name


def _lane_dir(slug: str) -> str:
    """Directory for one continuous lane under ``runs/<slug>/``."""
    return os.path.join(_STATE_DIR, 'runs', slug)


def _lane_events_path(slug: str) -> str:
    """Preferred ledger path: ``runs/<slug>/events.jsonl``."""
    return os.path.join(_lane_dir(slug), 'events.jsonl')


def _lane_legacy_path(slug: str) -> str:
    """Pre-folder flat ledger path: ``<slug>.jsonl`` at the state-dir root."""
    return os.path.join(_STATE_DIR, f'{slug}.jsonl')


def _run_id_for_ledger_path(path: str) -> str:
    """Map a ledger filesystem path to the dashboard run id ``runguard:<lane>``."""
    path = os.path.abspath(path)
    base = os.path.basename(path)
    if base == 'events.jsonl':
        return f'runguard:{os.path.basename(os.path.dirname(path))}'
    if base.endswith('.jsonl'):
        return f'runguard:{base[:-6]}'
    return f'runguard:{base}'


def _lane_from_run_id(run_id: object) -> str:
    """Extract the lane folder name from ``runguard:<lane>`` (optional ``.jsonl``)."""
    raw = str(run_id or '').strip()
    if not raw or raw == 'all':
        return ''
    _kind, sep, name = raw.partition(':')
    if sep:
        raw = name
    raw = os.path.basename(raw)
    if raw.endswith('.jsonl'):
        raw = raw[:-6]
    if not raw or raw in {'.', '..'}:
        return ''
    return raw


def _ensure_lane_explain(slug: str) -> None:
    """Seed ``runs/<slug>/EXPLAIN.md`` from the project template when missing."""
    if not slug:
        return
    dest = os.path.join(_lane_dir(slug), 'EXPLAIN.md')
    if os.path.isfile(dest):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for src in (
        os.path.join(_STATE_DIR, 'EXPLAIN.md'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'EXPLAIN.md'),
    ):
        if not os.path.isfile(src):
            continue
        try:
            with open(src, encoding='utf-8') as fh:
                text = fh.read()
            with open(dest, 'w', encoding='utf-8') as fh:
                fh.write(text)
            return
        except OSError:
            continue


def _side_channel_path(filename: str, run_id: object | None = None) -> str:
    """Preferred path for chat/controls: ``runs/<lane>/<file>``, else state root.

    Project-wide presence uses ``run_id='all'`` and stays at the state root.
    """
    lane = _lane_from_run_id(run_id)
    if lane:
        return os.path.join(_lane_dir(lane), filename)
    return os.path.join(_STATE_DIR, filename)


def _side_channel_read_paths(filename: str, run_id: object | None = None) -> list[str]:
    """Paths to scan for chat/controls, preferred first then legacy root."""
    paths: list[str] = []
    lane = _lane_from_run_id(run_id)
    if lane:
        paths.append(os.path.join(_lane_dir(lane), filename))
        # Legacy: one shared root file filtered by run id.
        paths.append(os.path.join(_STATE_DIR, filename))
    elif run_id in (None, '', 'all'):
        root = os.path.join(_STATE_DIR, filename)
        paths.append(root)
        runs_root = os.path.join(_STATE_DIR, 'runs')
        if os.path.isdir(runs_root):
            for name in sorted(os.listdir(runs_root)):
                candidate = os.path.join(runs_root, name, filename)
                if os.path.isfile(candidate):
                    paths.append(candidate)
    else:
        paths.append(os.path.join(_STATE_DIR, filename))
    # De-dupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        real = os.path.abspath(path)
        if real in seen:
            continue
        seen.add(real)
        ordered.append(path)
    return ordered


def _lane_path(scope: str) -> str:
    """Return the continuous ledger path for a scope in the selected lane.

    New lanes write ``runs/<slug>/events.jsonl``. If only a legacy flat
    ``<slug>.jsonl`` exists, keep appending there so resume history stays one
    continuous file.
    """
    cache_key = _lane_cache_key(scope)
    if cache_key not in _ledgers:
        os.makedirs(_STATE_DIR, exist_ok=True)
        slug = _lane_slug(scope)
        preferred = _lane_events_path(slug)
        legacy = _lane_legacy_path(slug)
        if os.path.isfile(legacy) and not os.path.isfile(preferred):
            _ledgers[cache_key] = legacy
        else:
            os.makedirs(os.path.dirname(preferred), exist_ok=True)
            _ensure_lane_explain(slug)
            _ledgers[cache_key] = preferred
    return _ledgers[cache_key]


def _session_lock_name(scope: str) -> str:
    """Lock name matches the ledger lane: session-scoped comparison/redo runs in parallel.

    Without this, two RUNGUARD_SESSION values for the same source share one flock
    while writing separate ledgers — contradicting the parallel-lane contract.
    """
    session_name = _session_slug()
    if not session_name:
        return scope
    return f'{session_name}--{scope}'


def _append_jsonl(path: str, record: dict) -> None:
    """Append one complete JSON value and fsync it.

    O_APPEND keeps concurrent small writes whole. fsync is required for the
    durability contract: without it a crash can drop just-committed ledger rows
    and write receipts, so resume may re-issue external writes (duplicates) or
    hit PendingWrite when only the receipt was lost.
    """
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    raw = (json.dumps(record, ensure_ascii=False, default=str, sort_keys=True) + '\n').encode('utf-8')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _looks_like_filesystem_path(raw: str) -> bool:
    """True when ``raw`` is a path identity, not a sheet/table/export id."""
    if raw.startswith('~') or raw.startswith('.') or os.path.isabs(raw):
        return True
    if os.sep in raw or (os.altsep and os.altsep in raw):
        return True
    # Windows drive path (C:\... or C:/...)
    if len(raw) >= 3 and raw[1] == ':' and raw[2] in '\\/':
        return True
    return False


def _source_identity(source: object) -> str:
    """Stable source identity that does not flip when a path is created later.

    Path-like values always go through ``realpath`` so:

    - missing and existing paths share one identity (realpath still normalizes
      existing parents, e.g. macOS ``/var`` → ``/private/var``);
    - symlink aliases resolve to the same target when the path exists.

    Non-path ids (sheet/table/export keys) are kept verbatim.
    """
    raw = str(source or '').strip()
    if not raw:
        raise ValueError('source must be a real source identity, not an empty label')
    if not _looks_like_filesystem_path(raw):
        return raw
    try:
        return os.path.realpath(os.path.expanduser(raw))
    except OSError:
        return os.path.abspath(os.path.expanduser(raw))


def _iter_jsonl(path: str):
    """Yield complete valid JSONL records. A partial tail is ignored until complete."""
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                if not line.endswith('\n'):
                    break
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _redact_sample(value: object, sensitive_fields: set[str]) -> object:
    """Copy a JSON-like sample while replacing credential-bearing fields."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() in sensitive_fields or _CREDENTIAL_FIELD.fullmatch(name):
                result[key] = '[REDACTED]'
            else:
                result[key] = _redact_sample(item, sensitive_fields)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_sample(item, sensitive_fields) for item in value]
    return value


def _schema_profile(value: object) -> dict[str, list[str]]:
    """Return a compact path-to-types profile for a representative JSON value."""
    paths: dict[str, set[str]] = {}

    def add(path: str, kind: str) -> None:
        paths.setdefault(path, set()).add(kind)

    def visit(item: object, path: str) -> None:
        if item is None:
            add(path, 'null')
        elif isinstance(item, bool):
            add(path, 'boolean')
        elif isinstance(item, int):
            add(path, 'integer')
        elif isinstance(item, float):
            add(path, 'number')
        elif isinstance(item, str):
            add(path, 'string')
        elif isinstance(item, dict):
            add(path, 'object')
            for key, child in item.items():
                visit(child, f'{path}.{key}')
        elif isinstance(item, (list, tuple)):
            add(path, 'array')
            for child in item:
                visit(child, f'{path}[]')
        else:
            add(path, type(item).__name__)

    visit(value, '$')
    return {path: sorted(kinds) for path, kinds in sorted(paths.items())}


def _file_hash(path: str) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def input_snapshot(source: object, records: object | None = None,
                   version: object | None = None) -> dict:
    """Describe the exact input a run reviewed, without storing its raw contents.

    Pass a path to hash a durable file, or pass ``records=`` for a small loaded
    fixture/list. For remote sources (a sheet/table/export ID), the identity and
    optional version still make a useful immutable manifest entry.
    """
    identity = str(source or '').strip()
    if not identity:
        raise ValueError('input snapshot needs a source identity')
    try:
        result = {'source': _source_identity(identity)}
    except ValueError:
        result = {'source': identity}
    if version is not None:
        result['version'] = str(version)
    # Hash using the resolved path when possible so ~/ and relative forms still
    # fingerprint file contents (not just the source name).
    file_path = None
    for candidate in (result.get('source'), os.path.expanduser(identity), identity):
        if candidate and os.path.isfile(str(candidate)):
            file_path = str(candidate)
            break
    if records is None and file_path:
        try:
            stat = os.stat(file_path)
            result['bytes'] = stat.st_size
            digest = hashlib.sha256()
            rows = 0
            with open(file_path, 'rb') as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                    digest.update(chunk)
                    rows += chunk.count(b'\n')
            result['rows'] = rows
            result['sha256'] = digest.hexdigest()
        except OSError:
            pass
        return result
    if records is not None:
        if isinstance(records, (str, bytes, dict)):
            values = [records]
        else:
            values = list(records)
        payload = '\n'.join(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
            for value in values
        ).encode('utf-8')
        result['sha256'] = hashlib.sha256(payload).hexdigest()
        result['rows'] = len(values)
        fields = set()
        for value in values:
            if isinstance(value, dict):
                fields.update(str(key) for key in value)
        if fields:
            result['fields'] = sorted(fields)
        return result
    result['sha256'] = _canonical_hash({'source': result['source'], 'version': result.get('version')})
    return result


def replay_fixture(path: str) -> list:
    """Load a JSON array, one JSON object, or JSONL fixture for a dry simulation."""
    with open(path, encoding='utf-8') as fh:
        raw = fh.read()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = []
        for line in raw.splitlines():
            if line.strip():
                value.append(json.loads(line))
    return value if isinstance(value, list) else [value]


def _control_path(run_id: object | None = None) -> str:
    """Write path for control requests (per-lane when ``run_id`` is a lane)."""
    return _side_channel_path('controls.jsonl', run_id)


def post_control(run_id: str, kind: str, note: str = '') -> dict:
    """Durably request a run action. The script/harness remains the decision-maker."""
    if kind not in {'pause', 'stop_after_record', 'approve_full_run'}:
        raise ValueError(f'unsupported control request: {kind}')
    rec = {'id': _canonical_hash([run_id, kind, note, time.time_ns(), os.getpid()])[:20],
           'ts': _timestamp(), 'run': str(run_id), 'kind': kind, 'note': str(note)[:1000]}
    path = _control_path(run_id)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    if _lane_from_run_id(run_id):
        _ensure_lane_explain(_lane_from_run_id(run_id))
    _append_jsonl(path, rec)
    return rec


def read_controls(run_id: str | None = None) -> list:
    """Read durable operator control requests, newest last."""
    out = []
    lane = _lane_from_run_id(run_id) if run_id else ''
    lane_control = os.path.abspath(_control_path(run_id)) if lane else ''
    for path in _side_channel_read_paths('controls.jsonl', run_id):
        path_abs = os.path.abspath(path)
        for rec in _iter_jsonl(path):
            tagged = rec.get('run')
            if run_id and tagged and tagged != run_id:
                continue
            if run_id and not tagged and path_abs != lane_control:
                # Untagged rows only count from the lane's own controls file.
                continue
            out.append(rec)
    return out


def source_scope(workflow: str, source: str) -> str:
    """Stable lock scope from the real source identity, not a run nickname.

    Pass a resolved CSV path, sheet ID, table ID, or another immutable source
    identifier. Two invocations with the same source get the same scope; a
    separate source gets a different scope and can run in parallel when it is
    provably disjoint.

    Path identities stay stable if the file is created after the first call:
    missing and existing paths share the same absolute form until a symlink
    target is resolved when the path exists.
    """
    identity = _source_identity(source)
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    return f'{_safe_component(workflow, "workflow")}-source-{digest}'


def _restore_stop_requested(scope: str) -> bool:
    """Re-arm stop-after-record after a crash mid-stop; clear after stop pause/finish.

    ``control_acknowledged`` alone must not die with the process: if the operator
    asked to stop and the worker died before ``run_paused``, the next attempt on
    the same lane still honors that stop. A completed stop pause or successful
    finish clears the arm so a deliberate resume can continue remaining work.
    """
    armed = False
    for event in _iter_jsonl(_lane_path(scope)):
        if (event.get('event') == 'control_acknowledged'
                and event.get('control') == 'stop_after_record'):
            armed = True
            continue
        if event.get('event') == 'run_paused':
            # Prefer the structured control field; keep English-substring fallback
            # for ledgers written before control= was stamped on stop pauses.
            if event.get('control') == 'stop_after_record':
                armed = False
            else:
                reason = str(event.get('reason') or '').lower()
                if 'stop after' in reason or 'stop_after' in reason:
                    armed = False
            continue
        # Any terminal close of the attempt clears stop arming (success, fail,
        # abandon). Otherwise run.fail() would leave stop stuck on the next resume.
        if event.get('event') in {'run_finished', 'run_failed', 'run_abandoned'}:
            armed = False
    return armed


def _restore_unique_owners(scope: str) -> dict[str, str]:
    """Rebuild marker -> record_key ownership for durable unique checks.

    Same key may re-validate after resume (idempotent). A different key still
    conflicts. Dead-lettered keys release their markers so a failed write can be
    retried instead of permanently blocking the value.
    """
    owners: dict[str, str] = {}
    for event in _iter_jsonl(_lane_path(scope)):
        kind = event.get('event')
        if kind == 'unique_reserved':
            record_key = str(event.get('key') or event.get('record_key') or '')
            for marker in event.get('markers') or []:
                owners[str(marker)] = record_key
            continue
        if kind == 'unique_released':
            for marker in event.get('markers') or []:
                owners.pop(str(marker), None)
            continue
        if kind == 'dead_letter':
            failed_key = str(event.get('record_key') or event.get('key') or '')
            if failed_key:
                owners = {marker: key for marker, key in owners.items()
                          if key != failed_key}
    return owners


def _approve_control_cutoff(scope: str) -> str:
    """Timestamp after which unacked full-run approvals remain valid.

    Completing any non-dry attempt expires earlier approvals so one operator
    click cannot authorize an unbounded series of full runs on the same lane.
    """
    cutoff = ''
    for event in _iter_jsonl(_lane_path(scope)):
        if event.get('event') not in {
            'run_finished', 'run_failed', 'run_abandoned', 'run_paused',
        }:
            continue
        if event.get('dry_run') is True:
            continue
        ts = str(event.get('ts') or '')
        if ts and _ts_order_key(ts) >= _ts_order_key(cutoff):
            cutoff = ts
    return cutoff


def _lockfile(name: str) -> str:
    return _state_path(name, '.lock', 'scope')


def _read_lock(fd: int) -> dict:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 8192).decode('utf-8', 'replace').strip()
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def _write_lock(fd: int, payload: dict) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, raw)
    os.fsync(fd)


def acquire_lock(name: str) -> None:
    """Exclusive per-scope advisory lock. Refuse while another process holds it.

    Same-process re-acquire is refcounted: nested ObservedRun holders on one
    scope share one fd, and only the final ``release_lock`` unlocks.
    """
    if name in _held:
        path, fd, refs = _held[name]
        _held[name] = (path, fd, refs + 1)
        return
    path = _lockfile(name)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock = _read_lock(fd)
        os.close(fd)
        pid = lock.get('pid', '?')
        started = lock.get('started', '?')
        raise SystemExit(
            f"WARNING: '{name}' is already running "
            f"(pid {pid}, started {started}).\n"
            "Starting it again can cause duplicate provider charges, duplicate CRM or "
            "sheet writes, and corrupted run history.\n"
            f"Wait for it to finish, or deliberately stop it first: kill {pid}")
    try:
        _write_lock(fd, {'pid': os.getpid(), 'started': _timestamp(), 'scope': name})
    except BaseException:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        raise
    _held[name] = (path, fd, 1)
    # Force-full unlock on process exit even if nested holders remain.
    atexit.register(lambda n=name: release_lock(n, force=True))


def release_lock(name: str, force: bool = False) -> None:
    held = _held.get(name)
    if not held:
        return
    path, fd, refs = held
    if not force and refs > 1:
        _held[name] = (path, fd, refs - 1)
        return
    _held.pop(name, None)
    try:
        # Keep the inode in place. Removing a flocked lockfile creates a race in
        # which a second process can lock a new inode while this process holds old one.
        _write_lock(fd, {'pid': 0, 'released': _timestamp(), 'scope': name})
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def ledger(scope: str, event: str, **fields) -> None:
    """Append one audit record to this run's JSONL ledger for the given scope.

    Runs over the SAME source share ONE continuous run by default: the ledger is
    named for the scope (which should encode the dataset identity, e.g.
    'enrich-prospects-csv'), so re-running the same source keeps appending to the
    same run — the dashboard shows the iterations in one table with before/after
    "· was X", and chat notes / ✓ persist across re-runs.

    Set RUNGUARD_SESSION=<slug> only to open a SEPARATE lane (a dated slug for a
    fresh weekly run, or a unique label for a clean A/B) →
    ``runs/<session>-<scope>/events.jsonl``."""
    path = _lane_path(scope)
    # Reserved keys always win so a **row payload cannot clobber event identity.
    rec = dict(fields)
    rec['event'] = event
    rec['ts'] = _timestamp()
    _append_jsonl(path, rec)
    if event == 'run_started':
        # Marker used by the CLI and harness hooks to create or reuse one watcher.
        rid = _run_id_for_ledger_path(path)
        sys.stderr.write(
            f"OBSERVER_RUN_STARTED {rid}\n"
            f"[observer] observer-kit run creates or reuses this run's chat watcher.\n"
            f"           direct worker launches need one run-scoped bridge for {rid}.\n")


def ledger_path(scope: str) -> str | None:
    return _ledgers.get(_lane_cache_key(scope))


def current_run_id(scope: str) -> str | None:
    """The dashboard run id for this scope's ledger (``runguard:<lane>``).

    Pass it to read_chat/post_chat so chat lands on the same run the dashboard is
    showing. With RUNGUARD_SESSION pinned this stays stable across re-runs, so
    notes persist.
    """
    p = _ledgers.get(_lane_cache_key(scope))
    return _run_id_for_ledger_path(p) if p else None


def schema_errors(record: dict, contract: dict | None = None) -> list[str]:
    """Return simple, portable schema-contract errors for one transformed record.

    ``contract`` supports ``required``, ``types`` and ``allowed``. It is kept
    declarative so the same contract can live beside a CSV, sheet, database, or
    API sink without pulling in a validation framework.
    """
    contract = contract or {}
    errors = []
    for field in contract.get('required', []):
        value = record.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f'missing required field: {field}')
    type_map = {'str': str, 'string': str, 'int': int, 'integer': int,
                'float': float, 'number': (int, float), 'bool': bool,
                'boolean': bool, 'dict': dict, 'object': dict, 'list': list,
                'array': list}
    for field, expected in (contract.get('types') or {}).items():
        if field not in record or record[field] is None:
            continue
        expected_type = type_map.get(expected, expected) if isinstance(expected, str) else expected
        if not isinstance(expected_type, type) and not isinstance(expected_type, tuple):
            errors.append(f'invalid type contract for {field}')
        elif isinstance(record[field], bool) and expected_type in {int, float, (int, float)}:
            errors.append(f'wrong type for {field}: expected {expected}')
        elif not isinstance(record[field], expected_type):
            errors.append(f'wrong type for {field}: expected {expected}')
    for field, allowed in (contract.get('allowed') or {}).items():
        if field in record and record[field] not in set(allowed):
            errors.append(f'unsupported value for {field}: {record[field]!r}')
    return errors


def policy_errors(record: dict, policy: dict | None = None,
                  current: dict | None = None, destination: str | None = None) -> list[str]:
    """Return policy violations before a side effect is attempted.

    Supported generic rules: allowed destinations, consent/suppression booleans,
    fields that must stay absent, and protected fields that cannot be overwritten.
    A small ``check(record, current)`` callable is available for a project rule
    that cannot be expressed by those primitives.
    """
    policy = policy or {}
    current = current or {}
    errors = []
    allowed = policy.get('allowed_destinations')
    if destination and allowed is not None and destination not in set(allowed):
        errors.append(f'destination not allowed: {destination}')
    for field in policy.get('required_true', []):
        if record.get(field) is not True:
            errors.append(f'{field} must be true')
    for field in policy.get('forbidden_true', []):
        if record.get(field) is True:
            errors.append(f'{field} must not be true')
    for field in policy.get('forbidden_fields', []):
        if record.get(field) not in (None, '', [], {}):
            errors.append(f'field is not allowed: {field}')
    for field in policy.get('protected_fields', []):
        old, new = current.get(field), record.get(field)
        if old not in (None, '') and new not in (None, old):
            errors.append(f'protected field would change: {field}')
    check = policy.get('check')
    if callable(check):
        value = check(record, current)
        if value:
            errors.extend(value if isinstance(value, list) else [str(value)])
    return errors


def operation_key(record_key: object, destination: str,
                  transform_version: object | None = None) -> str:
    """Stable idempotency key for one record, sink, and transform revision.

    The external write registry is **per destination**, not per flow node.
    Two nodes that share the same business key and write to the same
    destination+transform_version are one sink write: the second
    ``write_intent`` is an idempotent skip. Give each node a distinct
    destination (or transform version) when they must both land.
    """
    return _canonical_hash({'record_key': str(record_key), 'destination': destination,
                            'transform_version': transform_version or ''})


def _write_registry(destination: str) -> tuple[str, str]:
    stem = f'write-{destination}'
    return (_state_path(stem, '.receipts.jsonl', 'destination'),
            _state_path(stem, '.receipt.guard', 'destination'))


def _claim_write(destination: str, ticket: dict) -> str:
    """Atomically reserve an operation key, returning new/received/pending."""
    registry, guard = _write_registry(destination)
    fd = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        latest = None
        for event in _iter_jsonl(registry):
            if event.get('operation_key') == ticket['operation_key']:
                latest = event
        state = (latest or {}).get('state')
        if state in {'written', 'verified'}:
            return 'received'
        if state == 'pending':
            return 'pending'
        _append_jsonl(registry, {'ts': _timestamp(), 'state': 'pending', **ticket})
        return 'new'
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _record_receipt(destination: str, receipt: dict) -> None:
    registry, guard = _write_registry(destination)
    fd = os.open(guard, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _append_jsonl(registry, receipt)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class ObservedStep:
    """Context manager returned by ObservedRun.step()."""

    def __init__(self, run: 'ObservedRun', name: str, fields: dict):
        self.run = run
        self.name = name
        self.fields = dict(fields)
        self.table = self.fields.pop('table', 'steps')
        key = self.fields.pop('key', None)
        if key is None:
            _step_sequences[run.scope] = _step_sequences.get(run.scope, 0) + 1
            key = f'{name}:{_step_sequences[run.scope]}'
        self.key = str(key)

    def __enter__(self):
        ledger(self.run.scope, 'record', table=self.table, key=self.key,
               step=self.name, status='running', dry_run=self.run.dry_run,
               attempt=self.run.attempt, **self.fields)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            ledger(self.run.scope, 'record', table=self.table, key=self.key,
                   step=self.name, status='done', dry_run=self.run.dry_run,
                   attempt=self.run.attempt, **self.fields)
            return False
        # Intentional safety pause must not look like a failed mutation. Treating
        # RunPaused as step_exception polluted Data/Attention and replay lists.
        if exc_type is RunPaused or isinstance(exc, RunPaused):
            ledger(self.run.scope, 'record', table=self.table, key=self.key,
                   step=self.name, status='paused', reason=str(exc),
                   dry_run=self.run.dry_run, attempt=self.run.attempt, **self.fields)
            return False
        ledger(self.run.scope, 'record', table=self.table, key=self.key,
               step=self.name, status='failed', error=str(exc),
               dry_run=self.run.dry_run, attempt=self.run.attempt, **self.fields)
        dead_fields = dict(self.fields)
        dead_fields.setdefault('reason', 'step_exception')
        dead_fields.setdefault('step', self.name)
        self.run.dead_letter(self.key, exc, table=self.table, **dead_fields)
        return False


class ObservedRun:
    """Small run contract for scripts that spend credits or mutate shared state.

    The wrapper deliberately stays thin: it acquires the existing lock, writes
    the existing JSONL ledger, exposes dry-run state, and gives scripts a common
    success/fail/counter/checkpoint shape. The low-level primitives remain
    available for advanced flows.
    """

    def __init__(self, name: str, lock_key: str | None = None,
                 dry_run: bool = False, description: str | None = None,
                 source: str | None = None, input_snapshot: dict | None = None,
                 destination: str | None = None, transform_version: str | None = None,
                 policy_version: str | None = None, script: str | None = None,
                 config: object | None = None, **fields):
        self.name = name
        if source is not None and lock_key is not None:
            raise ValueError('pass source= for a source-derived scope, not both source and lock_key')
        self.source = source
        self.scope = source_scope(name, source) if source is not None else (lock_key or name)
        # Locks follow the ledger lane (including RUNGUARD_SESSION) so comparison
        # and parallel redo sessions do not hard-contend the same flock.
        self.lock_key = _session_lock_name(self.scope)
        self.dry_run = bool(dry_run)
        self.description = description
        self.destination = destination
        self.transform_version = transform_version
        self.policy_version = policy_version
        # File inputs get a free content fingerprint. Remote sources need the
        # caller to supply an export/version because an ID alone cannot reveal
        # whether its contents changed.
        self.input_snapshot = (
            dict(input_snapshot) if input_snapshot else
            (globals()['input_snapshot'](source) if source and os.path.isfile(source) else None)
        )
        self.attempt = f'{os.getpid()}-{time.time_ns()}'
        self.counters: dict[str, int | float] = {}
        self.checkpoints: dict[str, object] = {}
        self._unique_owners: dict[str, str] = {}
        self._seen_controls: set[str] = set()
        self._schema_profiles: dict[str, dict[str, set[str]]] = {}
        self._schema_samples: dict[str, int] = {}
        self.stop_requested = False
        self.closed = False
        acquire_lock(self.lock_key)
        started = dict(fields)
        started.update({'name': self.name, 'dry_run': self.dry_run, 'attempt': self.attempt})
        if source is not None:
            started['source'] = source
        if description:
            started['description'] = description
        ledger(self.scope, 'run_started', **started)
        self.run_id = current_run_id(self.scope)
        # A control is a one-shot request for this ledger lane. Persisted
        # acknowledgements prevent a recovered/retried process from re-applying
        # pause/stop acks. stop_after_record stays armed across crash/resume until
        # a stop pause or successful finish (see _restore_stop_requested).
        # approve_full_run is NOT auto-acked here — see check_controls().
        self._seen_controls = {
            str(event['control_id']) for event in _iter_jsonl(_lane_path(self.scope))
            if event.get('event') == 'control_acknowledged' and event.get('control_id')
        }
        self._unique_owners = _restore_unique_owners(self.scope)
        self._approve_cutoff = _approve_control_cutoff(self.scope)
        self.stop_requested = _restore_stop_requested(self.scope)
        self.manifest(snapshot=self.input_snapshot, destination=destination,
                      transform_version=transform_version, policy_version=policy_version,
                      script=script, config=config)
        _open_runs.add(self)
        _install_signal_handlers()
        atexit.register(self._abandon_if_open)

    def _event(self, event: str, **fields) -> None:
        ledger(self.scope, event, attempt=self.attempt, **fields)

    def _mark_closed(self) -> None:
        self.closed = True
        try:
            _open_runs.discard(self)
        except Exception:
            pass

    def _terminal_payload(self, **fields) -> dict:
        """Merge counters/checkpoints without clobbering lifecycle field names."""
        payload = {
            key: value for key, value in self.counters.items()
            if key not in _RESERVED_RUN_FIELDS
        }
        payload.update({
            key: value for key, value in fields.items()
            if key not in _RESERVED_RUN_FIELDS
        })
        if self.checkpoints:
            payload['checkpoints'] = dict(self.checkpoints)
        # Surface reserved counter names under a nested map so metrics survive.
        reserved_counts = {
            key: value for key, value in self.counters.items()
            if key in _RESERVED_RUN_FIELDS
        }
        if reserved_counts:
            payload['counter_overrides'] = reserved_counts
        return payload

    def _abandon_if_open(self) -> None:
        if self.closed:
            return
        self._event('run_abandoned', status='failed', dry_run=self.dry_run,
                    error='process exited before run.success() or run.fail()',
                    **self._terminal_payload())
        release_lock(self.lock_key)
        self._mark_closed()

    def step(self, name: str, **fields) -> ObservedStep:
        """Log one visible unit of work as a generic dashboard record."""
        return ObservedStep(self, name, fields)

    def count(self, name: str, amount: int | float = 1) -> int | float:
        """Increment an in-memory counter and emit a lightweight metric event."""
        self.counters[name] = self.counters.get(name, 0) + amount
        self._event('metric', metric=name, value=self.counters[name], increment=amount)
        return self.counters[name]

    def checkpoint(self, name: str, value) -> None:
        """Record the last durable point the script can resume from."""
        self.checkpoints[name] = value
        self._event('checkpoint', checkpoint=name, value=value)

    def _consume_pending_approvals(self, note: str) -> None:
        """Spend unacked full-run approvals when a non-dry attempt ends."""
        if self.dry_run:
            return
        for control in read_controls(self.run_id):
            if control.get('kind') != 'approve_full_run':
                continue
            control_id = str(control.get('id') or _canonical_hash(control))
            if control_id in self._seen_controls:
                continue
            control_ts = str(control.get('ts') or '')
            if (self._approve_cutoff
                    and control_ts
                    and _ts_order_key(control_ts) <= _ts_order_key(self._approve_cutoff)):
                continue
            self.acknowledge_control(control, note=note)

    def success(self, **fields) -> None:
        if self.closed:
            return
        self._consume_pending_approvals('consumed by completed full run')
        self._event('run_finished', status='success', dry_run=self.dry_run,
                    **self._terminal_payload(**fields))
        release_lock(self.lock_key)
        self._mark_closed()

    def fail(self, error: BaseException | str, **fields) -> None:
        if self.closed:
            return
        self._consume_pending_approvals('consumed by failed full run')
        self._event('run_failed', status='failed', error=str(error),
                    dry_run=self.dry_run, **self._terminal_payload(**fields))
        release_lock(self.lock_key)
        self._mark_closed()

    def manifest(self, snapshot: dict | None = None, destination: str | None = None,
                 transform_version: str | None = None, policy_version: str | None = None,
                 script: str | None = None, config: object | None = None, **fields) -> dict:
        """Record run provenance and warn when the same lane's input changed.

        The manifest stores hashes and identities, not input rows or credentials.
        Pass ``input_snapshot(path_or_id, records=...)`` when the source needs a
        content fingerprint in addition to its source identity.
        """
        snapshot = dict(snapshot or self.input_snapshot or {})
        destination = destination if destination is not None else self.destination
        transform_version = transform_version if transform_version is not None else self.transform_version
        policy_version = policy_version if policy_version is not None else self.policy_version
        prior = None
        for event in _iter_jsonl(_lane_path(self.scope)):
            if event.get('event') == 'run_manifest' and event.get('attempt') != self.attempt:
                prior = event.get('input_snapshot') or prior
        old_hash = (prior or {}).get('sha256')
        new_hash = snapshot.get('sha256')
        if old_hash and new_hash and old_hash != new_hash:
            self._event('input_changed', status='warning', previous_sha256=old_hash,
                        current_sha256=new_hash,
                        note='input changed since the previous attempt; review before resuming')
        payload = dict(fields)
        payload.update({'name': self.name, 'dry_run': self.dry_run})
        if self.source is not None:
            payload['source'] = self.source
        if snapshot:
            payload['input_snapshot'] = snapshot
        if destination:
            payload['destination'] = destination
        if transform_version:
            payload['transform_version'] = transform_version
        if policy_version:
            payload['policy_version'] = policy_version
        if script:
            digest = _file_hash(script)
            if digest:
                payload['script_sha256'] = digest
        if config is not None:
            if isinstance(config, str) and os.path.isfile(config):
                payload['config_sha256'] = _file_hash(config)
            else:
                payload['config_sha256'] = _canonical_hash(config)
        self._event('run_manifest', **payload)
        return payload

    def preview(self, samples: object, estimates: dict | None = None, **fields) -> dict:
        """Show a small, reviewable impact preview before an irreversible run."""
        if isinstance(samples, (str, bytes, dict)):
            sample_rows = [samples]
        else:
            sample_rows = list(samples)
        payload = dict(fields)
        payload.update({'samples': sample_rows[:25], 'sample_count': len(sample_rows)})
        if estimates:
            payload['estimates'] = dict(estimates)
        self._event('impact_preview', **payload)
        return payload

    def schema_sample(self, table: str, key: object, response: object,
                      raw_field: str = 'response_json',
                      sensitive_fields: tuple[str, ...] | list[str] | set[str] = (),
                      **fields) -> object:
        """Expose one bounded API response body and its observed shape for review.

        Common credential fields are redacted recursively. Add provider-specific
        response fields through ``sensitive_fields``. Request bodies and headers
        are separate API-client concerns. The returned value matches the response
        JSON written to the sample row.
        """
        extra_sensitive = {str(name).lower() for name in sensitive_fields}
        safe_response = _redact_sample(response, extra_sensitive)
        observed = _schema_profile(safe_response)
        profile = self._schema_profiles.setdefault(table, {})
        for path, kinds in observed.items():
            profile.setdefault(path, set()).update(kinds)
        self._schema_samples[table] = self._schema_samples.get(table, 0) + 1
        self._event(
            'schema_observed', table=table,
            sample_count=self._schema_samples[table],
            paths={path: sorted(kinds) for path, kinds in sorted(profile.items())},
        )
        row = dict(fields)
        row.pop('table', None)
        row.pop('key', None)
        row.setdefault('sample', True)
        if raw_field:
            row[raw_field] = safe_response
        self._event('record', table=table, key=str(key), **row)
        return safe_response

    def validate(self, record: dict, key: object, contract: dict,
                 table: str = 'records', on_error: str = 'pause') -> bool:
        """Check a transformed record before a write; pause on schema drift by default.

        ``unique`` ownership is durable: another record key cannot claim the same
        value after resume. Re-validating the **same** key is idempotent so a
        successful earlier attempt does not phantom-pause. Dead-lettered keys
        release their markers so a failed write can be retried.
        """
        errors = schema_errors(record, contract)
        new_unique = []
        record_key = str(key)
        for field in contract.get('unique', []):
            value = record.get(field)
            if value in (None, ''):
                continue
            marker = _canonical_hash([table, field, value])
            owner = self._unique_owners.get(marker)
            if owner is not None and owner != record_key:
                errors.append(f'duplicate unique field: {field}')
            elif owner == record_key:
                continue  # already reserved for this key (resume / re-validate)
            else:
                new_unique.append(marker)
        if not errors:
            if new_unique:
                for marker in new_unique:
                    self._unique_owners[marker] = record_key
                self._event('unique_reserved', table=table, key=record_key,
                            markers=list(new_unique))
            return True
        self._event('schema_violation', status='failed', key=record_key, table=table,
                    errors=errors)
        self.dead_letter(key, '; '.join(errors), table=table, reason='schema_violation')
        if on_error == 'pause':
            self.pause(f'schema drift for {key}: {errors[0]}')
        return False

    def allow_write(self, record: dict, key: object, policy: dict | None = None,
                    current: dict | None = None, destination: str | None = None,
                    on_error: str = 'skip') -> bool:
        """Enforce generic write rules before a CRM, sheet, database, or API mutation."""
        destination = destination or self.destination
        errors = policy_errors(record, policy, current=current, destination=destination)
        if not errors:
            return True
        self._event('policy_blocked', status='blocked', key=str(key), destination=destination,
                    errors=errors)
        self.dead_letter(key, '; '.join(errors), reason='policy_blocked', destination=destination)
        if on_error == 'pause':
            self.pause(f'policy blocked {key}: {errors[0]}')
        return False

    def pause(self, reason: str, **fields) -> None:
        """Close this attempt deliberately and release the source lock."""
        if not self.closed:
            self._event('run_paused', status='paused', dry_run=self.dry_run,
                        reason=str(reason), **fields)
            release_lock(self.lock_key)
            self._mark_closed()
        raise RunPaused(str(reason))

    def dead_letter(self, record_key: object, error: object, retry: int = 0,
                    table: str = 'dead_letters', payload_ref: str | None = None,
                    node_id: str | None = None,
                    **fields) -> None:
        """Keep a failed record replayable without logging its full sensitive payload.

        Pass ``node_id`` (or leave it in ``fields``) when several nodes share the
        same business key so ``replay_candidates`` can keep them distinct.
        """
        payload = dict(fields)
        key = str(record_key)
        payload.update({'record_key': key, 'error': str(error), 'retry': retry})
        if payload_ref:
            payload['payload_ref'] = payload_ref
        # Prefer an explicit node_id; fall back to table when it is a node label.
        if node_id is not None:
            payload['node_id'] = str(node_id)
        elif payload.get('node_id') is None and table and table not in {
            'dead_letters', 'records', 'writes',
        }:
            payload.setdefault('node_id', str(table))
        released = [marker for marker, owner in self._unique_owners.items() if owner == key]
        if released:
            for marker in released:
                self._unique_owners.pop(marker, None)
            self._event('unique_released', key=key, markers=released,
                        reason='dead_letter')
        self._event('dead_letter', status='failed', **payload)
        self._event('record', table='dead_letters', key=key, status='failed', **payload)

    def lineage(self, record_key: object, **fields) -> None:
        """Attach provenance (source URL/provider/reasoning/version) to one output row."""
        self._event('lineage', record_key=str(record_key), **fields)

    def simulate(self, fixture: str) -> list:
        """Load deterministic fixture data and record that this attempt is simulated."""
        records = replay_fixture(fixture)
        snapshot = input_snapshot(fixture, records=records)
        self._event('simulation', fixture=os.path.realpath(fixture), input_snapshot=snapshot,
                    records=len(records))
        return records

    def gate(self, name: str, observed: int | float, minimum: int | float | None = None,
             maximum: int | float | None = None, action: str = 'pause', **fields) -> bool:
        """Record a batch quality gate; a failed default gate pauses before more writes."""
        failed = ((minimum is not None and observed < minimum) or
                  (maximum is not None and observed > maximum))
        self._event('quality_gate', gate=name, observed=observed, minimum=minimum,
                    maximum=maximum, status='failed' if failed else 'passed', **fields)
        if failed and action == 'pause':
            self.pause(f'quality gate {name} failed ({observed})')
        return not failed

    def check_controls(self, after_record: bool = False) -> list[dict]:
        """Acknowledge dashboard requests at a script-defined safe point.

        ``pause`` acts immediately at the next check. ``stop_after_record`` is
        remembered until a check made with ``after_record=True``, and stays
        armed across crash/resume until a stop pause or successful finish.

        ``approve_full_run`` is returned every time while unacked; it is never
        auto-acknowledged here so item-loop checks cannot burn the operator's
        full-run approval. Call ``acknowledge_control`` when the harness acts
        on approval. Approval never starts a full run by itself.
        """
        fresh = []
        for control in read_controls(self.run_id):
            control_id = str(control.get('id') or _canonical_hash(control))
            kind = control.get('kind')
            if kind == 'approve_full_run':
                # Surface approval without consuming it. Scripts that only call
                # check_controls() in the work loop must not silently burn it.
                # Approvals older than the last completed full-run attempt are
                # expired so one click cannot authorize unbounded full runs.
                if control_id in self._seen_controls:
                    continue
                control_ts = str(control.get('ts') or '')
                if (self._approve_cutoff
                        and control_ts
                        and _ts_order_key(control_ts) <= _ts_order_key(self._approve_cutoff)):
                    continue
                fresh.append(control)
                continue
            if control_id in self._seen_controls:
                continue
            self._seen_controls.add(control_id)
            fresh.append(control)
            self._event('control_acknowledged', control_id=control_id,
                        control=kind, note=control.get('note', ''))
            if kind == 'pause':
                self.pause('operator requested pause', control='pause')
            if kind == 'stop_after_record':
                self.stop_requested = True
        if after_record and self.stop_requested:
            self.pause('operator requested stop after current record',
                       control='stop_after_record')
        return fresh

    def acknowledge_control(self, control: dict | str, note: str = '') -> None:
        """Persist that the harness acted on a control (especially full-run approval).

        Pass the control dict returned by ``check_controls`` or its ``id``.
        Safe to call more than once for the same id.
        """
        if isinstance(control, dict):
            control_id = str(control.get('id') or _canonical_hash(control))
            kind = control.get('kind') or 'approve_full_run'
            note = note or str(control.get('note') or '')
        else:
            control_id = str(control)
            kind = 'approve_full_run'
        if control_id in self._seen_controls:
            return
        self._seen_controls.add(control_id)
        self._event('control_acknowledged', control_id=control_id,
                    control=kind, note=note)

    def write_intent(self, record_key: object, destination: str | None = None,
                     transform_version: str | None = None, payload: object | None = None,
                     payload_ref: str | None = None, node_id: str | None = None,
                     **fields) -> dict | None:
        """Reserve one external write before calling a sink.

        A returned ticket is the sink idempotency key. Pass it to the provider
        when supported, then call ``write_receipt`` only after the provider or
        destination has confirmed the write. A prior pending ticket raises
        ``PendingWrite`` instead of guessing whether an interrupted call landed.

        ``node_id`` is stamped on the ticket so receipts match multi-node
        ``dead_letter`` identity in ``replay_candidates``. It does **not**
        partition the destination write registry: registry identity is still
        ``(record_key, destination, transform_version)`` via ``operation_key``.
        Nodes that must both write the same key need distinct destinations (or
        transform versions).
        """
        destination = destination or self.destination
        if not destination:
            raise ValueError('write_intent needs a destination')
        transform_version = transform_version if transform_version is not None else self.transform_version
        ticket = {'operation_key': operation_key(record_key, destination, transform_version),
                  'record_key': str(record_key), 'destination': destination,
                  'transform_version': transform_version or ''}
        if payload is not None:
            ticket['payload_sha256'] = _canonical_hash(payload)
        if payload_ref:
            ticket['payload_ref'] = payload_ref
        # Prefer explicit arg; allow node_id= in **fields without dropping it from the ticket.
        if node_id is not None:
            ticket['node_id'] = str(node_id)
        elif fields.get('node_id') is not None:
            ticket['node_id'] = str(fields['node_id'])
        if self.dry_run:
            self._event('write_preview', status='planned', **ticket, **fields)
            return {**ticket, 'dry_run': True}
        claim = _claim_write(destination, ticket)
        if claim == 'received':
            self._event('write_skipped', status='skipped', reason='idempotent', **ticket, **fields)
            return None
        if claim == 'pending':
            self._event('write_blocked', status='blocked', reason='pending receipt', **ticket, **fields)
            raise PendingWrite(
                f"write for {record_key!r} to {destination!r} has a prior intent without a receipt; "
                "reconcile the destination before retrying")
        self._event('write_intent', status='pending', **ticket, **fields)
        return ticket

    def write_receipt(self, ticket: dict, destination_id: object | None = None,
                      verified: bool = False, lineage: dict | None = None,
                      record_table: str | None = None,
                      record_key: object | None = None,
                      outcome: object | None = None,
                      outcome_field: str | None = None,
                      record_fields: dict | None = None,
                      node_id: str | None = None,
                      **fields) -> None:
        """Durably confirm an external write and optionally update its business row.

        ``record_table`` turns the receipt into an in-place dashboard update for
        the original entity. The destination name is the default outcome column;
        use ``outcome_field`` when the API/destination label should differ from
        the operator-facing column name.

        ``node_id`` (or the ticket's ``node_id``) must match the dead_letter
        identity when several nodes share a business key.
        """
        required = {'operation_key', 'record_key', 'destination'}
        if not required.issubset(ticket):
            raise ValueError('write_receipt needs the ticket returned by write_intent')
        if node_id is not None:
            fields = {**fields, 'node_id': str(node_id)}
        elif ticket.get('node_id') is not None and 'node_id' not in fields:
            fields = {**fields, 'node_id': str(ticket['node_id'])}
        dry = bool(ticket.get('dry_run') or self.dry_run)
        if dry:
            # Dry runs must not mint written/verified ledger noise. Keep the
            # planned preview surface and optional business-row projection.
            real_write_outcomes = {
                'written', 'verified', 'appended', 'inserted', 'upserted',
                'pushed', 'created', 'updated', 'synced', 'success', 'succeeded',
            }
            if outcome is None or str(outcome).lower() in real_write_outcomes:
                planned = 'planned'
            else:
                planned = outcome
            payload = dict(fields)

            payload.update(ticket)
            # Explicit receipt node_id wins over a bare ticket field if both set.
            if fields.get('node_id') is not None:
                payload['node_id'] = fields['node_id']
            payload.update({
                'status': 'planned',
                'destination_id': destination_id,
                'dry_run': True,
                'verified': bool(verified),
            })
            if lineage:
                payload['lineage'] = lineage
            self._event('write_preview', **payload)
            if record_table:
                row = dict(record_fields or {})
                row.update({
                    'table': record_table,
                    'key': str(record_key if record_key is not None else ticket['record_key']),
                    str(outcome_field or ticket['destination']): planned,
                    'status': row.get('status') or 'preview',
                    'dry_run': True,
                })
                self._event('record', **row)
            return
        status = 'verified' if verified else 'written'
        receipt = {'ts': _timestamp(), 'state': status,
                   'operation_key': ticket['operation_key'],
                   'record_key': ticket['record_key'], 'destination': ticket['destination'],
                   'destination_id': destination_id, 'attempt': self.attempt}
        _record_receipt(ticket['destination'], receipt)
        payload = dict(fields)
        payload.update(ticket)
        if fields.get('node_id') is not None:
            payload['node_id'] = fields['node_id']
        payload.update({'status': status, 'destination_id': destination_id})
        if lineage:
            payload['lineage'] = lineage
        self._event('write_receipt', **payload)
        self._event('record', table='writes', key=ticket['operation_key'], **payload)
        if record_table:
            row = dict(record_fields or {})
            row.update({
                'table': record_table,
                'key': str(record_key if record_key is not None else ticket['record_key']),
                str(outcome_field or ticket['destination']): outcome if outcome is not None else status,
            })
            self._event('record', **row)

    @staticmethod
    def _replay_identity(event: dict) -> tuple[str, str]:
        """Stable (node, record) key so multi-node runs do not mask each other.

        A write_receipt for the same business key from a different node must not
        clear another node's dead_letter. Node falls back to empty string when
        omitted (legacy single-node scripts).
        """
        node = str(
            event.get('node_id')
            or event.get('node')
            or ''
        )
        record = str(event.get('record_key') or event.get('key') or '')
        return (node, record)

    def replay_candidates(self, all_attempts: bool = True) -> list[dict]:
        """Return failed records that have not later received a matching receipt.

        Matching is by ``(node_id, record_key)``. A later dead_letter for the same
        pair replaces the earlier one; a write_receipt only clears the pair it
        names (legacy receipts without node_id only clear bare-key failures).
        """
        failed: dict[tuple[str, str], dict] = {}
        complete: set[tuple[str, str]] = set()
        for event in _iter_jsonl(_lane_path(self.scope)):
            if not all_attempts and event.get('attempt') != self.attempt:
                continue
            if event.get('event') == 'dead_letter':
                failed[self._replay_identity(event)] = event
            elif event.get('event') == 'write_receipt':
                complete.add(self._replay_identity(event))
        return [event for key, event in failed.items() if key not in complete]

    def reconcile(self, all_attempts: bool = False) -> dict:
        """Count write intent/receipt state from the ledger and make it reviewable."""
        intents, receipts, skipped, blocked = {}, {}, set(), set()
        for event in _iter_jsonl(_lane_path(self.scope)):
            if not all_attempts and event.get('attempt') != self.attempt:
                continue
            op = event.get('operation_key')
            if not op:
                continue
            if event.get('event') == 'write_intent':
                intents[op] = event
            elif event.get('event') == 'write_receipt':
                receipts[op] = event
            elif event.get('event') == 'write_skipped':
                skipped.add(op)
            elif event.get('event') == 'write_blocked':
                blocked.add(op)
        open_intents = set(intents) - set(receipts) - skipped
        result = {'intended': len(intents), 'written': len(receipts),
                  'verified': sum(1 for item in receipts.values() if item.get('status') == 'verified'),
                  'pending': len(open_intents),
                  'skipped': len(skipped), 'blocked': len(blocked),
                  'dead_letters': len(self.replay_candidates(all_attempts=all_attempts))}
        self._event('reconciliation', **result)
        return result


def start_observed_run(name: str, lock_key: str | None = None,
                       dry_run: bool = False, description: str | None = None,
                       source: str | None = None,
                       **fields) -> ObservedRun:
    """Start the boring default contract: lock, run id, ledger, dry-run state."""
    return ObservedRun(name=name, lock_key=lock_key, dry_run=dry_run,
                       description=description, source=source, **fields)




def throttle(resource: str, per_second: float) -> None:
    """Cross-process rate limiter. Blocks until this process may fire one request.

    Coordination is a tiny file per resource holding the next free time slot,
    guarded by flock: each caller atomically claims the next slot (grant =
    max(now, stored)) and advances the file by 1/per_second, then sleeps
    OUTSIDE the flock until its slot arrives. N processes calling
    throttle('some-api', 5) collectively fire at 5/s, FIFO by arrival —
    regardless of which run/table they belong to.

    Use the same `resource` string everywhere the same provider account is hit.
    POSIX only (flock); all coordinating processes must share the state dir.
    """
    if per_second <= 0:
        return
    os.makedirs(_STATE_DIR, exist_ok=True)
    path = _state_path(resource, '.throttle', 'resource')
    interval = 1.0 / per_second
    # Corrupt or clock-skewed throttle files can store a far-future grant and
    # stall every caller. Cap how far ahead a slot may be claimed.
    max_ahead = max(interval * 4.0, 30.0)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64).decode('ascii', 'replace').strip()
        try:
            stored = float(raw)
        except ValueError:
            stored = 0.0
        now = time.time()
        # nan/inf must not disable pacing (nan comparisons never trip the clamp).
        if not math.isfinite(stored) or stored > now + max_ahead:
            stored = now
        grant = max(now, stored)
        if not math.isfinite(grant):
            grant = now
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f'{grant + interval:.6f}'.encode('ascii'))
        # Persist the schedule claim; without fsync a crash resets pacing to 0
        # and recovering processes can burst past per_second.
        os.fsync(fd)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    wait = grant - time.time()
    if wait > 0 and math.isfinite(wait):
        time.sleep(wait)


# ---- inline-dashboard chat (the run_dashboard.py "chat in the cells" inbox) ----
# The dashboard WRITES operator notes into the lane's chat.jsonl; the agent
# PULLS them with post_chat(author='agent'). Delivery is a pull, never a push.
# Project-wide poll presence uses run_id='all' at the state-dir root.
def _chat_path(run_id: object | None = None) -> str:
    return _side_channel_path('chat.jsonl', run_id)


def read_chat(run_id: str | None = None, after_ts: str | None = None,
              author: str | None = None) -> list:
    """Operator notes left in the dashboard, newest last. Filter to one run,
    to messages after a timestamp (to see only what's new since you last read),
    and/or by author ('user' for operator notes you haven't answered yet)."""
    out = []
    lane = _lane_from_run_id(run_id) if run_id else ''
    lane_chat = os.path.abspath(_chat_path(run_id)) if lane else ''
    for path in _side_channel_read_paths('chat.jsonl', run_id):
        if not os.path.exists(path):
            continue
        path_abs = os.path.abspath(path)
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        m = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tagged = m.get('run')
                    if run_id and run_id != 'all' and tagged and tagged != run_id:
                        continue
                    if run_id and run_id != 'all' and not tagged and path_abs != lane_chat:
                        continue
                    if after_ts and _ts_order_key(m.get('ts')) <= _ts_order_key(after_ts):
                        continue
                    if author and m.get('author') != author:
                        continue
                    out.append(m)
        except OSError:
            continue
    return out


def post_chat(run_id: str, anchor: str, text: str, author: str = 'agent',
              resolved: bool = False) -> None:
    """Reply into the dashboard thread (shows under the same column/cell). The
    agent uses this to answer an operator note; `anchor` must match the note's.
    Pass resolved=True when the note is handled — the cell's badge flips to a ✓."""
    path = _chat_path(run_id)
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    if _lane_from_run_id(run_id):
        _ensure_lane_explain(_lane_from_run_id(run_id))
    rec = {'ts': _timestamp(), 'run': run_id,
           'anchor': anchor, 'author': author, 'text': text, 'resolved': bool(resolved)}
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')


def wait_for_feedback(run_id: str, timeout: float = 600, poll: float = 2.0,
                      since_ts: str | None = None) -> list:
    """Block until the operator leaves at least one new note for this run in the
    dashboard, or until timeout. Returns the new user messages (empty on timeout).

    This is the AXI-style review gate: run a SMALL SAMPLE, call this so the operator
    can inspect the sample in the dashboard and leave notes on cells/columns, then
    adapt and run the full list. ``since_ts`` defaults to the instant before the
    wait begins; notes must carry a strictly later timestamp (nanosecond stamps)
    so a same-second reply is not dropped.
    """
    if since_ts is None:
        since_ts = _timestamp()
    deadline = time.time() + timeout
    while time.time() < deadline:
        msgs = read_chat(run_id, after_ts=since_ts, author='user')
        if msgs:
            return msgs
        time.sleep(poll)
    return []
