#!/usr/bin/env python3
"""Acceptance tests for references/lint_emit.py — the observer-kit guardrail that
blocks the #1 live-visibility bug: buffering provider results in memory and
emitting `record` ledger rows only at the final flush.

Run:  python3 test_lint_emit.py
Exits non-zero on any failure.
"""
import os, sys, tempfile, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
LINT = os.path.join(HERE, 'references', 'lint_emit.py')
passed, failed = 0, 0


def ok(name, cond, detail=''):
    global passed, failed
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  — {detail}" if detail and not cond else ''))
    if cond:
        passed += 1
    else:
        failed += 1


def run_lint(code):
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        f.write(code)
        path = f.name
    try:
        p = subprocess.run([sys.executable, LINT, path], capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout, p.stderr
    finally:
        os.unlink(path)


print(f"Testing lint_emit.py at {LINT}\n")

# 1. Buffered-then-flush MUST be caught (exit 1)
rc, out, err = run_lint("""
from runguard import ledger
def main():
    todo = [{'id': 1, 'name': 'x'}]
    results = {}
    for c in todo:
        results[c['id']] = {'name': c['name']}      # buffered in memory
    for c in todo:                                    # flush only at the end
        ledger('scope', 'record', table='contacts', key=c['id'], **results[c['id']])
main()
""")
ok("buffered-then-flush is flagged (exit 1)", rc == 1, f"rc={rc}")
ok("buffered-then-flush message names the violation", 'BUFFERED' in out or 'outside any work loop' in out, out[:120])

# 2. Emit-inside-loop MUST pass (exit 0)
rc, out, err = run_lint("""
from runguard import ledger
def main():
    todo = [{'id': 1, 'name': 'x'}]
    for c in todo:
        ledger('scope', 'record', table='contacts', key=c['id'], name=c['name'])  # live
main()
""")
ok("emit-inside-loop passes (exit 0)", rc == 0, f"rc={rc}; {out[:120]}")

# 3. Thread-pool completion emit MUST pass (exit 0) — the source_contacts.py shape
rc, out, err = run_lint("""
from runguard import ledger
from concurrent.futures import as_completed, ThreadPoolExecutor
def main():
    todo = [{'id': 1}]
    results = {}
    with ThreadPoolExecutor() as ex:
        futs = [ex.submit(lambda c: (c['id'], [1,2])) for c in todo]
        for f in as_completed(futs):
            vat, people = f.result()
            results[vat] = people
            ledger('scope', 'record', table='contacts', key=str(vat))  # emitted from completion loop
main()
""")
ok("thread-pool completion emit passes (exit 0)", rc == 0, f"rc={rc}; {out[:120]}")

# 4. No record emits at all MUST pass (not our concern)
rc, out, err = run_lint("""
print('hello')
""")
ok("no-record script passes (exit 0)", rc == 0, f"rc={rc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
