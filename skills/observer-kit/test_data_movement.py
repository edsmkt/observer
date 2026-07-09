#!/usr/bin/env python3
"""Acceptance tests for Observer Kit's optional data-movement guardrails."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time


RG_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
STATE = tempfile.mkdtemp(prefix='rg-data-')
os.environ['RUNGUARD_STATE_DIR'] = STATE
sys.path.insert(0, RG_DIR)
import runguard  # noqa: E402

passed = failed = 0


def ok(name, condition, detail=''):
    global passed, failed
    print(f"  {'PASS' if condition else 'FAIL'} {name}" + (f' - {detail}' if detail and not condition else ''))
    if condition:
        passed += 1
    else:
        failed += 1


def events(run):
    with open(runguard.ledger_path(run.scope), encoding='utf-8') as fh:
        return [json.loads(line) for line in fh if line.strip()]


print(f"Testing data-movement guardrails in {RG_DIR}\n  state dir: {STATE}\n")

# Manifests compare reviewed source fingerprints across attempts in the same lane.
first = runguard.input_snapshot('sheet:leads', records=[{'id': '1', 'domain': 'acme.test'}], version='v1')
run = runguard.start_observed_run('manifest-demo', source='sheet:leads', input_snapshot=first)
run.success()
second = runguard.input_snapshot('sheet:leads', records=[{'id': '2', 'domain': 'other.test'}], version='v2')
run = runguard.start_observed_run('manifest-demo', source='sheet:leads', input_snapshot=second)
run.success()
manifest_events = events(run)
ok('manifest records source fingerprints and changed input',
   len([e for e in manifest_events if e.get('event') == 'run_manifest']) == 2 and
   any(e.get('event') == 'input_changed' for e in manifest_events))

input_file = os.path.join(STATE, 'auto-snapshot.csv')
with open(input_file, 'w', encoding='utf-8') as fh:
    fh.write('id\n1\n')
run = runguard.start_observed_run('auto-snapshot-demo', source=input_file)
run.success()
with open(input_file, 'a', encoding='utf-8') as fh:
    fh.write('2\n')
run = runguard.start_observed_run('auto-snapshot-demo', source=input_file)
run.success()
auto_events = events(run)
ok('file sources get automatic input snapshots before a resume',
   all(e.get('input_snapshot', {}).get('sha256') for e in auto_events if e.get('event') == 'run_manifest') and
   any(e.get('event') == 'input_changed' for e in auto_events))

# Contracts, policy, and quality failures stay visible and stop safely when asked.
run = runguard.start_observed_run('contract-demo')
schema_ok = run.validate({'id': '1', 'email': 'bad'}, '1',
                         {'required': ['id', 'consent'], 'types': {'id': 'str'}},
                         on_error='dead_letter')
policy_ok = run.allow_write({'id': '1', 'suppressed': True}, '1',
                            {'forbidden_true': ['suppressed']})
try:
    run.gate('minimum-qualified', observed=0, minimum=1)
except runguard.RunPaused:
    paused = True
else:
    paused = False
contract_events = events(run)
ok('schema, policy, and quality gates leave durable review events',
   not schema_ok and not policy_ok and paused and
   {'schema_violation', 'policy_blocked', 'dead_letter', 'run_paused'}
   .issubset({e.get('event') for e in contract_events}))

# Receipts are global to a destination. A receipt dedupes; an unknown pending
# state refuses a blind external retry.
run = runguard.start_observed_run('write-a', source='write-source-a', destination='sheet', transform_version='v1')
ticket = run.write_intent('row-1', payload={'name': 'Acme'})
run.write_receipt(ticket, destination_id='sheet-row-1', verified=True,
                  record_table='accounts', outcome_field='google_sheet',
                  outcome='appended', record_fields={'company': 'Acme'})
write_a_events = events(run)
run.success()
run = runguard.start_observed_run('write-b', source='write-source-b', destination='sheet', transform_version='v1')
duplicate = run.write_intent('row-1', payload={'name': 'Acme'})
pending_ticket = run.write_intent('row-2')
run.success()
run = runguard.start_observed_run('write-c', source='write-source-c', destination='sheet', transform_version='v1')
try:
    run.write_intent('row-2')
except runguard.PendingWrite:
    protected = True
    run.fail('pending receipt protected')
else:
    protected = False
    run.success()
business_update = next((event for event in write_a_events
                        if event.get('event') == 'record' and event.get('table') == 'accounts'), {})
ok('receipt dedupe, business-row outcome updates, and pending-write protection work across runs',
   ticket is not None and duplicate is None and pending_ticket is not None and protected and
   business_update.get('key') == 'row-1' and business_update.get('google_sheet') == 'appended' and
   business_update.get('company') == 'Acme')

go = os.path.join(STATE, 'receipt-race.go')
race_code = """
import os, sys, time, runguard
while not os.path.exists(%r):
    time.sleep(.01)
run = runguard.start_observed_run('receipt-race-' + sys.argv[1], source='receipt-race-' + sys.argv[1], destination='parallel-sink')
try:
    run.write_intent('shared-row')
    print('CLAIMED')
    time.sleep(.3)
    run.fail('intentional pending receipt for race test')
except runguard.PendingWrite:
    print('PENDING')
    run.fail('pending receipt protected')
""" % go
race_env = {**os.environ, 'RUNGUARD_STATE_DIR': STATE,
            'PYTHONPATH': RG_DIR + os.pathsep + os.environ.get('PYTHONPATH', '')}
racers = [subprocess.Popen([sys.executable, '-c', race_code, str(n)], env=race_env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
          for n in range(2)]
time.sleep(.05)
with open(go, 'w', encoding='utf-8') as fh:
    fh.write('go')
race_output = [proc.communicate(timeout=10)[0] for proc in racers]
ok('parallel sinks reserve one write intent and block the duplicate',
   sum('CLAIMED' in output for output in race_output) == 1 and
   sum('PENDING' in output for output in race_output) == 1, str(race_output))

# Failed items stay small, deterministic replay candidates until a receipt lands.
run = runguard.start_observed_run('replay-demo', source='replay-source', destination='crm')
run.dead_letter('bad-row', 'temporary provider error', payload_ref='cache:bad-row')
before = run.replay_candidates()
ticket = run.write_intent('bad-row')
run.write_receipt(ticket, destination_id='crm-42')
after = run.replay_candidates()
summary = run.reconcile()
run.success()
ok('dead-letter replay candidates resolve after a matching receipt',
   [e.get('record_key') for e in before] == ['bad-row'] and not after and
   summary['intended'] == summary['written'] == 1 and summary['dead_letters'] == 0)

# Controls are input to a worker, not a dashboard-side kill command.
run = runguard.start_observed_run('control-demo', source='control-source')
runguard.post_control(run.run_id, 'stop_after_record')
seen = run.check_controls()
try:
    run.check_controls(after_record=True)
except runguard.RunPaused:
    control_paused = True
else:
    control_paused = False
control_events = events(run)
ok('control requests wait for the worker safe point',
   bool(seen) and control_paused and any(e.get('event') == 'control_acknowledged' for e in control_events) and
   control_events[-1].get('event') == 'run_paused')

run = runguard.start_observed_run('control-demo', source='control-source')
replayed_controls = run.check_controls()
run.success()
ok('a recovered run does not honor an acknowledged control again', not replayed_controls)

# Fixture simulation records a reproducible input but never needs live systems.
fixture = os.path.join(STATE, 'simulation.jsonl')
with open(fixture, 'w', encoding='utf-8') as fh:
    fh.write('{"id":"a"}\n{"id":"b"}\n')
run = runguard.start_observed_run('simulation-demo', dry_run=True)
simulated = run.simulate(fixture)
run.success()
ok('simulation fixture is replayable and ledgered',
   [row['id'] for row in simulated] == ['a', 'b'] and
   any(e.get('event') == 'simulation' and e.get('records') == 2 for e in events(run)))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
