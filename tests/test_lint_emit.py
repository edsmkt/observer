#!/usr/bin/env python3
"""Acceptance tests for the Observer Kit liveness and durability tripwire.

The suite covers final record flushes, memory-only result accumulation, helper
indirection, multi-phase work loops, and accepted durable sink patterns.

Run:  python3 test_lint_emit.py
Exits non-zero on any failure.
"""
import os, sys, tempfile, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LINT = os.path.join(REPO, 'observer_kit', 'lint_emit.py')
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
ok("buffered-then-flush message names the violation",
   'DURABILITY MISSING' in out or 'outside any work loop' in out, out[:160])

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
   'No common buffered-output' in out and 'ROW_LIVENESS_MISSING' in out and 'crash/resume' in out,
   out[:180])

# 11. No work or record events at all MUST pass (not our concern)
rc, out, err = run_lint("""
print('hello')
""")
ok("no-record script passes (exit 0)", rc == 0, f"rc={rc}")

# 12. A completed paid unit may persist after its nested pagination loops.
rc, out, err = run_lint("""
from runguard import ledger
def run(companies, durable):
    results_by_vat = {}
    for chunk in companies:
        chunk_hits = {}
        cursor = 'first'
        while cursor:
            response = fetch_paid_provider(chunk, cursor)
            for person in (response.get('results') or []):
                vat = person['vat_id']
                results_by_vat.setdefault(vat, []).append(person)
                chunk_hits.setdefault(vat, []).append(person)
            cursor = response.get('cursor')
        durable.persist('provider', [c['vat_id'] for c in chunk], chunk_hits)
        ledger('scope', 'record', table='provider_units', key=str(chunk[0]['vat_id']))
""")
ok("nested pagination accepts the enclosing unit's later durable write",
   rc == 0, f"rc={rc}; {out[:220]}")

# 13. A sink before nested work does not protect the result produced afterward.
rc, out, err = run_lint("""
def run(companies, durable):
    results_by_vat = {}
    for chunk in companies:
        durable.persist('provider', [], {})
        for page in fetch_pages(chunk):
            for person in (page.get('results') or []):
                results_by_vat.setdefault(person['vat_id'], []).append(person)
""")
ok("an enclosing sink before nested work remains a violation",
   rc == 1 and 'DURABILITY MISSING' in out, f"rc={rc}; {out[:220]}")

# 14. Replaying an append-only durable store is a read, not new paid work.
rc, out, err = run_lint("""
import json
class Durable:
    def __init__(self, path):
        self.path = path
    def load(self, results_by_vat):
        for line in open(self.path, encoding='utf-8'):
            record = json.loads(line)
            for vat, people in (record.get('hits') or {}).items():
                if vat in results_by_vat:
                    results_by_vat[vat].extend(people)
""")
ok("read-only durable replay does not require another durable write",
   rc == 0, f"rc={rc}; {out[:220]}")

# 15. Reading a file does not exempt fresh provider work nested inside it.
rc, out, err = run_lint("""
import json
def run(results_by_vat):
    for line in open('checkpoint.jsonl', encoding='utf-8'):
        record = json.loads(line)
        for company in (record.get('companies') or []):
            person = fetch_paid_provider(company)
            results_by_vat.setdefault(company['id'], []).append(person)
""")
ok("provider work inside a read loop still needs a durable boundary",
   rc == 1 and 'DURABILITY MISSING' in out, f"rc={rc}; {out[:220]}")

# 16. A final flush inside a surrounding context is still outside the unit loop.
rc, out, err = run_lint("""
def run(items):
    results = {}
    with open('output.jsonl', 'a') as sink:
        for item in items:
            results[item['id']] = fetch_paid_provider(item)
        for item in items:
            sink.write(str(results[item['id']]) + '\\n')
""")
ok("a context-wrapped final flush remains a violation",
   rc == 1 and 'DURABILITY MISSING' in out, f"rc={rc}; {out[:220]}")

# 17. Provider-yielded rows are fresh work even beneath a durable-store read.
rc, out, err = run_lint("""
import json
def run(results_by_vat):
    for line in open('checkpoint.jsonl', encoding='utf-8'):
        record = json.loads(line)
        for person in fetch_company_results(record):
            results_by_vat.setdefault(person['id'], []).append(person)
""")
ok("a provider iterator beneath a read loop still requires persistence",
   rc == 1 and 'DURABILITY MISSING' in out, f"rc={rc}; {out[:220]}")

# 18. Progress during discovery cannot defer every preview row to a final dump.
rc, out, err = run_lint("""
import runguard
def build_targets():
    targets = {}
    for page in source_pages:
        for row in fetch_page(page):
            targets[row['id']] = row
        runguard.ledger('scope', 'progress', phase='discover', read=len(targets))
    return targets
def main():
    targets = build_targets()
    for row in targets.values():
        runguard.ledger('scope', 'record', table='records', key=row['id'],
                        destination='planned')
    runguard.ledger('scope', 'run_finished')
""")
ok("progress with a terminal preview dump is flagged",
   rc == 1, f"rc={rc}; {out[:240]}")
ok("row-surface liveness failure is named",
   'ROW LIVENESS MISSING' in out, out[:240])

# 18b. run.count() heartbeats are not a substitute for live table rows.
rc, out, err = run_lint("""
from runguard import start_observed_run, ledger
def build_targets(run):
    targets = {}
    for page in source_pages:
        for row in fetch_page(page):
            targets[row['id']] = row
        run.count('pages_read')
    return targets
def main():
    run = start_observed_run(
        'scope', dry_run=True,
        summary_metrics=[{'key': 'pages_read', 'label': 'pages'}])
    targets = build_targets(run)
    for row in targets.values():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
    run.success()
""")
ok("count heartbeats with a terminal planned dump are flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18c. ledger metric events during discovery also require a row path.
rc, out, err = run_lint("""
from runguard import ledger
def build():
    targets = {}
    for page in pages:
        for row in fetch(page):
            targets[row['id']] = row
        ledger('s', 'metric', name='read', value=len(targets))
    return targets
def main():
    for row in build().values():
        ledger('s', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("metric heartbeats with a terminal planned dump are flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18d. Aliased run.count cannot hide a terminal planned dump.
rc, out, err = run_lint("""
from runguard import start_observed_run, ledger
def build_targets(run):
    beat = run.count
    targets = {}
    for page in source_pages:
        for row in fetch_page(page):
            targets[row['id']] = row
        beat('pages_read')
    return targets
def main():
    run = start_observed_run(
        'scope', dry_run=True,
        summary_metrics=[{'key': 'pages_read', 'label': 'pages'}])
    for row in build_targets(run).values():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
    run.success()
""")
ok("aliased count heartbeats with a terminal planned dump are flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18e. Imported-style tick() helper name is treated as a heartbeat.
rc, out, err = run_lint("""
from elsewhere import tick
from runguard import ledger
def build():
    targets = {}
    for page in pages:
        for row in fetch(page):
            targets[row['id']] = row
        tick(len(targets))
    return targets
def main():
    for row in build().values():
        ledger('s', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("unresolved tick() heartbeats with a terminal planned dump are flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18f. list.count must not be treated as a run.count heartbeat (false positive).
rc, out, err = run_lint("""
from runguard import ledger
def main():
    keys = []
    for page in pages:
        for row in fetch(page):
            keys.append(row['id'])
            n = keys.count(row['id'])
        ledger('s', 'record', table='companies', key=row['id'], status='ok')
""")
ok("list.count does not false-positive as a heartbeat",
   rc == 0, f"rc={rc}; {out[:280]}")

# 18g. getattr(run, 'count') must not hide a terminal planned dump.
rc, out, err = run_lint("""
from runguard import start_observed_run, ledger
def build_targets(run):
    targets = {}
    for page in source_pages:
        for row in fetch_page(page):
            targets[row['id']] = row
        getattr(run, 'count')('pages_read')
    return targets
def main():
    run = start_observed_run(
        'scope', dry_run=True,
        summary_metrics=[{'key': 'pages_read', 'label': 'pages'}])
    for row in build_targets(run).values():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
    run.success()
""")
ok("getattr count heartbeats with a terminal planned dump are flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18h. Lambda-bound run.progress must not hide a terminal planned dump.
rc, out, err = run_lint("""
from runguard import start_observed_run, ledger
def build_targets(run):
    refresh = lambda: run.progress(phase='discover', done=1)
    targets = {}
    for page in source_pages:
        for row in fetch_page(page):
            targets[row['id']] = row
        refresh()
    return targets
def main():
    run = start_observed_run(
        'scope', dry_run=True,
        summary_metrics=[{'key': 'x', 'label': 'x'}])
    for row in build_targets(run).values():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
    run.success()
""")
ok("lambda-bound progress heartbeats with a terminal planned dump are flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18i. Silent discovery (no heartbeats) then terminal planned dump is flagged.
rc, out, err = run_lint("""
from runguard import ledger
def build_targets():
    targets = {}
    for page in source_pages:
        for row in fetch_page(page):
            targets[row['id']] = row
    return targets
def main():
    for row in build_targets().values():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("silent discovery with a terminal planned dump is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18j. Attribute-bound heartbeat alias (h.beat = run.progress).
rc, out, err = run_lint("""
from runguard import start_observed_run, ledger
def build(run):
    h = type('H', (), {})()
    h.beat = run.progress
    found = []
    for page in source_pages:
        found = found + list(fetch_page(page))
        h.beat(phase='discover', done=len(found))
    return found
def main():
    run = start_observed_run(
        'scope', dry_run=True,
        summary_metrics=[{'key': 'x', 'label': 'x'}])
    for row in build(run):
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
    run.success()
""")
ok("attribute-aliased progress heartbeats with terminal dump are flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18k. Dict self-merge accumulation without subscript/update.
rc, out, err = run_lint("""
from runguard import ledger
def build():
    t = {}
    for page in source_pages:
        chunk = {row['id']: row for row in fetch_page(page)}
        t = {**t, **chunk}
    return t
def main():
    for key, row in build().items():
        ledger('scope', 'record', table='companies', key=key,
               destination='planned')
""")
ok("dict self-merge silent discovery with terminal dump is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18l. AugAssign list growth (targets += [row]) then terminal dump.
rc, out, err = run_lint("""
from runguard import ledger
def build():
    targets = []
    for page in source_pages:
        for row in fetch_page(page):
            targets += [row]
    return targets
def main():
    for row in build():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("augassign list growth silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18m. Helper-mediated append on a non-result-named buffer.
rc, out, err = run_lint("""
from runguard import ledger
def collect(buf, row):
    buf.append(row)
def build():
    companies = []
    for page in source_pages:
        for row in fetch_page(page):
            collect(companies, row)
    return companies
def main():
    for row in build():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("helper-mediated append silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18n. Multi-source list comprehension builder then terminal dump.
rc, out, err = run_lint("""
from runguard import ledger
def build():
    return [row for page in source_pages for row in fetch_page(page)]
def main():
    for row in build():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("multi-source listcomp silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18o. Nested helper + free-global buffer (results not a parameter).
rc, out, err = run_lint("""
from runguard import ledger
results = []
def collect(buf, row):
    buf.append(row)
def build(item):
    collect(results, item)
def main():
    for page in source_pages:
        for row in fetch_page(page):
            build(row)
    for row in results:
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("nested helper free-global buffer silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18p. ThreadPoolExecutor.map silent accumulation then terminal dump.
rc, out, err = run_lint("""
from concurrent.futures import ThreadPoolExecutor
from runguard import ledger
results = []
def worker(item):
    results.append(item)
def main():
    with ThreadPoolExecutor() as ex:
        list(ex.map(worker, source_items))
    for row in results:
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("thread-pool map silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18q. Bound-method alias add = results.append; add(row).
rc, out, err = run_lint("""
from runguard import ledger
def build():
    results = []
    add = results.append
    for page in source_pages:
        for row in fetch_page(page):
            add(row)
    return results
def main():
    for row in build():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("bound-method append alias silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18r. Multi-source listcomp builder + module-level terminal flush.
rc, out, err = run_lint("""
from runguard import ledger
def build():
    return [row for page in source_pages for row in fetch_page(page)]
rows = build()
for row in rows:
    ledger('scope', 'record', table='companies', key=row['id'],
           destination='planned')
""")
ok("listcomp with module-level flush silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18s. functools.reduce fold then terminal dump.
rc, out, err = run_lint("""
from functools import reduce
from runguard import ledger
def add_page(acc, page):
    acc.extend(fetch_page(page))
    return acc
def main():
    results = reduce(add_page, source_pages, [])
    for row in results:
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("reduce fold silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18t. chain.from_iterable materialize then terminal dump.
rc, out, err = run_lint("""
from itertools import chain
from runguard import ledger
def main():
    results = list(chain.from_iterable(fetch_page(p) for p in source_pages))
    for row in results:
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("chain.from_iterable silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18u. Single-generator map-comp over source_items then dump.
rc, out, err = run_lint("""
from runguard import ledger
def build():
    return [transform(row) for row in source_items]
def main():
    for row in build():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("single-gen source map-comp silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18v. list(map(...)) materialize then terminal dump.
rc, out, err = run_lint("""
from runguard import ledger
def transform(row):
    return row
def main():
    results = list(map(transform, source_items))
    for row in results:
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("list(map) materialize silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18w. list(starmap(...)) materialize then terminal dump.
rc, out, err = run_lint("""
from itertools import starmap
from runguard import ledger
def transform(a, b):
    return {'id': a, 'v': b}
def main():
    results = list(starmap(transform, source_pairs))
    for row in results:
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("list(starmap) materialize silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18x. list(genexp) materialize then terminal dump.
rc, out, err = run_lint("""
from runguard import ledger
def worker(x):
    return x
def main():
    results = list(worker(x) for x in source_items)
    for row in results:
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("list(genexp) materialize silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18y. [*map(...)] starred list materialize then dump.
rc, out, err = run_lint("""
from runguard import ledger
def worker(x):
    return x
def main():
    results = [*map(worker, source_items)]
    for row in results:
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("starred-map list materialize silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18z. dict(genexp) materialize then terminal dump.
rc, out, err = run_lint("""
from runguard import ledger
def main():
    results = dict((row['id'], row) for page in source_pages
                   for row in fetch_page(page))
    for key, row in results.items():
        ledger('scope', 'record', table='companies', key=key,
               destination='planned')
""")
ok("dict(genexp) materialize silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18aa. Helper returns list(genexp) then caller dumps.
rc, out, err = run_lint("""
from runguard import ledger
def worker(x):
    return x
def build():
    return list(worker(x) for x in source_items)
def main():
    for row in build():
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("helper-returned list(genexp) silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18ab. list(source_items) bare-name materialize then dump.
rc, out, err = run_lint("""
from runguard import ledger
def main():
    results = list(source_items)
    for row in results:
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("list(source_items) materialize silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 18ac. list(fetch_all()) source-fetch materialize then dump.
rc, out, err = run_lint("""
from runguard import ledger
def main():
    results = list(fetch_all())
    for row in results:
        ledger('scope', 'record', table='companies', key=row['id'],
               destination='planned')
""")
ok("list(fetch_all) materialize silent discovery is flagged",
   rc == 1 and 'ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:280]}")

# 19. Qualified ledger calls pass when progress and stable rows advance together.
rc, out, err = run_lint("""
import runguard
def run():
    for page in source_pages:
        for row in fetch_page(page):
            runguard.ledger('scope', 'record', table='records', key=row['id'],
                            destination='discovered')
        runguard.ledger('scope', 'progress', phase='discover')
""")
ok("qualified ledger progress plus incremental rows passes",
   rc == 0, f"rc={rc}; {out[:240]}")

# 20. A slow phase can update one stable phase row while it discovers entities.
rc, out, err = run_lint("""
from runguard import ledger
def run():
    has_more = True
    while has_more:
        has_more = fetch_page()
        ledger('scope', 'progress', phase='download')
        ledger('scope', 'record', table='phases', key='download', status='running')
""")
ok("a stable phase row satisfies table liveness",
   rc == 0, f"rc={rc}; {out[:240]}")

# 21. Local helpers preserve the same progress-plus-record contract.
rc, out, err = run_lint("""
from runguard import ledger
def emit_progress(done):
    ledger('scope', 'progress', phase='discover', done=done)
def emit_row(row):
    ledger('scope', 'record', table='records', key=row['id'])
def run():
    for row in source_rows:
        emit_progress(row['id'])
        emit_row(row)
""")
ok("helper-mediated progress plus rows passes",
   rc == 0, f"rc={rc}; {out[:240]}")

# 22. A durable helper may emit its record through a stored ledger callback.
rc, out, err = run_lint("""
from runguard import ledger
class Durable:
    def __init__(self, callback):
        self._ledger = callback
    def persist(self, row):
        append_checkpoint(row)
        self._ledger('scope', 'record', table='provider_units', key=row['id'])
def run(durable):
    for row in source_rows:
        fetch_paid_provider(row)
        durable.persist(row)
        ledger('scope', 'progress', phase='provider')
""")
ok("stored ledger callbacks preserve batch-row liveness",
   rc == 0, f"rc={rc}; {out[:240]}")

# 23. An explicit run start selects the small headline surface the operator sees.
rc, out, err = run_lint("""
from runguard import start_observed_run
run = start_observed_run('backfill', source='crm:companies')
run.success(companies={'write': 12, 'held': 2})
""")
ok("run start without headline metrics is flagged",
   rc == 1 and 'SUMMARY METRICS MISSING' in out, f"rc={rc}; {out[:260]}")

# 24. A declared summary contract remains valid for wrapper and raw-ledger starts.
rc, out, err = run_lint("""
from runguard import start_observed_run
run = start_observed_run(
    'backfill', source='crm:companies',
    summary_metrics=['companies_to_write', 'companies_held'],
)
run.success(companies_to_write=12, companies_held=2)
""")
ok("wrapper summary metrics pass",
   rc == 0, f"rc={rc}; {out[:240]}")

rc, out, err = run_lint("""
from runguard import ledger
ledger('backfill', 'run_started', summary_metrics=['companies_to_write'])
ledger('backfill', 'run_finished', companies_to_write=12)
""")
ok("raw-ledger summary metrics pass",
   rc == 0, f"rc={rc}; {out[:240]}")

# 25. Phase rows cannot stand in for business entities already discovered.
rc, out, err = run_lint("""
from runguard import ledger
def phase_row(done):
    ledger('scope', 'record', table='phase', key='discover', done=done)
def discover():
    companies = {}
    for page in source_pages:
        for row in fetch_page(page):
            companies[row['id']] = row
        ledger('scope', 'progress', phase='discover', done=len(companies))
        phase_row(len(companies))
    return companies
def preview(companies):
    for row in companies.values():
        ledger('scope', 'record', table='companies', key=row['id'])
""")
ok("entity discovery with only phase rows is flagged",
   rc == 1 and 'BUSINESS ROW LIVENESS MISSING' in out, f"rc={rc}; {out[:300]}")

# 26. A dry-run limit must bound source work rather than only preview output.
rc, out, err = run_lint("""
import argparse
from runguard import ledger
ap = argparse.ArgumentParser()
ap.add_argument('--dry-run', action='store_true')
ap.add_argument('--limit', type=int)
args = ap.parse_args()
companies = fetch_all_companies()
for row in companies[:args.limit]:
    ledger('scope', 'record', table='companies', key=row['id'])
""")
ok("terminal-only sample limit is flagged",
   rc == 1 and 'SAMPLE LIMIT LATE' in out, f"rc={rc}; {out[:300]}")

rc, out, err = run_lint("""
import argparse
from runguard import ledger
ap = argparse.ArgumentParser()
ap.add_argument('--dry-run', action='store_true')
ap.add_argument('--limit', type=int)
args = ap.parse_args()
for row in fetch_companies(limit=args.limit):
    ledger('scope', 'record', table='companies', key=row['id'])
""")
ok("source-bounded sample limit passes", rc == 0, f"rc={rc}; {out[:260]}")

# 27. A canary stays visible before, during, and after its mutation.
rc, out, err = run_lint("""
from runguard import ledger
def canary(row):
    patch_company(row['id'], {'country': 'Germany'})
    verify_company(row['id'])
    ledger('scope', 'record', table='companies', key=row['id'], status='verified')
""")
ok("canary mutation before its first row is flagged",
   rc == 1 and 'CANARY VISIBILITY MISSING' in out, f"rc={rc}; {out[:300]}")

rc, out, err = run_lint("""
from runguard import ledger
def canary(row):
    ledger('scope', 'record', table='companies', key=row['id'], status='writing')
    patch_company(row['id'], {'country': 'Germany'})
    ledger('scope', 'record', table='companies', key=row['id'], status='verifying')
    verify_company(row['id'])
    ledger('scope', 'record', table='companies', key=row['id'], status='verified')
""")
ok("visible canary transition passes", rc == 0, f"rc={rc}; {out[:260]}")

# 28. Free-form chat remains agent input; workers consume structured controls.
rc, out, err = run_lint("""
import runguard
def run(run_id):
    for message in runguard.read_chat(run_id=run_id, author='user'):
        if 'stop' in message.get('text', '').lower():
            break
""")
ok("worker chat used as control is flagged",
   rc == 1 and 'CHAT CONTROL MISUSE' in out, f"rc={rc}; {out[:300]}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
