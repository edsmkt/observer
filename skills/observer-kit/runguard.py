"""Run-exclusivity locks + local run ledgers for spending/mutating batch scripts.

Prevents a whole class of batch-job failures: a process nobody realizes is still
running gets a second start, the two double-spend or corrupt shared state, and a
panicked "cleanup" makes it worse. Two primitives:

  acquire_lock(scope) — PID lockfile per resource scope. A second process on the
                        same scope HARD-REFUSES while the first is alive (SystemExit).
                        Same-PID re-acquire is a no-op (re-entrant). A lock whose
                        PID is dead is stale and taken over silently — crash
                        recovery is "just re-run", never "clean up".
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
Override for deliberate parallel use of one scope (rare): RUNGUARD_DISABLE=1.
"""
from __future__ import annotations

import atexit
import fcntl
import json
import os
import sys
import time

_STATE_DIR = os.environ.get('RUNGUARD_STATE_DIR') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '.runguard')

_held: dict[str, str] = {}   # name -> lockfile path (this process)
_ledgers: dict[str, str] = {}
_step_sequences: dict[str, int] = {}


def _lockfile(name: str) -> str:
    os.makedirs(_STATE_DIR, exist_ok=True)
    return os.path.join(_STATE_DIR, f'{name}.lock')


def acquire_lock(name: str) -> None:
    """Exclusive per-scope run lock. SystemExit(1) if another live process holds it."""
    if os.environ.get('RUNGUARD_DISABLE') == '1':
        return
    if name in _held:
        return  # re-entrant within this process
    path = _lockfile(name)
    if os.path.exists(path):
        try:
            lock = json.load(open(path))
            pid = int(lock.get('pid', -1))
            if pid != os.getpid():
                os.kill(pid, 0)  # raises if dead
                raise SystemExit(
                    f"REFUSING TO START: another '{name}' run is live "
                    f"(pid {pid}, started {lock.get('started')}).\n"
                    f"If it is genuinely hung, stop it deliberately first: kill {pid}\n"
                    f"Never start a parallel run to 'fix' a stuck one — that is exactly how "
                    f"double-charges and corrupted state happen.")
        except (ProcessLookupError, PermissionError, ValueError, json.JSONDecodeError):
            pass  # stale (dead pid / unreadable) — take over; re-run is always safe
    json.dump({'pid': os.getpid(), 'started': time.strftime('%Y-%m-%dT%H:%M:%S'),
               'scope': name}, open(path, 'w'))
    _held[name] = path
    atexit.register(release_lock, name)


def release_lock(name: str) -> None:
    path = _held.pop(name, None)
    if path and os.path.exists(path):
        try:
            lock = json.load(open(path))
            if int(lock.get('pid', -1)) == os.getpid():
                os.remove(path)
        except Exception:
            pass


def ledger(scope: str, event: str, **fields) -> None:
    """Append one audit record to this run's JSONL ledger for the given scope.

    Runs over the SAME source share ONE continuous run by default: the ledger is
    named for the scope (which should encode the dataset identity, e.g.
    'enrich-prospects-csv'), so re-running the same source keeps appending to the
    same run — the dashboard shows the iterations in one table with before/after
    "· was X", and chat notes / ✓ persist across re-runs.

    Set RUNGUARD_SESSION=<slug> only to open a SEPARATE lane (a dated slug for a
    fresh weekly run, or a unique label for a clean A/B) → '<slug>-<scope>.jsonl'."""
    if scope not in _ledgers:
        os.makedirs(_STATE_DIR, exist_ok=True)
        session = os.environ.get('RUNGUARD_SESSION')
        name = f"{session}-{scope}.jsonl" if session else f"{scope}.jsonl"
        _ledgers[scope] = os.path.join(_STATE_DIR, name)
    rec = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'event': event}
    rec.update(fields)
    with open(_ledgers[scope], 'a') as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')
    if event == 'run_started':
        # Marker a harness hook can match to remind the agent to start this run's
        # watcher (so operator dashboard notes reach THIS session). Cheap + universal:
        # any run that logs run_started emits it, whether or not start_run() is used.
        rid = f'runguard:{os.path.basename(_ledgers[scope])}'
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
               **self.fields)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            ledger(self.run.scope, 'record', table=self.table, key=self.key,
                   step=self.name, status='done', dry_run=self.run.dry_run,
                   **self.fields)
            return False
        ledger(self.run.scope, 'record', table=self.table, key=self.key,
               step=self.name, status='failed', error=str(exc),
               dry_run=self.run.dry_run, **self.fields)
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
                 **fields):
        self.name = name
        self.scope = lock_key or name
        self.lock_key = self.scope
        self.dry_run = bool(dry_run)
        self.description = description
        self.counters: dict[str, int | float] = {}
        self.checkpoints: dict[str, object] = {}
        self.closed = False
        acquire_lock(self.lock_key)
        started = dict(fields)
        started.update({'name': self.name, 'dry_run': self.dry_run})
        if description:
            started['description'] = description
        ledger(self.scope, 'run_started', **started)
        self.run_id = current_run_id(self.scope)

    def step(self, name: str, **fields) -> ObservedStep:
        """Log one visible unit of work as a generic dashboard record."""
        return ObservedStep(self, name, fields)

    def count(self, name: str, amount: int | float = 1) -> int | float:
        """Increment an in-memory counter and emit a lightweight metric event."""
        self.counters[name] = self.counters.get(name, 0) + amount
        ledger(self.scope, 'metric', metric=name, value=self.counters[name],
               increment=amount)
        return self.counters[name]

    def checkpoint(self, name: str, value) -> None:
        """Record the last durable point the script can resume from."""
        self.checkpoints[name] = value
        ledger(self.scope, 'checkpoint', checkpoint=name, value=value)

    def success(self, **fields) -> None:
        if self.closed:
            return
        payload = dict(fields)
        payload.update(self.counters)
        if self.checkpoints:
            payload['checkpoints'] = dict(self.checkpoints)
        ledger(self.scope, 'run_finished', status='success',
               dry_run=self.dry_run, **payload)
        release_lock(self.lock_key)
        self.closed = True

    def fail(self, error: BaseException | str, **fields) -> None:
        if self.closed:
            return
        payload = dict(fields)
        payload.update(self.counters)
        if self.checkpoints:
            payload['checkpoints'] = dict(self.checkpoints)
        ledger(self.scope, 'run_failed', status='failed', error=str(error),
               dry_run=self.dry_run, **payload)
        release_lock(self.lock_key)
        self.closed = True


def start_observed_run(name: str, lock_key: str | None = None,
                       dry_run: bool = False, description: str | None = None,
                       **fields) -> ObservedRun:
    """Start the boring default contract: lock, run id, ledger, dry-run state."""
    return ObservedRun(name=name, lock_key=lock_key, dry_run=dry_run,
                       description=description, **fields)




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
    path = os.path.join(_STATE_DIR, f'{resource}.throttle')
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
    rec = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'run': run_id,
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
        since_ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    deadline = time.time() + timeout
    while time.time() < deadline:
        msgs = read_chat(run_id, after_ts=since_ts, author='user')
        if msgs:
            return msgs
        time.sleep(poll)
    return []
