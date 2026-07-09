#!/usr/bin/env python3
"""Run-scoped chat watcher — routes dashboard notes to the RIGHT agent session.

The dashboard writes every operator note into ONE shared `chat.jsonl`, each tagged
with the `run` it's about. With several agent sessions open, an unscoped watcher would
wake all of them on every note. This watcher only surfaces notes for ONE run, so the
session that launched that run is the only one that acts on them.

Harness-agnostic: it just prints new user notes (as JSON lines) and exits — wire it
into whatever your harness uses to wake an idle agent.
  - Claude Code: point the Monitor tool at `python3 watch_chat.py <run_id>`; each time
    it prints + exits, the harness re-invokes you with the note. (Already scoped, so
    other sessions' runs never wake you.)
  - Anything else: run it in a loop, or call runguard.read_chat(run_id) yourself.
  - runguard.start_run(scope) spawns this with --follow, funnelling a run's notes into
    <state>/<run>.inbox.jsonl the moment the run starts.

The run_id is what runguard.current_run_id(scope) returns, e.g.
'runguard:2025-06-15-enrich.jsonl' — the same value the dashboard tags notes with.

By default only notes that arrive AFTER the watcher starts are surfaced (pre-existing
notes are marked seen); pass --include-existing to also emit ones already in the file.
Dedup is by message content, not timestamp, so a note posted in the same second the
watcher starts is not lost.

Usage:
  python3 watch_chat.py <run_id> [--state-dir DIR] [--poll SEC]
                                 [--follow] [--timeout SEC] [--include-existing]
  python3 watch_chat.py <run_id> --reply "text" [--anchor ANCHOR] [--resolved]
                                 [--state-dir DIR]
"""
import os
import sys
import json
import time
import argparse


def _timestamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


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


def _matches(m, run_id):
    return (m.get('author') == 'user' or m.get('kind') == 'control') and m.get('run') == run_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('run_id', help="only notes for THIS run wake the watcher (multi-session safe)")
    ap.add_argument('--state-dir', default=os.environ.get('RUNGUARD_STATE_DIR') or '.runguard')
    ap.add_argument('--poll', type=float, default=2.0)
    ap.add_argument('--follow', action='store_true', help="keep streaming instead of exiting on the first batch")
    ap.add_argument('--timeout', type=float, default=0, help="0 = wait forever")
    ap.add_argument('--include-existing', action='store_true',
                    help="also emit notes already in the file at startup (default: only new)")
    ap.add_argument('--reply', help="post an agent reply to chat.jsonl and exit")
    ap.add_argument('--anchor', default='run', help="dashboard anchor/cell id (used with --reply)")
    ap.add_argument('--resolved', action='store_true', help="mark the reply as resolved (used with --reply)")
    a = ap.parse_args()

    chat_path = os.path.join(a.state_dir, 'chat.jsonl')

    # Reply mode: write one agent reply and exit (no poll).
    if a.reply:
        os.makedirs(a.state_dir, exist_ok=True)
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

    # Poll mode: watch for new user notes.
    seen = set()
    if not a.include_existing:                      # ignore notes left before we started
        for m in _load(chat_path):
            if _matches(m, a.run_id):
                seen.add(_sig(m))
    deadline = (time.time() + a.timeout) if a.timeout else None

    while True:
        fresh = []
        for m in _load(chat_path):
            if _matches(m, a.run_id):
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


if __name__ == '__main__':
    sys.exit(main())
