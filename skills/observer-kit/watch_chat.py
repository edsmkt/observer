#!/usr/bin/env python3
"""Run-scoped chat watcher — routes dashboard notes to the RIGHT agent session.

The dashboard writes every operator note into ONE shared `chat.jsonl`, each tagged
with the `run` it's about. With several agent sessions open, an unscoped watcher would
wake all of them on every note. This watcher only surfaces notes for ONE run, so the
session that launched that run is the only one that acts on them.

Harness-agnostic: it just prints new user notes (as JSON lines) and exits — wire it
into whatever your harness uses to wake an idle agent.
  - Claude Code: point the Monitor tool at `python3 watch_chat.py <run_id>`; each time
    it prints + exits, the harness re-invokes you with the note. (It's already scoped,
    so other sessions' runs never wake you.)
  - Anything else: run it in a loop, or call runguard.read_chat(run_id) yourself.

The run_id is what runguard.current_run_id(scope) returns, e.g.
'runguard:2025-06-15-enrich-companies.jsonl' — the same value the dashboard tags notes with.

Usage:
  python3 watch_chat.py <run_id> [--state-dir DIR] [--since TS] [--poll SEC]
                                 [--follow] [--timeout SEC]
Defaults: state dir from $RUNGUARD_STATE_DIR (else ./.runguard); notes after start time;
blocks until the first new note for this run, prints it, exits 0 (re-invoke loop).
--follow keeps streaming; --timeout N exits 0 after N seconds even with nothing new.
"""
import os
import sys
import json
import time
import argparse


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


def new_notes(chat_path, run_id, after_ts):
    """Operator (author='user') notes for THIS run, left after `after_ts`."""
    return [m for m in _load(chat_path)
            if m.get('author') == 'user'
            and m.get('run') == run_id
            and (m.get('ts') or '') > after_ts]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('run_id', help="only notes for THIS run wake the watcher (multi-session safe)")
    ap.add_argument('--state-dir', default=os.environ.get('RUNGUARD_STATE_DIR') or '.runguard')
    ap.add_argument('--since', default=None, help="ISO ts; default = now (only notes left after start)")
    ap.add_argument('--poll', type=float, default=2.0)
    ap.add_argument('--follow', action='store_true', help="keep streaming instead of exiting on the first batch")
    ap.add_argument('--timeout', type=float, default=0, help="0 = wait forever")
    a = ap.parse_args()

    chat_path = os.path.join(a.state_dir, 'chat.jsonl')
    since = a.since or time.strftime('%Y-%m-%dT%H:%M:%S')
    deadline = (time.time() + a.timeout) if a.timeout else None

    while True:
        notes = new_notes(chat_path, a.run_id, since)
        if notes:
            for m in notes:
                print(json.dumps(m, ensure_ascii=False))
                since = m.get('ts') or since
            sys.stdout.flush()
            if not a.follow:
                return 0
        if deadline and time.time() > deadline:
            return 0
        time.sleep(a.poll)


if __name__ == '__main__':
    sys.exit(main())
