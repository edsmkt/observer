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
ok("refusal message explains why", 'REFUS' in err.upper() or 'refus' in err, err.strip()[:80])

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
assert not os.path.exists(os.path.join(os.environ['RUNGUARD_STATE_DIR'], 'wrapper-demo.lock'))
print(runguard.ledger_path('wrapper-demo'))
""")
ok("start_observed_run closes and releases its lock", rc8 == 0 and 'wrapper-demo.jsonl' in out8,
   f"rc={rc8} err={err8.strip()[:120]}")
wrapper_files = glob.glob(f'{STATE}/wrapper-demo.jsonl')
if wrapper_files:
    wrapper_lines = [json.loads(l) for l in open(wrapper_files[0]) if l.strip()]
    ok("wrapper logs dry-run run_started", wrapper_lines[0]['event'] == 'run_started' and wrapper_lines[0]['dry_run'] is True)
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

print(f"\n{'='*48}\n  {passed} passed, {failed} failed\n{'='*48}")
sys.exit(1 if failed else 0)
