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

State dir: $RUNGUARD_STATE_DIR, else ./.runguard next to this file. All
processes that should coordinate must use the SAME state dir.
Override for deliberate parallel use of one scope (rare): RUNGUARD_DISABLE=1.
"""
from __future__ import annotations

import atexit
import fcntl
import json
import os
import time

_STATE_DIR = os.environ.get('RUNGUARD_STATE_DIR') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '.runguard')

_held: dict[str, str] = {}   # name -> lockfile path (this process)
_ledgers: dict[str, str] = {}


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
    """Append one audit record to this run's JSONL ledger for the given scope."""
    if scope not in _ledgers:
        os.makedirs(_STATE_DIR, exist_ok=True)
        _ledgers[scope] = os.path.join(
            _STATE_DIR, f"{time.strftime('%Y-%m-%d-%H%M%S')}-{scope}.jsonl")
    rec = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'event': event}
    rec.update(fields)
    with open(_ledgers[scope], 'a') as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')


def ledger_path(scope: str) -> str | None:
    return _ledgers.get(scope)


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
