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

# Multi-node: a write_receipt for the same business key must not mask another
# node's dead_letter (flow demos share domain as the row key across nodes).
run = runguard.start_observed_run('replay-nodes', source='replay-nodes', destination='crm')
run.dead_letter('acme.com', 'contact lookup failed', node_id='find_contact')
run.dead_letter('acme.com', 'sheet append failed', node_id='prepare_sheet')
both = run.replay_candidates()
ticket = run.write_intent('acme.com')
run.write_receipt(ticket, destination_id='sheet-1', node_id='prepare_sheet')
after_sheet = run.replay_candidates()
ok('replay keeps distinct (node_id, record_key) pairs',
   sorted(e.get('node_id') for e in both) == ['find_contact', 'prepare_sheet'])
ok('write_receipt clears only the matching node pair',
   [e.get('node_id') for e in after_sheet] == ['find_contact'],
   str([e.get('node_id') for e in after_sheet]))
run.success()

# Integration-shaped path: validate dead_letter falls back to table=node id,
# write_intent stamps node_id on the ticket, write_receipt inherits it — no
# manual node_id= on the receipt call required. Use a unique destination so
# prior tests' receipt registry does not idempotent-skip this intent.
run = runguard.start_observed_run(
    'replay-plumb', source='replay-plumb',
    destination='synthetic-review-sheet-plumb', dry_run=False,
)
run.validate(
    {'id': 'x'}, key='northstar.test',
    contract={'required': ['missing_field']},
    table='prepare_sheet', on_error='skip',
)
after_fail = run.replay_candidates()
ticket = run.write_intent('northstar.test', node_id='prepare_sheet')
ok('write_intent stamps node_id on the ticket',
   bool(ticket) and ticket.get('node_id') == 'prepare_sheet', str(ticket))
run.write_receipt(ticket, destination_id='ok')  # no explicit node_id=
after_ok = run.replay_candidates()
ok('demo-shaped dead_letter uses table as node_id',
   after_fail and after_fail[0].get('node_id') == 'prepare_sheet'
   and after_fail[0].get('record_key') == 'northstar.test',
   str(after_fail))
ok('ticket-stamped write_receipt clears same-node dead_letter without extra args',
   not any(e.get('record_key') == 'northstar.test' and e.get('node_id') == 'prepare_sheet'
           for e in after_ok),
   str(after_ok))
run.success()

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
ok('a recovered run after stop-pause does not re-list the control', not replayed_controls)
ok('a recovered run after completed stop-pause is not still armed to stop',
   run.stop_requested is False)
run.success()

# Crash after stop is acked but before the stop pause: re-arm on resume.
run = runguard.start_observed_run('control-stop-sticky', source='control-stop-sticky')
runguard.post_control(run.run_id, 'stop_after_record')
armed = run.check_controls()
ok('stop_after_record arms stop_requested without pausing yet',
   bool(armed) and run.stop_requested is True)
run.closed = True
runguard.release_lock(run.lock_key)

run = runguard.start_observed_run('control-stop-sticky', source='control-stop-sticky')
ok('stop stays armed across resume when pause never completed',
   run.stop_requested is True and run.check_controls() == [])
try:
    run.check_controls(after_record=True)
except runguard.RunPaused:
    sticky_paused = True
else:
    sticky_paused = False
ok('resumed stop still pauses at the next after_record boundary', sticky_paused)

# Full-run approval must survive item-loop check_controls until the harness acts.
run = runguard.start_observed_run('control-approve', source='control-approve', dry_run=True)
runguard.post_control(run.run_id, 'approve_full_run', note='ship it')
first = run.check_controls()
second = run.check_controls()
ok('approve_full_run is returned without being auto-acknowledged',
   [c.get('kind') for c in first] == ['approve_full_run']
   and [c.get('kind') for c in second] == ['approve_full_run']
   and not any(e.get('event') == 'control_acknowledged' for e in events(run)))
run.acknowledge_control(first[0])
third = run.check_controls()
ok('acknowledge_control consumes full-run approval once',
   third == [] and any(e.get('event') == 'control_acknowledged'
                       and e.get('control') == 'approve_full_run' for e in events(run)))
run.success()

# Nanosecond stamps keep chat watermarks and ledger order trustworthy.
burst = []
for i in range(20):
    burst.append(runguard._timestamp())
ok('timestamps are unique within a rapid burst', len(set(burst)) == len(burst))
since = runguard._timestamp()
runguard.post_chat('chat-watermark', 'run', 'operator note', author='user')
notes = runguard.read_chat('chat-watermark', after_ts=since, author='user')
ok('chat notes posted after a watermark are not dropped',
   len(notes) == 1 and notes[0].get('text') == 'operator note')
# Mixed second-only vs nanosecond stamps still compare chronologically.
mixed_since = '2026-07-11T12:00:00.500000000Z'
os.makedirs(os.path.join(STATE, 'runs', 'mixed-ts'), exist_ok=True)
with open(os.path.join(STATE, 'runs', 'mixed-ts', 'chat.jsonl'), 'a', encoding='utf-8') as fh:
    fh.write(json.dumps({
        'ts': '2026-07-11T12:00:00Z', 'run': 'mixed-ts', 'anchor': 'run',
        'author': 'user', 'text': 'before-watermark',
    }) + '\n')
    fh.write(json.dumps({
        'ts': '2026-07-11T12:00:01Z', 'run': 'mixed-ts', 'anchor': 'run',
        'author': 'user', 'text': 'after-watermark',
    }) + '\n')
mixed = runguard.read_chat('mixed-ts', after_ts=mixed_since, author='user')
ok('mixed-precision chat watermarks keep later second-only notes',
   [m.get('text') for m in mixed] == ['after-watermark'])

# Pause inside step must not fail the row or open a dead letter.
run = runguard.start_observed_run('pause-in-step', source='pause-in-step', dry_run=True)
runguard.post_control(run.run_id, 'pause')
try:
    with run.step('enrich', table='records', key='row-1', label='Acme'):
        run.check_controls()
except runguard.RunPaused:
    pause_in_step = True
else:
    pause_in_step = False
pause_events = events(run)
ok('pause inside step does not fail the row or dead-letter it',
   pause_in_step
   and any(e.get('event') == 'run_paused' for e in pause_events)
   and any(e.get('event') == 'record' and e.get('key') == 'row-1'
           and e.get('status') == 'paused' for e in pause_events)
   and not any(e.get('event') == 'dead_letter' for e in pause_events)
   and not any(e.get('event') == 'record' and e.get('key') == 'row-1'
               and e.get('status') == 'failed' for e in pause_events)
   and not any(e.get('record_key') == 'row-1' for e in run.replay_candidates()))

# Path scopes stay stable when the file appears after the first call.
pending_path = os.path.join(STATE, 'appears-later.csv')
if os.path.exists(pending_path):
    os.remove(pending_path)
before = runguard.source_scope('import', pending_path)
open(pending_path, 'w').write('id\n1\n')
after = runguard.source_scope('import', pending_path)
ok('source scope is stable when a path is created after first use', before == after)

# Unique-field reservations survive crash/resume on the same lane.
run = runguard.start_observed_run('unique-durability', source='unique-src', dry_run=True)
ok('first unique value is accepted',
   run.validate({'id': '1', 'email': 'a@x.com'}, '1',
                {'unique': ['email']}, on_error='skip') is True)
run.closed = True
runguard.release_lock(run.lock_key)
run = runguard.start_observed_run('unique-durability', source='unique-src', dry_run=True)
ok('same key may re-validate its unique value after resume',
   run.validate({'id': '1', 'email': 'a@x.com'}, '1',
                {'unique': ['email']}, on_error='skip') is True)
ok('unique values remain reserved against a different key after resume',
   run.validate({'id': '2', 'email': 'a@x.com'}, '2',
                {'unique': ['email']}, on_error='skip') is False)
# Failed rows release markers so a retry can reclaim the value.
run.dead_letter('1', 'downstream write failed')
ok('dead-letter releases unique markers for retry',
   run.validate({'id': '1', 'email': 'a@x.com'}, '1',
                {'unique': ['email']}, on_error='skip') is True)
run.success()

# Dry-run receipts must not look like real writes.
run = runguard.start_observed_run('dry-receipt', source='dry-receipt', dry_run=True,
                                  destination='crm')
ticket = run.write_intent('row-1', payload={'ok': True})
run.write_receipt(ticket, destination_id='crm-1', verified=True,
                  record_table='records', outcome='appended')
dry_events = events(run)
ok('dry-run write_receipt stays on the preview surface',
   any(e.get('event') == 'write_preview' and e.get('status') == 'planned' for e in dry_events)
   and not any(e.get('event') == 'write_receipt' for e in dry_events)
   and not any(e.get('event') == 'record' and e.get('table') == 'writes'
               and e.get('status') in {'written', 'verified'} for e in dry_events)
   and any(e.get('event') == 'record' and e.get('table') == 'records'
           and e.get('key') == 'row-1' and e.get('crm') == 'planned'
           and e.get('status') == 'preview' for e in dry_events))
run.success()

# One full-run completion consumes pending operator approval.
run = runguard.start_observed_run('approve-once', source='approve-once', dry_run=False)
runguard.post_control(run.run_id, 'approve_full_run', note='go')
# Stamp control in the past relative to a synthetic prior full-run finish by
# completing this full run (consumes approval), then ensure a later run does not
# still see it.
ok('full-run sees pending approval before completion',
   any(c.get('kind') == 'approve_full_run' for c in run.check_controls()))
run.success()
run = runguard.start_observed_run('approve-once', source='approve-once', dry_run=False)
ok('completed full-run consumes approval for later attempts',
   run.check_controls() == [])
run.success()

# Reserved counter names must not break terminal lifecycle fields.
run = runguard.start_observed_run('counter-reserve', source='counter-src', dry_run=True)
run.count('status', 3)
run.count('processed', 1)
run.success()
finished = [e for e in events(run) if e.get('event') == 'run_finished']
ok('run.count("status") does not clobber terminal status',
   finished and finished[-1].get('status') == 'success'
   and finished[-1].get('processed') == 1
   and finished[-1].get('counter_overrides', {}).get('status') == 3)

# stop_after_record must not stick after an explicit failure.
run = runguard.start_observed_run('stop-fail', source='stop-fail', dry_run=False)
runguard.post_control(run.run_id, 'stop_after_record')
run.check_controls()
ok('stop arms before fail', run.stop_requested is True)
run.fail('forced')
run = runguard.start_observed_run('stop-fail', source='stop-fail', dry_run=False)
ok('stop is cleared after run.fail terminal', run.stop_requested is False)
run.success()

# Idempotent skips are not pending writes.
run = runguard.start_observed_run('reconcile-skip', source='reconcile-skip', dry_run=False,
                                  destination='crm')
first = run.write_intent('k1', payload={'v': 1})
run.write_receipt(first, destination_id='1', verified=True)
second = run.write_intent('k1', payload={'v': 1})
ok('duplicate write_intent is skipped', second is None)
summary = run.reconcile()
ok('reconcile does not count skipped ops as pending',
   summary['skipped'] >= 1 and summary['pending'] == 0, str(summary))
run.success()

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

# A bounded real response can establish the observed schema and remain inspectable.
run = runguard.start_observed_run('schema-sample-demo', source='api:companies', dry_run=True,
                                  summary_metrics=['sampled'])
response = {
    'id': 'company-1',
    'properties': {'name': 'Acme', 'employees': 42, 'tags': ['saas', 'b2b']},
}
observed_response = run.schema_sample('companies', response['id'], response, name='Acme')
run.count('sampled')
run.success()
schema_events = events(run)
schema = next((event for event in schema_events
               if event.get('event') == 'schema_observed'), {})
sample_row = next((event for event in schema_events
                   if event.get('event') == 'record' and event.get('table') == 'companies'), {})
defensive_redaction = runguard._redact_sample(
    {'authorization': 'unexpected-credential', 'result': {'id': 'company-1'}}, set())
ok('schema samples store field types and the decoded response body',
   schema.get('paths', {}).get('$.properties.employees') == ['integer'] and
   sample_row.get('response_json') == response and observed_response == response and
   defensive_redaction.get('authorization') == '[REDACTED]')

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
