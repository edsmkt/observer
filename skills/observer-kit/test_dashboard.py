#!/usr/bin/env python3
"""Acceptance tests for run_dashboard.py JSONL reading behavior."""
import importlib.util
import json
import os
import tempfile


HERE = os.path.dirname(os.path.abspath(__file__))
RUN_DASHBOARD = os.path.join(HERE, 'run_dashboard.py')
passed, failed = 0, 0


def ok(name, cond, detail=''):
    global passed, failed
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  — {detail}" if detail and not cond else ''))
    if cond:
        passed += 1
    else:
        failed += 1


spec = importlib.util.spec_from_file_location('run_dashboard_under_test', RUN_DASHBOARD)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)

print(f"Testing run_dashboard.py at {RUN_DASHBOARD}\n")

with tempfile.TemporaryDirectory(prefix='rgdash-') as state:
    dashboard.SOURCES['runguard'] = state
    dashboard.EVENT_READ_BYTES = 80
    ledger = os.path.join(state, 'large-run.jsonl')
    rows = [
        {'ts': '2026-07-09T12:00:00', 'event': 'run_started', 'todo': 2},
        {
            'ts': '2026-07-09T12:00:01',
            'event': 'record',
            'table': 'companies',
            'key': 'big',
            'company': 'big.example',
            'notes': 'x' * 500,
        },
        {'ts': '2026-07-09T12:00:02', 'event': 'record', 'table': 'companies', 'key': 'small'},
    ]
    with open(ledger, 'w', encoding='utf-8') as fh:
        for row in rows:
            fh.write(json.dumps(row) + '\n')

    events, offsets = dashboard.read_events('runguard:large-run.jsonl', {})
    ok("chunked read keeps full JSONL records", [e.get('key') for e in events if e.get('event') == 'record'] == ['big'])
    ok("chunked read advances beyond the large record", list(offsets.values())[0] > 80)

    events2, offsets2 = dashboard.read_events('runguard:large-run.jsonl', offsets)
    ok("next read continues after completed large record", [e.get('key') for e in events2] == ['small'])
    ok("offset reaches end after second read", list(offsets2.values())[0] == os.path.getsize(ledger))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
