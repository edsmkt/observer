#!/usr/bin/env python3
"""Chat watcher that routes dashboard notes to the owning agent session.

The dashboard writes operator notes into each lane's `runs/<lane>/chat.jsonl`
(legacy root `chat.jsonl` is still read). With several agent sessions open, an
unscoped watcher would wake all of them on every note. This watcher only surfaces
notes for ONE run, so the session that launched that run is the only one that acts.

Harness-agnostic: it just prints new user notes (as JSON lines) and exits — wire it
into whatever your harness uses to wake an idle agent.
  - Claude Code: point the Monitor tool at `python3 watch_chat.py <run_id>`; each time
    it prints + exits, the harness re-invokes you with the note. (Already scoped, so
    other sessions' runs never wake you.)
  - Anything else: run it in a loop, or call runguard.read_chat(run_id) yourself.
  - runguard.start_run(scope) spawns this with --follow, funnelling a run's notes into
    <state>/<run>.inbox.jsonl the moment the run starts.

The run_id is what runguard.current_run_id(scope) returns, e.g.
'runguard:2025-06-15-enrich' — the same value the dashboard tags notes with.
Notes for that run live under ``runs/2025-06-15-enrich/chat.jsonl``.

By default only notes that arrive AFTER the watcher starts are surfaced (pre-existing
notes are marked seen); pass --include-existing to also emit ones already in the file.
Dedup is by message content, not timestamp, so a note posted in the same second the
watcher starts is not lost.

Usage:
  python3 watch_chat.py <run_id> [--state-dir DIR] [--poll SEC]
                                 [--follow] [--timeout SEC] [--include-existing]
  python3 watch_chat.py --all [--state-dir DIR] [--follow]
  python3 watch_chat.py <run_id> --reply "text" [--anchor ANCHOR] [--resolved]
                                 [--state-dir DIR]
"""
import fcntl
import hashlib
import os
import sys
import json
import time
import argparse

from observer_kit._util import pid_alive as _pid_alive, timestamp as _timestamp


WATCH_PREFIX = '.observer-watcher-'
REGISTRY_LOCK = '.observer-watchers.registry.lock'


def _load(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _sig(m):
    """Stable identity of a note — timestamp-independent, so same-second notes aren't lost."""
    return json.dumps([m.get('ts'), m.get('run'), m.get('anchor'), m.get('text')],
                      ensure_ascii=False, sort_keys=True)


def _matches(m, run_id, all_runs=False):
    wakes = m.get('author') == 'user' or m.get('kind') == 'control'
    return wakes and (all_runs or m.get('run') == run_id)


def _load_one(path):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}


def _active_watchers(state_dir):
    watchers = []
    try:
        names = os.listdir(state_dir)
    except OSError:
        return watchers
    for name in names:
        if not (name.startswith(WATCH_PREFIX) and name.endswith('.lock')):
            continue
        meta = _load_one(os.path.join(state_dir, name))
        if meta.get('active') and _pid_alive(meta.get('pid')):
            watchers.append(meta)
    return watchers


def _watch_key(run_id, all_runs):
    return 'all' if all_runs else f'run:{run_id}'


def _watch_path(state_dir, key):
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]
    return os.path.join(state_dir, f'{WATCH_PREFIX}{digest}.lock')


def _write_meta(fd, meta):
    payload = json.dumps(meta, ensure_ascii=False, sort_keys=True).encode('utf-8')
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, payload)
    os.fsync(fd)


def _conflicting_watcher(active, key):
    for watcher in active:
        other = watcher.get('key')
        if key == 'all' or other == 'all' or other == key:
            return watcher
    return None


def _acquire_watcher(state_dir, run_id, all_runs, parent_pid):
    os.makedirs(state_dir, exist_ok=True)
    key = _watch_key(run_id, all_runs)
    registry_path = os.path.join(state_dir, REGISTRY_LOCK)
    registry_fd = os.open(registry_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(registry_fd, fcntl.LOCK_EX)
    try:
        conflict = _conflicting_watcher(_active_watchers(state_dir), key)
        if conflict:
            label = 'all runs' if conflict.get('key') == 'all' else conflict.get('run')
            print(f"WATCHER ALREADY ACTIVE: {label} (pid {conflict.get('pid')}). Reuse it.",
                  file=sys.stderr)
            return None

        path = _watch_path(state_dir, key)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        meta = {
            'active': True,
            'key': key,
            'mode': 'all' if all_runs else 'run',
            'run': None if all_runs else run_id,
            'pid': os.getpid(),
            'parent_pid': parent_pid,
            'started': _timestamp(),
            'state_dir': os.path.abspath(state_dir),
        }
        _write_meta(fd, meta)
        return fd, meta
    finally:
        fcntl.flock(registry_fd, fcntl.LOCK_UN)
        os.close(registry_fd)


def _release_watcher(lease):
    if not lease:
        return
    fd, meta = lease
    meta = dict(meta, active=False, stopped=_timestamp())
    try:
        _write_meta(fd, meta)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('run_id', nargs='?', help="only notes for THIS run wake the watcher")
    ap.add_argument('--all', dest='all_runs', action='store_true',
                    help="bridge every run for one long-lived project session")
    ap.add_argument('--state-dir', default=os.environ.get('RUNGUARD_STATE_DIR') or '.observer')
    ap.add_argument('--poll', type=float, default=2.0)
    ap.add_argument('--follow', action='store_true', help="keep streaming instead of exiting on the first batch")
    ap.add_argument('--timeout', type=float, default=0, help="0 = wait forever")
    ap.add_argument('--include-existing', action='store_true',
                    help="also emit notes already in the file at startup (default: only new)")
    ap.add_argument('--reply', help="post an agent reply to chat.jsonl and exit")
    ap.add_argument('--anchor', default='run', help="dashboard anchor/cell id (used with --reply)")
    ap.add_argument('--resolved', action='store_true', help="mark the reply as resolved (used with --reply)")
    ap.add_argument('--parent-pid', type=int,
                    help="exit when the owning observer-kit process exits")
    a = ap.parse_args()

    if a.all_runs and a.run_id:
        ap.error('choose a run_id or --all')
    if not a.all_runs and not a.run_id:
        ap.error('run_id is required unless --all is set')
    if a.reply and a.all_runs:
        ap.error('--reply requires a run_id')

    a.state_dir = os.path.abspath(os.path.expanduser(a.state_dir))

    def _lane_name(run_id):
        raw = str(run_id or '')
        if ':' in raw:
            raw = raw.split(':', 1)[1]
        raw = os.path.basename(raw)
        if raw.endswith('.jsonl'):
            raw = raw[:-6]
        return raw

    def _chat_write_path(run_id):
        lane = _lane_name(run_id)
        if lane and run_id != 'all':
            return os.path.join(a.state_dir, 'runs', lane, 'chat.jsonl')
        return os.path.join(a.state_dir, 'chat.jsonl')

    def _chat_read_paths():
        paths = []
        if a.all_runs:
            root = os.path.join(a.state_dir, 'chat.jsonl')
            paths.append(root)
            runs_root = os.path.join(a.state_dir, 'runs')
            if os.path.isdir(runs_root):
                for name in sorted(os.listdir(runs_root)):
                    paths.append(os.path.join(runs_root, name, 'chat.jsonl'))
        else:
            paths.append(_chat_write_path(a.run_id))
            paths.append(os.path.join(a.state_dir, 'chat.jsonl'))
        seen = set()
        ordered = []
        for path in paths:
            key = os.path.abspath(path)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(path)
        return ordered

    def _load_all():
        messages = []
        for path in _chat_read_paths():
            messages.extend(_load(path))
        return messages

    # Reply mode: write one agent reply and exit (no poll).
    if a.reply:
        chat_path = _chat_write_path(a.run_id)
        os.makedirs(os.path.dirname(chat_path) or a.state_dir, exist_ok=True)
        rec = {
            "ts": _timestamp(),
            "run": a.run_id,
            "anchor": a.anchor,
            "author": "agent",
            "text": a.reply,
            "resolved": bool(a.resolved),
        }
        with open(chat_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')
        return 0

    lease = _acquire_watcher(a.state_dir, a.run_id, a.all_runs, a.parent_pid) if a.follow else None
    if a.follow and lease is None:
        return 3

    # Poll mode: watch for new user notes.
    seen = set()
    if not a.include_existing:                      # ignore notes left before we started
        for m in _load_all():
            if _matches(m, a.run_id, a.all_runs):
                seen.add(_sig(m))
    deadline = (time.time() + a.timeout) if a.timeout else None

    try:
        while True:
            if a.parent_pid and not _pid_alive(a.parent_pid):
                return 0
            fresh = []
            for m in _load_all():
                if _matches(m, a.run_id, a.all_runs):
                    s = _sig(m)
                    if s not in seen:
                        seen.add(s)
                        fresh.append(m)
            if fresh:
                for m in fresh:
                    print(json.dumps(m, ensure_ascii=False))
                sys.stdout.flush()
                if not a.follow:
                    return 0
            if deadline and time.time() > deadline:
                return 0
            time.sleep(a.poll)
    finally:
        _release_watcher(lease)


if __name__ == '__main__':
    sys.exit(main())
