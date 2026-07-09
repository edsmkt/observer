#!/usr/bin/env python3
"""Acceptance tests for runguard's SAFETY core (not the UI): lock exclusivity,
stale-lock takeover, re-entrancy, scope independence, ledger append/continuity,
and cross-process throttle pacing. Uses real subprocesses. Exits non-zero on any fail."""
import os, sys, json, time, subprocess, tempfile, textwrap

RG_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))  # dir with runguard.py
STATE = tempfile.mkdtemp(prefix='rgtest-')
ENV = {**os.environ, 'RUNGUARD_STATE_DIR': STATE, 'PYTHONPATH': RG_DIR}
passed, failed = 0, 0

def ok(name, cond, detail=''):
    global passed, failed
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail and not cond else ''))
    if cond: passed += 1
    else: failed += 1

def child(code, bg=False):
    """Run python code with runguard importable + shared STATE dir."""
    p = subprocess.Popen([sys.executable, '-c', textwrap.dedent(code)],
                         env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if bg: return p
    out, err = p.communicate(timeout=30)
    return p.returncode, out, err

print(f"Testing runguard in {RG_DIR}\n  state dir: {STATE}\n")

# ---- 1. Lock exclusivity: a 2nd process on the same scope HARD-REFUSES ----
holder = child("""
import runguard, time
runguard.acquire_lock('scopeA')
open('%s/holder.ready','w').write('1')
time.sleep(6)
""" % STATE, bg=True)
for _ in range(50):
    if os.path.exists(f'{STATE}/holder.ready'): break
    time.sleep(0.1)
rc, out, err = child("import runguard; runguard.acquire_lock('scopeA'); print('ACQUIRED')")
ok("2nd run on same scope refuses (nonzero exit)", rc != 0, f"rc={rc}")
ok("refusal warning explains consequences", 'WARNING:' in err and 'duplicate provider charges' in err and 'kill ' in err,
   err.strip()[:160])

# ---- 2. Different scope is NOT blocked while scopeA is held ----
rc2, out2, _ = child("import runguard; runguard.acquire_lock('scopeB'); print('ACQUIRED')")
ok("different scope runs in parallel", rc2 == 0 and 'ACQUIRED' in out2, f"rc={rc2}")
holder.wait(timeout=10)

# ---- 3. After holder exits, the scope frees and can be re-acquired ----
rc3, out3, _ = child("import runguard; runguard.acquire_lock('scopeA'); print('ACQUIRED')")
ok("scope re-acquirable after holder exits", rc3 == 0 and 'ACQUIRED' in out3, f"rc={rc3}")

# ---- 4. Stale lock (dead PID) is taken over, not honored forever ----
import glob
# forge a lockfile for scopeC with a guaranteed-dead PID
lf = None
child("import runguard; print(runguard._lockfile('scopeC'))")  # warm path
rc, out, _ = child("import runguard; print(runguard._lockfile('scopeC'))")
lf = out.strip()
with open(lf, 'w') as f:
    json.dump({'pid': 999999, 'started': '2020-01-01T00:00:00'}, f)
rc4, out4, err4 = child("import runguard; runguard.acquire_lock('scopeC'); print('ACQUIRED')")
ok("stale lock (dead pid) is taken over", rc4 == 0 and 'ACQUIRED' in out4, f"rc={rc4} err={err4.strip()[:80]}")

# ---- 5. Re-entrant: same process acquiring same scope twice does NOT refuse ----
rc5, out5, err5 = child("""
import runguard
runguard.acquire_lock('scopeD')
runguard.acquire_lock('scopeD')   # again, same process
print('OK-REENTRANT')
""")
ok("re-entrant acquire (same PID) is safe", rc5 == 0 and 'OK-REENTRANT' in out5, f"rc={rc5} err={err5.strip()[:80]}")

# ---- 6. Ledger: appends JSONL, same scope => same continuous file ----
child("""
import runguard
runguard.ledger('mysrc','run_started',todo=3)
runguard.ledger('mysrc','record',key='a',company='a.de',status='done')
""")
child("import runguard; runguard.ledger('mysrc','record',key='b',company='b.de',status='skipped')")
files = [p for p in glob.glob(f'{STATE}/*mysrc*.jsonl')]
ok("ledger continuous-by-source (one file for the scope)", len(files) == 1, f"files={[os.path.basename(f) for f in files]}")
if files:
    lines = [json.loads(l) for l in open(files[0]) if l.strip()]
    ok("all events appended in order across processes", len(lines) == 3 and lines[0]['event']=='run_started' and lines[-1]['key']=='b', f"n={len(lines)}")
    ok("record fields preserved (status)", any(l.get('status')=='skipped' for l in lines))

# ---- 7. RUNGUARD_SESSION opens a separate lane ----
env2 = {**ENV, 'RUNGUARD_SESSION': '2099-01-01-lane'}
subprocess.run([sys.executable,'-c','import runguard; runguard.ledger("mysrc","run_started",todo=1)'],
               env=env2, timeout=20)
laned = glob.glob(f'{STATE}/2099-01-01-lane-mysrc.jsonl')
ok("RUNGUARD_SESSION creates a separate lane file", len(laned) == 1, f"{[os.path.basename(f) for f in laned]}")

# ---- 8. Boring wrapper: lock + dry-run + steps + counters + checkpoints ----
rc8, out8, err8 = child("""
import json, os, runguard
run = runguard.start_observed_run('wrapper-demo', dry_run=True, description='demo')
assert run.run_id == 'runguard:wrapper-demo.jsonl'
with run.step('enrich_lead', table='companies', key='lead-1', company='acme'):
    run.count('leads_enriched')
    run.checkpoint('last_lead', 'lead-1')
run.success(processed=1)
# Lockfiles are deliberately persistent: the OS flock, not file deletion, is the guard.
runguard.acquire_lock('wrapper-demo')
runguard.release_lock('wrapper-demo')
print(runguard.ledger_path('wrapper-demo'))
""")
ok("start_observed_run closes and releases its lock", rc8 == 0 and 'wrapper-demo.jsonl' in out8,
   f"rc={rc8} err={err8.strip()[:120]}")
wrapper_files = glob.glob(f'{STATE}/wrapper-demo.jsonl')
if wrapper_files:
    wrapper_lines = [json.loads(l) for l in open(wrapper_files[0]) if l.strip()]
    ok("wrapper logs dry-run run_started", wrapper_lines[0]['event'] == 'run_started' and wrapper_lines[0]['dry_run'] is True)
    ok("ledger timestamps are explicit UTC", wrapper_lines[0]['ts'].endswith('Z'))
    ok("wrapper step records running then done",
       [l.get('status') for l in wrapper_lines if l.get('event') == 'record'] == ['running', 'done'])
    ok("wrapper success carries counters + checkpoints",
       wrapper_lines[-1]['event'] == 'run_finished'
       and wrapper_lines[-1]['leads_enriched'] == 1
       and wrapper_lines[-1]['checkpoints']['last_lead'] == 'lead-1')

# ---- 9. Cross-process throttle: N calls at R/s across P procs takes ~ (N-1)/R ----
RATE, CALLS, PROCS = 4, 4, 3   # 12 calls total at 4/s -> expect ~2.75s if cross-process
worker = "import runguard,time\n[runguard.throttle('api',%d) for _ in range(%d)]\n" % (RATE, CALLS)
t0 = time.time()
ps = [subprocess.Popen([sys.executable,'-c',worker], env=ENV) for _ in range(PROCS)]
for p in ps: p.wait(timeout=30)
elapsed = time.time() - t0
expected = (RATE*0 + (CALLS*PROCS - 1)) / RATE   # (total-1)/rate
ok(f"throttle paces cross-process ({CALLS*PROCS} calls @ {RATE}/s ≈ {expected:.1f}s)",
   elapsed >= expected*0.7, f"took {elapsed:.2f}s (per-process-broken would be ~{(CALLS-1)/RATE:.1f}s)")

# ---- 10. Scope/resource names are safe filenames, not paths ----
rc10, out10, err10 = child("""
import os, runguard
runguard.acquire_lock('hubspot/list-a')
runguard.ledger('../escaped', 'record', table='companies', key='x')
p = runguard.ledger_path('../escaped')
assert os.path.realpath(p).startswith(os.path.realpath(os.environ['RUNGUARD_STATE_DIR']) + os.sep)
print('SAFE')
""")
ok("path-like scope names stay inside state dir", rc10 == 0 and 'SAFE' in out10, f"rc={rc10} err={err10.strip()[:80]}")

# ---- 11. A forgotten close leaves an explicit failed terminal event ----
rc11, out11, err11 = child("""
import runguard
runguard.start_observed_run('abandoned-demo')
raise RuntimeError('boom')
""")
abandoned = os.path.join(STATE, 'abandoned-demo.jsonl')
abandoned_events = [json.loads(line).get('event') for line in open(abandoned) if line.strip()]
ok("unhandled exits log run_abandoned", rc11 != 0 and abandoned_events[-1] == 'run_abandoned', str(abandoned_events))

# ---- 12. Simultaneous first starts have exactly one flock holder ----
go = os.path.join(STATE, 'race.go')
race_code = """
import os, time, runguard
while not os.path.exists(%r):
    time.sleep(.01)
runguard.acquire_lock('simultaneous-race')
print('ACQUIRED')
time.sleep(.5)
""" % go
race_a = child(race_code, bg=True)
race_b = child(race_code, bg=True)
time.sleep(.1)
open(go, 'w').write('go')
race_out = []
for proc in (race_a, race_b):
    out, err = proc.communicate(timeout=10)
    race_out.append((proc.returncode, out, err))
ok("simultaneous first starts have one holder",
   sum(1 for rc, out, _ in race_out if rc == 0 and 'ACQUIRED' in out) == 1
   and sum(1 for rc, _, _ in race_out if rc != 0) == 1,
   str([(rc, out.strip(), err.strip()[:40]) for rc, out, err in race_out]))

# ---- 13. Source-derived scopes are stable and reject manual alternatives ----
source_file = os.path.join(STATE, 'actual-source.csv')
open(source_file, 'w').write('id\n1\n')
rc13, out13, err13 = child("""
import runguard
p = %r
first = runguard.source_scope('enrich', p)
second = runguard.source_scope('enrich', p)
assert first == second
run = runguard.start_observed_run('enrich', source=p)
assert run.scope == first
run.success()
try:
    runguard.start_observed_run('enrich', source=p, lock_key='made-up-label')
except ValueError:
    print('SOURCE-SAFE')
""" % source_file)
ok("source-derived scopes are stable and reject manual alternatives",
   rc13 == 0 and 'SOURCE-SAFE' in out13, f"rc={rc13} err={err13.strip()[:80]}")

# ---- 14. Concurrent append stress: every JSONL event survives and parses ----
WRITERS, EVENTS_PER_WRITER = 8, 75
stress_code = """
import runguard, sys
worker, count = sys.argv[1], int(sys.argv[2])
for n in range(count):
    runguard.ledger('append-stress', 'record', table='rows', key=f'{worker}-{n}', worker=worker, n=n)
"""
stress = [subprocess.Popen([sys.executable, '-c', stress_code, str(w), str(EVENTS_PER_WRITER)],
                           env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
          for w in range(WRITERS)]
for proc in stress:
    proc.wait(timeout=30)
stress_path = os.path.join(STATE, 'append-stress.jsonl')
try:
    stress_events = [json.loads(line) for line in open(stress_path, encoding='utf-8') if line.strip()]
except (OSError, json.JSONDecodeError) as exc:
    stress_events = []
    stress_error = str(exc)
else:
    stress_error = ''
stress_keys = {event.get('key') for event in stress_events}
ok("concurrent ledger appends keep every complete JSONL event",
   len(stress_events) == WRITERS * EVENTS_PER_WRITER and len(stress_keys) == WRITERS * EVENTS_PER_WRITER,
   f"events={len(stress_events)} unique={len(stress_keys)} {stress_error}")

# ---- 15. Step exceptions write one failed row and an explicit terminal failure ----
rc15, out15, err15 = child("""
import runguard
run = runguard.start_observed_run('step-exception')
try:
    with run.step('mutate', table='companies', key='bad-row'):
        raise ValueError('planned step failure')
except ValueError as exc:
    run.fail(exc)
""")
step_exception_path = os.path.join(STATE, 'step-exception.jsonl')
step_exception_events = [json.loads(line) for line in open(step_exception_path) if line.strip()]
ok("step exceptions retain row failure and terminal failure",
   rc15 == 0
   and [event.get('status') for event in step_exception_events
        if event.get('event') == 'record' and event.get('table') != 'dead_letters'] == ['running', 'failed']
   and any(event.get('event') == 'dead_letter' and event.get('record_key') == 'bad-row'
           for event in step_exception_events)
   and step_exception_events[-1].get('event') == 'run_failed',
   str(step_exception_events))

# ---- 16. Source path aliases coordinate through the resolved source identity ----
real_source = os.path.join(STATE, 'source-real.csv')
link_source = os.path.join(STATE, 'source-link.csv')
open(real_source, 'w').write('id\n1\n')
os.symlink(real_source, link_source)
rc16, out16, err16 = child("""
import runguard
assert runguard.source_scope('sync', %r) == runguard.source_scope('sync', %r)
print('SAME-SCOPE')
""" % (real_source, link_source))
ok("source symlinks resolve to the same lock scope", rc16 == 0 and 'SAME-SCOPE' in out16,
   f"rc={rc16} err={err16.strip()[:80]}")

# ---- 17. Sanitized names cannot collide with a friendly filename lookalike ----
rc17, out17, err17 = child("""
import runguard
assert runguard._safe_component('../same', 'scope') != runguard._safe_component('same', 'scope')
print('NO-COLLISION')
""")
ok("sanitized scope names keep a collision-resistant digest", rc17 == 0 and 'NO-COLLISION' in out17,
   f"rc={rc17} err={err17.strip()[:80]}")

print(f"\n{'='*48}\n  {passed} passed, {failed} failed\n{'='*48}")
sys.exit(1 if failed else 0)
