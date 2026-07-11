#!/usr/bin/env python3
"""Acceptance tests for runguard's SAFETY core (not the UI): lock exclusivity,
stale-lock takeover, re-entrancy, scope independence, ledger append/continuity,
and cross-process throttle pacing. Uses real subprocesses. Exits non-zero on any fail."""
import os, sys, json, time, subprocess, tempfile, textwrap

RG_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))  # dir with runguard.py
STATE = tempfile.mkdtemp(prefix='rgtest-')
ENV = {**os.environ, 'RUNGUARD_STATE_DIR': STATE, 'PYTHONPATH': RG_DIR}


def lane_events(slug: str) -> str:
    """Preferred ledger path under runs/<slug>/events.jsonl."""
    return os.path.join(STATE, 'runs', slug, 'events.jsonl')
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
files = [p for p in glob.glob(f'{STATE}/runs/*/events.jsonl') if 'mysrc' in p]
ok("ledger continuous-by-source (one file for the scope)", len(files) == 1, f"files={files}")
if files:
    lines = [json.loads(l) for l in open(files[0]) if l.strip()]
    ok("all events appended in order across processes", len(lines) == 3 and lines[0]['event']=='run_started' and lines[-1]['key']=='b', f"n={len(lines)}")
    ok("record fields preserved (status)", any(l.get('status')=='skipped' for l in lines))

# ---- 7. RUNGUARD_SESSION opens a separate lane ----
env2 = {**ENV, 'RUNGUARD_SESSION': '2099-01-01-lane'}
subprocess.run([sys.executable,'-c','import runguard; runguard.ledger("mysrc","run_started",todo=1)'],
               env=env2, timeout=20)
laned = glob.glob(f'{STATE}/runs/2099-01-01-lane-mysrc/events.jsonl')
ok("RUNGUARD_SESSION creates a separate lane folder", len(laned) == 1, f"{laned}")

# ---- 8. Boring wrapper: lock + dry-run + steps + counters + checkpoints ----
rc8, out8, err8 = child("""
import json, os, runguard
run = runguard.start_observed_run('wrapper-demo', dry_run=True, description='demo')
assert run.run_id == 'runguard:wrapper-demo'
with run.step('enrich_lead', table='companies', key='lead-1', company='acme'):
    run.count('leads_enriched')
    run.checkpoint('last_lead', 'lead-1')
run.success(processed=1)
# Lockfiles are deliberately persistent: the OS flock, not file deletion, is the guard.
runguard.acquire_lock('wrapper-demo')
runguard.release_lock('wrapper-demo')
print(runguard.ledger_path('wrapper-demo'))
""")
ok("start_observed_run closes and releases its lock", rc8 == 0 and 'runs' in out8 and 'events.jsonl' in out8,
   f"rc={rc8} err={err8.strip()[:120]}")
wrapper_path = lane_events('wrapper-demo')
if os.path.isfile(wrapper_path):
    wrapper_lines = [json.loads(l) for l in open(wrapper_path) if l.strip()]
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
abandoned = lane_events('abandoned-demo')
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
stress_path = lane_events('append-stress')
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
step_exception_path = lane_events('step-exception')
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

# ---- 18. Missing then created path keeps one source scope ----
pending = os.path.join(STATE, 'pending-source.csv')
if os.path.exists(pending):
    os.remove(pending)
rc18, out18, err18 = child("""
import runguard, os
p = %r
if os.path.exists(p):
    os.remove(p)
before = runguard.source_scope('load', p)
open(p, 'w').write('id\\n1\\n')
after = runguard.source_scope('load', p)
assert before == after, (before, after)
print('STABLE-PATH')
""" % pending)
ok("source path scope stays stable when the file is created later",
   rc18 == 0 and 'STABLE-PATH' in out18, f"rc={rc18} err={err18.strip()[:120]}")

# ---- 19. Rapid ledger stamps stay ordered and unique ----
rc19, out19, err19 = child("""
import runguard, json, os
runguard.ledger('ts-burst', 'run_started')
for i in range(30):
    runguard.ledger('ts-burst', 'record', table='t', key=str(i), n=i)
path = runguard._lane_path('ts-burst')
rows = [json.loads(line) for line in open(path) if line.strip()]
stamps = [row['ts'] for row in rows]
assert len(stamps) == len(set(stamps)), stamps
assert stamps == sorted(stamps), stamps
assert all('.' in ts for ts in stamps)
print('TS-OK')
""")
ok("ledger timestamps are unique and ordered under rapid emit",
   rc19 == 0 and 'TS-OK' in out19, f"rc={rc19} err={err19.strip()[:120]}")

# ---- 20. Far-future throttle slots are clamped instead of hanging pipelines ----
rc20, out20, err20 = child("""
import runguard, os, time
path = runguard._state_path('stuck-api', '.throttle', 'resource')
open(path, 'w').write(str(time.time() + 3600))
start = time.time()
runguard.throttle('stuck-api', 10)
elapsed = time.time() - start
assert elapsed < 2.0, elapsed
print('THROTTLE-CLAMPED')
""")
ok("corrupt far-future throttle grants are clamped",
   rc20 == 0 and 'THROTTLE-CLAMPED' in out20, f"rc={rc20} err={err20.strip()[:120]}")

# ---- 21. RunPaused inside step does not emit failed/dead_letter ----
rc21, out21, err21 = child("""
import runguard, json
run = runguard.start_observed_run('pause-step', source='pause-step', dry_run=True)
runguard.post_control(run.run_id, 'pause')
try:
    with run.step('work', table='records', key='row-1'):
        run.check_controls()
except runguard.RunPaused:
    pass
rows = [json.loads(line) for line in open(runguard._lane_path(run.scope)) if line.strip()]
assert any(r.get('event') == 'run_paused' for r in rows)
assert any(r.get('event') == 'record' and r.get('key') == 'row-1' and r.get('status') == 'paused'
           for r in rows)
assert not any(r.get('event') == 'dead_letter' for r in rows)
assert not any(r.get('event') == 'record' and r.get('key') == 'row-1' and r.get('status') == 'failed'
               for r in rows)
print('PAUSE-STEP-OK')
""")
ok("RunPaused inside step marks the row paused without dead-letter",
   rc21 == 0 and 'PAUSE-STEP-OK' in out21, f"rc={rc21} err={err21.strip()[:160]}")

# ---- 22. Non-finite throttle values do not disable pacing forever ----
rc22, out22, err22 = child("""
import runguard, os, time, math
path = runguard._state_path('nan-api', '.throttle', 'resource')
open(path, 'w').write('nan')
start = time.time()
runguard.throttle('nan-api', 20)
# First call should heal the file to a finite grant.
raw = open(path).read().strip()
assert math.isfinite(float(raw)), raw
# Two paced calls at 20/s should still take a little time, not free-spin forever broken.
runguard.throttle('nan-api', 20)
runguard.throttle('nan-api', 20)
assert time.time() - start < 2.0
print('NAN-THROTTLE-OK')
""")
ok("non-finite throttle slots are rejected and pacing recovers",
   rc22 == 0 and 'NAN-THROTTLE-OK' in out22, f"rc={rc22} err={err22.strip()[:160]}")

# ---- 23. SIGTERM records run_abandoned instead of a silent hang ----
rc23, out23, err23 = child("""
import os, signal, time, json, runguard
run = runguard.start_observed_run('sigterm-demo', source='sig-src', dry_run=True)
path = runguard._lane_path(run.scope)
open(os.environ['RUNGUARD_STATE_DIR'] + '/sig.path', 'w').write(path)
os.kill(os.getpid(), signal.SIGTERM)
time.sleep(2)
print('STILL-ALIVE')
""")
# Child may exit via signal; read the ledger path it left behind.
sig_path_file = os.path.join(STATE, 'sig.path')
sig_ok = False
sig_detail = f'rc={rc23} out={out23!r} err={err23.strip()[:120]}'
if os.path.isfile(sig_path_file):
    ledger_path = open(sig_path_file).read().strip()
    if os.path.isfile(ledger_path):
        events = [json.loads(line) for line in open(ledger_path) if line.strip()]
        sig_ok = any(e.get('event') == 'run_abandoned' for e in events) and rc23 != 0
        sig_detail = f'rc={rc23} events={[e.get("event") for e in events]}'
ok("SIGTERM abandons the open run with a terminal ledger event", sig_ok, sig_detail)

# ---- 24. Ledger/receipt appends fsync (durability contract) ----
rc24, out24, err24 = child("""
import runguard
fsync_calls = []
_real = runguard.os.fsync
def tracking(fd):
    fsync_calls.append(fd)
    return _real(fd)
runguard.os.fsync = tracking
try:
    before = len(fsync_calls)
    runguard.ledger('fsync-scope', 'run_started', note='durability')
    after_ledger = len(fsync_calls)
    runguard.throttle('fsync-api', 1000)
    after_throttle = len(fsync_calls)
finally:
    runguard.os.fsync = _real
assert after_ledger > before, (before, after_ledger)
assert after_throttle > after_ledger, (after_ledger, after_throttle)
print('FSYNC-OK', after_ledger - before, after_throttle - after_ledger)
""")
ok("_append_jsonl and throttle fsync durable state",
   rc24 == 0 and 'FSYNC-OK' in out24, f"rc={rc24} out={out24!r} err={err24.strip()[:160]}")

# ---- 25. RUNGUARD_SESSION scopes the flock, not only the ledger path ----
holder25 = child("""
import os, runguard, time
os.environ['RUNGUARD_SESSION'] = 'lane-a'
run = runguard.start_observed_run('session-lock', source='shared-src', dry_run=True)
open(os.environ['RUNGUARD_STATE_DIR'] + '/session-a.ready', 'w').write(run.lock_key)
time.sleep(6)
run.success()
""", bg=True)
for _ in range(50):
    if os.path.exists(f'{STATE}/session-a.ready'):
        break
    time.sleep(0.1)
lock_a = open(f'{STATE}/session-a.ready').read().strip() if os.path.exists(f'{STATE}/session-a.ready') else ''
env_b = {**ENV, 'RUNGUARD_SESSION': 'lane-b'}
proc_b = subprocess.run(
    [sys.executable, '-c', textwrap.dedent("""
        import os, runguard
        os.environ['RUNGUARD_SESSION'] = 'lane-b'
        run = runguard.start_observed_run('session-lock', source='shared-src', dry_run=True)
        print('LOCK', run.lock_key)
        print('OK')
        run.success()
    """)],
    env=env_b, capture_output=True, text=True, timeout=20,
)
holder25.wait(timeout=15)
ok("session lane lock keys include the session slug",
   'lane-a--' in lock_a, f"lock_a={lock_a!r}")
ok("parallel RUNGUARD_SESSION lanes do not hard-contend the same flock",
   proc_b.returncode == 0 and 'OK' in proc_b.stdout,
   f"rc={proc_b.returncode} out={proc_b.stdout!r} err={proc_b.stderr.strip()[:160]}")
ok("session B lock key differs from session A",
   'lane-b--' in proc_b.stdout and lock_a not in proc_b.stdout.split(),
   f"lock_a={lock_a!r} out={proc_b.stdout!r}")

# ---- 26. stop pause stamps control= for durable disarm ----
rc26, out26, err26 = child("""
import runguard, json
run = runguard.start_observed_run('stop-control-field', source='stop-control-field')
runguard.post_control(run.run_id, 'stop_after_record')
run.check_controls()
try:
    run.check_controls(after_record=True)
except runguard.RunPaused:
    pass
else:
    raise SystemExit('expected stop pause')
rows = [json.loads(line) for line in open(runguard._lane_path(run.scope)) if line.strip()]
paused = [r for r in rows if r.get('event') == 'run_paused']
assert any(r.get('control') == 'stop_after_record' for r in paused), paused
print('STOP-CONTROL-OK')
""")
ok("stop_after_record pause stamps control=stop_after_record",
   rc26 == 0 and 'STOP-CONTROL-OK' in out26, f"rc={rc26} err={err26.strip()[:160]}")

# ---- 27. Same-process lock re-acquire is refcounted ----
rc27, out27, err27 = child("""
import runguard
runguard.acquire_lock('refcount-scope')
runguard.acquire_lock('refcount-scope')  # nested
runguard.release_lock('refcount-scope')  # still held
# A third process must still refuse while the outer hold remains.
import subprocess, os, sys
env = {**os.environ}
rc = subprocess.run(
    [sys.executable, '-c', "import runguard; runguard.acquire_lock('refcount-scope'); print('STOLE')"],
    env=env, capture_output=True, text=True, timeout=10,
).returncode
assert rc != 0, 'inner release must not unlock outer hold'
runguard.release_lock('refcount-scope')  # final unlock
rc2 = subprocess.run(
    [sys.executable, '-c', "import runguard; runguard.acquire_lock('refcount-scope'); print('FREE')"],
    env=env, capture_output=True, text=True, timeout=10,
)
assert rc2.returncode == 0 and 'FREE' in rc2.stdout
print('REFCOUNT-OK')
""")
ok("lock re-entrancy is refcounted (inner release keeps outer hold)",
   rc27 == 0 and 'REFCOUNT-OK' in out27, f"rc={rc27} err={err27.strip()[:200]}")

print(f"\n{'='*48}\n  {passed} passed, {failed} failed\n{'='*48}")
sys.exit(1 if failed else 0)
