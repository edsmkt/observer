#!/usr/bin/env python3
"""Acceptance tests for the Observer Kit liveness and durability tripwire.

The suite covers final record flushes, memory-only result accumulation, helper
indirection, multi-phase work loops, and accepted durable sink patterns.

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

# 3. Thread-pool completion persists and emits each completed result (exit 0).
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
            append_contact(vat, people)  # durable sink in the completion loop
            ledger('scope', 'record', table='contacts', key=str(vat))  # emitted from completion loop
main()
""")
ok("thread-pool completion persistence passes (exit 0)", rc == 0, f"rc={rc}; {out[:120]}")

# 4. Progress heartbeats cannot hide memory-only provider results.
rc, out, err = run_lint("""
from runguard import ledger
def main():
    todo = [{'id': 1}]
    results_by_vat = {}
    for company in todo:
        results_by_vat[company['id']] = fetch_paid_provider(company)
        ledger('scope', 'progress', phase='provider', done=1, total=1)
    for company in todo:
        ledger('scope', 'record', table='contacts', key=company['id'], **results_by_vat[company['id']])
main()
""")
ok("progress-only memory buffering is flagged", rc == 1, f"rc={rc}; {out[:160]}")
ok("durability failure is named", 'DURABILITY MISSING' in out, out[:160])

# 5. A nested append to the same results buffer is memory, not durability.
rc, out, err = run_lint("""
from runguard import ledger
def main():
    companies = [{'id': 1}]
    results_by_vat = {}
    for company in companies:
        for person in fetch_paid_provider(company):
            results_by_vat.setdefault(company['id'], []).append(person)
        ledger('scope', 'progress', phase='provider', done=1, total=1)
    for company in companies:
        ledger('scope', 'record', table='contacts', key=company['id'],
               contacts=results_by_vat[company['id']])
main()
""")
ok("same-buffer append is flagged as memory-only", rc == 1, f"rc={rc}; {out[:180]}")
ok("same-buffer append names durability", 'DURABILITY MISSING' in out, out[:180])

# 6. A helper that only mutates its buffer argument is still memory-only.
rc, out, err = run_lint("""
from runguard import ledger
def collect_result(buffer, key, value):
    buffer.setdefault(key, []).append(value)
def main():
    rows = [{'id': 1}]
    results = {}
    for row in rows:
        collect_result(results, row['id'], fetch_paid_provider(row))
        ledger('scope', 'progress', done=1, total=1)
    for row in rows:
        ledger('scope', 'record', table='records', key=row['id'], **results[row['id']])
main()
""")
ok("helper-hidden buffer append is flagged", rc == 1, f"rc={rc}; {out[:180]}")

# 7. Every provider loop is checked, including later loops after a valid one.
rc, out, err = run_lint("""
from runguard import ledger
def emit_row(key):
    ledger('scope', 'record', table='records', key=key)
def main():
    items = [{'id': 1}]
    companies = [{'id': 2}]
    first_results = {}
    for item in items:
        first_results[item['id']] = first_provider(item)
        persist_result(item['id'], first_results[item['id']])
        emit_row(item['id'])
    second_results = {}
    for company in companies:
        second_results[company['id']] = second_provider(company)
        emit_row(company['id'])
main()
""")
ok("later undurable provider loop is flagged", rc == 1, f"rc={rc}; {out[:180]}")

# 8. Direct file writes and explicit durable helpers remain accepted.
rc, out, err = run_lint("""
from runguard import ledger
def append_checkpoint(path, row):
    with open(path, 'a') as handle:
        handle.write(str(row) + '\\n')
def main():
    rows = [{'id': 1}]
    results = {}
    with open('direct.jsonl', 'a') as direct:
        for row in rows:
            results[row['id']] = transform(row)
            direct.write(str(results[row['id']]) + '\\n')
            append_checkpoint('checkpoint.jsonl', results[row['id']])
            ledger('scope', 'record', table='records', key=row['id'])
main()
""")
ok("real file writes and durable helpers pass", rc == 0, f"rc={rc}; {out[:180]}")

# 9. Database client writes remain accepted.
rc, out, err = run_lint("""
from runguard import ledger
def main(db):
    rows = [{'id': 1}]
    results = {}
    for row in rows:
        results[row['id']] = transform(row)
        db.upsert('records', results[row['id']])
        ledger('scope', 'record', table='records', key=row['id'])
main(database)
""")
ok("database upsert passes", rc == 0, f"rc={rc}; {out[:180]}")

# 10. Success remains an explicitly heuristic result.
ok("success message requires crash-resume proof",
   'No common buffered-output violation detected' in out and 'crash/resume' in out,
   out[:180])

# 11. No work or record events at all MUST pass (not our concern)
rc, out, err = run_lint("""
print('hello')
""")
ok("no-record script passes (exit 0)", rc == 0, f"rc={rc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
