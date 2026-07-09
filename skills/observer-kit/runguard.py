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

State dir: $RUNGUARD_STATE_DIR, else ./.runguard next to this file. All
processes that should coordinate must use the SAME state dir.
"""
from __future__ import annotations

import atexit
import hashlib
import fcntl
import json
import os
import re
import sys
import time

_STATE_DIR = os.environ.get('RUNGUARD_STATE_DIR') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '.runguard')

_held: dict[str, tuple[str, int]] = {}  # name -> (persistent lockfile path, fd)
_ledgers: dict[str, str] = {}
_step_sequences: dict[str, int] = {}
_SAFE_COMPONENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


class RunPaused(RuntimeError):
    """A deliberate safety pause, not a failed attempt.

    Let this escape the work loop. Do not catch it and call ``run.fail()``: the
    ledger already has an explicit ``run_paused`` terminal event.
    """


class PendingWrite(RuntimeError):
    """A prior write intent has no receipt, so a duplicate write is unsafe."""


def _timestamp() -> str:
    """UTC RFC 3339 timestamp understood consistently by every dashboard."""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


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


def _lane_path(scope: str) -> str:
    """Return the continuous ledger path for a scope in the selected lane."""
    if scope not in _ledgers:
        os.makedirs(_STATE_DIR, exist_ok=True)
        session = os.environ.get('RUNGUARD_SESSION')
        scope_name = _safe_component(scope, 'scope')
        session_name = _safe_component(session, 'session') if session else ''
        name = f"{session_name}-{scope_name}.jsonl" if session_name else f"{scope_name}.jsonl"
        _ledgers[scope] = os.path.join(_STATE_DIR, name)
    return _ledgers[scope]


def _append_jsonl(path: str, record: dict) -> None:
    """Append one complete JSON value. O_APPEND keeps concurrent small writes whole."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    raw = (json.dumps(record, ensure_ascii=False, default=str, sort_keys=True) + '\n').encode('utf-8')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
    finally:
        os.close(fd)


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
    result = {'source': os.path.realpath(identity) if os.path.exists(identity) else identity}
    if version is not None:
        result['version'] = str(version)
    if records is None and os.path.isfile(identity):
        try:
            stat = os.stat(identity)
            result['bytes'] = stat.st_size
            digest = hashlib.sha256()
            rows = 0
            with open(identity, 'rb') as fh:
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


def _control_path() -> str:
    return os.path.join(_STATE_DIR, 'controls.jsonl')


def post_control(run_id: str, kind: str, note: str = '') -> dict:
    """Durably request a run action. The script/harness remains the decision-maker."""
    if kind not in {'pause', 'stop_after_record', 'approve_full_run'}:
        raise ValueError(f'unsupported control request: {kind}')
    rec = {'id': _canonical_hash([run_id, kind, note, time.time_ns(), os.getpid()])[:20],
           'ts': _timestamp(), 'run': str(run_id), 'kind': kind, 'note': str(note)[:1000]}
    _append_jsonl(_control_path(), rec)
    return rec


def read_controls(run_id: str | None = None) -> list:
    """Read durable operator control requests, newest last."""
    return [rec for rec in _iter_jsonl(_control_path())
            if not run_id or rec.get('run') == run_id]


def source_scope(workflow: str, source: str) -> str:
    """Stable lock scope from the real source identity, not a run nickname.

    Pass a resolved CSV path, sheet ID, table ID, or another immutable source
    identifier. Two invocations with the same source get the same scope; a
    separate source gets a different scope and can run in parallel when it is
    provably disjoint.
    """
    raw = str(source or '').strip()
    if not raw:
        raise ValueError('source must be a real source identity, not an empty label')
    identity = os.path.realpath(raw) if os.path.exists(raw) else raw
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    return f'{_safe_component(workflow, "workflow")}-source-{digest}'


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
    """Exclusive per-scope advisory lock. Refuse while another process holds it."""
    if name in _held:
        return  # re-entrant within this process
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
    _held[name] = (path, fd)
    atexit.register(release_lock, name)


def release_lock(name: str) -> None:
    held = _held.pop(name, None)
    if not held:
        return
    _path, fd = held
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
    fresh weekly run, or a unique label for a clean A/B) → '<slug>-<scope>.jsonl'."""
    path = _lane_path(scope)
    rec = {'ts': _timestamp(), 'event': event}
    rec.update(fields)
    _append_jsonl(path, rec)
    if event == 'run_started':
        # Marker a harness hook can match to remind the agent to start this run's
        # watcher (so operator dashboard notes reach THIS session). Cheap + universal:
        # any run that logs run_started emits it, whether or not start_run() is used.
        rid = f'runguard:{os.path.basename(path)}'
        sys.stderr.write(
            f"OBSERVER_RUN_STARTED {rid}\n"
            f"[observer] start this run's chat watcher to receive operator notes:\n"
            f"           python3 watch_chat.py {rid} --state-dir {_STATE_DIR}\n")


def ledger_path(scope: str) -> str | None:
    return _ledgers.get(scope)


def current_run_id(scope: str) -> str | None:
    """The dashboard run id for this scope's ledger ('runguard:<file>'). Pass it to
    read_chat/post_chat so chat lands on the same run the dashboard is showing.
    With RUNGUARD_SESSION pinned this stays stable across re-runs, so notes persist."""
    p = _ledgers.get(scope)
    return f'runguard:{os.path.basename(p)}' if p else None


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
    """Stable idempotency key for one record, sink, and transform revision."""
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
        self.lock_key = self.scope
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
        self._seen_unique: set[str] = set()
        self._seen_controls: set[str] = set()
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
        # acknowledgements prevent a recovered/retried process from honoring an
        # already-applied pause or approval again.
        self._seen_controls = {
            str(event['control_id']) for event in _iter_jsonl(_lane_path(self.scope))
            if event.get('event') == 'control_acknowledged' and event.get('control_id')
        }
        self.manifest(snapshot=self.input_snapshot, destination=destination,
                      transform_version=transform_version, policy_version=policy_version,
                      script=script, config=config)
        atexit.register(self._abandon_if_open)

    def _event(self, event: str, **fields) -> None:
        ledger(self.scope, event, attempt=self.attempt, **fields)

    def _abandon_if_open(self) -> None:
        if self.closed:
            return
        self._event('run_abandoned', status='failed', dry_run=self.dry_run,
                    error='process exited before run.success() or run.fail()',
                    **self.counters)
        release_lock(self.lock_key)
        self.closed = True

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

    def success(self, **fields) -> None:
        if self.closed:
            return
        payload = dict(fields)
        payload.update(self.counters)
        if self.checkpoints:
            payload['checkpoints'] = dict(self.checkpoints)
        self._event('run_finished', status='success', dry_run=self.dry_run, **payload)
        release_lock(self.lock_key)
        self.closed = True

    def fail(self, error: BaseException | str, **fields) -> None:
        if self.closed:
            return
        payload = dict(fields)
        payload.update(self.counters)
        if self.checkpoints:
            payload['checkpoints'] = dict(self.checkpoints)
        self._event('run_failed', status='failed', error=str(error),
                    dry_run=self.dry_run, **payload)
        release_lock(self.lock_key)
        self.closed = True

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

    def validate(self, record: dict, key: object, contract: dict,
                 table: str = 'records', on_error: str = 'pause') -> bool:
        """Check a transformed record before a write; pause on schema drift by default."""
        errors = schema_errors(record, contract)
        new_unique = []
        for field in contract.get('unique', []):
            value = record.get(field)
            marker = _canonical_hash([table, field, value])
            if value not in (None, '') and marker in self._seen_unique:
                errors.append(f'duplicate unique field: {field}')
            elif value not in (None, ''):
                new_unique.append(marker)
        if not errors:
            self._seen_unique.update(new_unique)
            return True
        self._event('schema_violation', status='failed', key=str(key), table=table,
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
            self.closed = True
        raise RunPaused(str(reason))

    def dead_letter(self, record_key: object, error: object, retry: int = 0,
                    table: str = 'dead_letters', payload_ref: str | None = None,
                    **fields) -> None:
        """Keep a failed record replayable without logging its full sensitive payload."""
        payload = dict(fields)
        payload.update({'record_key': str(record_key), 'error': str(error), 'retry': retry})
        if payload_ref:
            payload['payload_ref'] = payload_ref
        self._event('dead_letter', status='failed', **payload)
        self._event('record', table='dead_letters', key=str(record_key), status='failed', **payload)

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
        remembered until a check made with ``after_record=True``. Approval is
        returned to the harness/script; it never starts a full run by itself.
        """
        fresh = []
        for control in read_controls(self.run_id):
            control_id = str(control.get('id') or _canonical_hash(control))
            if control_id in self._seen_controls:
                continue
            self._seen_controls.add(control_id)
            fresh.append(control)
            self._event('control_acknowledged', control_id=control_id,
                        control=control.get('kind'), note=control.get('note', ''))
            if control.get('kind') == 'pause':
                self.pause('operator requested pause')
            if control.get('kind') == 'stop_after_record':
                self.stop_requested = True
        if after_record and self.stop_requested:
            self.pause('operator requested stop after current record')
        return fresh

    def write_intent(self, record_key: object, destination: str | None = None,
                     transform_version: str | None = None, payload: object | None = None,
                     payload_ref: str | None = None, **fields) -> dict | None:
        """Reserve one external write before calling a sink.

        A returned ticket is the sink idempotency key. Pass it to the provider
        when supported, then call ``write_receipt`` only after the provider or
        destination has confirmed the write. A prior pending ticket raises
        ``PendingWrite`` instead of guessing whether an interrupted call landed.
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
                      **fields) -> None:
        """Durably confirm an external write and optionally update its business row.

        ``record_table`` turns the receipt into an in-place dashboard update for
        the original entity. The destination name is the default outcome column;
        use ``outcome_field`` when the API/destination label should differ from
        the operator-facing column name.
        """
        required = {'operation_key', 'record_key', 'destination'}
        if not required.issubset(ticket):
            raise ValueError('write_receipt needs the ticket returned by write_intent')
        status = 'verified' if verified else 'written'
        receipt = {'ts': _timestamp(), 'state': status,
                   'operation_key': ticket['operation_key'],
                   'record_key': ticket['record_key'], 'destination': ticket['destination'],
                   'destination_id': destination_id, 'attempt': self.attempt}
        if not ticket.get('dry_run'):
            _record_receipt(ticket['destination'], receipt)
        payload = dict(fields)
        payload.update(ticket)
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

    def replay_candidates(self, all_attempts: bool = True) -> list[dict]:
        """Return failed records that have not later received a write receipt."""
        failed, complete = {}, set()
        for event in _iter_jsonl(_lane_path(self.scope)):
            if not all_attempts and event.get('attempt') != self.attempt:
                continue
            if event.get('event') == 'dead_letter':
                failed[str(event.get('record_key'))] = event
            elif event.get('event') == 'write_receipt':
                complete.add(str(event.get('record_key')))
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
        result = {'intended': len(intents), 'written': len(receipts),
                  'verified': sum(1 for item in receipts.values() if item.get('status') == 'verified'),
                  'pending': len(set(intents) - set(receipts)),
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
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64).decode('ascii', 'replace').strip()
        try:
            stored = float(raw)
        except ValueError:
            stored = 0.0
        grant = max(time.time(), stored)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f'{grant + interval:.6f}'.encode('ascii'))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    wait = grant - time.time()
    if wait > 0:
        time.sleep(wait)


# ---- inline-dashboard chat (the run_dashboard.py "chat in the cells" inbox) ----
# The dashboard WRITES operator notes here (anchored to a column/cell); the agent
# PULLS them to receive feedback and replies with post_chat(author='agent').
# Delivery is a pull, never a push: read at the start of your next turn, or poll
# between rounds of a long run for a stop/adjust signal. Same _STATE_DIR as the
# dashboard's SOURCES['runguard'] — all coordinating processes must share it.
def _chat_path() -> str:
    return os.path.join(_STATE_DIR, 'chat.jsonl')


def read_chat(run_id: str | None = None, after_ts: str | None = None,
              author: str | None = None) -> list:
    """Operator notes left in the dashboard, newest last. Filter to one run,
    to messages after a timestamp (to see only what's new since you last read),
    and/or by author ('user' for operator notes you haven't answered yet)."""
    path = _chat_path()
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if run_id and m.get('run') != run_id:
                continue
            if after_ts and (m.get('ts') or '') <= after_ts:
                continue
            if author and m.get('author') != author:
                continue
            out.append(m)
    return out


def post_chat(run_id: str, anchor: str, text: str, author: str = 'agent',
              resolved: bool = False) -> None:
    """Reply into the dashboard thread (shows under the same column/cell). The
    agent uses this to answer an operator note; `anchor` must match the note's.
    Pass resolved=True when the note is handled — the cell's badge flips to a ✓."""
    os.makedirs(_STATE_DIR, exist_ok=True)
    rec = {'ts': _timestamp(), 'run': run_id,
           'anchor': anchor, 'author': author, 'text': text, 'resolved': bool(resolved)}
    with open(_chat_path(), 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')


def wait_for_feedback(run_id: str, timeout: float = 600, poll: float = 2.0,
                      since_ts: str | None = None) -> list:
    """Block until the operator leaves at least one new note for this run in the
    dashboard, or until timeout. Returns the new user messages (empty on timeout).

    This is the AXI-style review gate: run a SMALL SAMPLE, call this so the operator
    can inspect the sample in the dashboard and leave notes on cells/columns, then
    adapt and run the full list. `since_ts` defaults to now, so only notes left
    after the call count."""
    if since_ts is None:
        since_ts = _timestamp()
    deadline = time.time() + timeout
    while time.time() < deadline:
        msgs = read_chat(run_id, after_ts=since_ts, author='user')
        if msgs:
            return msgs
        time.sleep(poll)
    return []
