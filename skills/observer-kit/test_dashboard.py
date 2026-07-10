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
    dashboard.SOURCES['push'] = os.path.join(state, 'missing-push')
    dashboard.SOURCES['enrich'] = os.path.join(state, 'missing-enrich')
    dashboard.CHAT_FILE = os.path.join(state, 'chat.jsonl')
    dashboard.CONTROL_FILE = os.path.join(state, 'controls.jsonl')
    dashboard.EVENT_READ_BYTES = 80
    ledger = os.path.join(state, 'large-run.jsonl')
    rows = [
        {'ts': '2026-07-09T12:00:00', 'event': 'run_started', 'description': 'old dry-run attempt', 'todo': 1},
        {'ts': '2026-07-09T12:00:01', 'event': 'record', 'table': 'companies', 'key': 'old'},
        {'ts': '2026-07-09T12:01:00', 'event': 'run_started', 'description': 'latest large dashboard check', 'todo': 2},
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

    offsets = {}
    seen = []
    for _ in range(10):
        events, offsets = dashboard.read_events('runguard:large-run.jsonl', offsets)
        seen.extend(e.get('key') for e in events if e.get('event') == 'record')
        if list(offsets.values())[0] == os.path.getsize(ledger):
            break
    ok("chunked read keeps full JSONL records", seen == ['old', 'big', 'small'], str(seen))
    ok("chunked read advances beyond the large record", list(offsets.values())[0] > 80)
    ok("chunked reads eventually reach end", list(offsets.values())[0] == os.path.getsize(ledger))
    ok("reader reports whether a client should fetch the next chunk immediately",
       not dashboard.has_more_events('runguard:large-run.jsonl', offsets))

    runs = dashboard.list_runs()
    desc = next((r.get('desc') for r in runs if r.get('label') == 'large-run.jsonl'), '')
    ok("run list describes the latest attempt", desc == 'latest large dashboard check', desc)

    # The newest attempt can be surrounded by large record batches, so sidebar
    # metadata must not depend on a fixed head or tail window.
    long_summary = os.path.join(state, 'long-summary.jsonl')
    with open(long_summary, 'w', encoding='utf-8') as fh:
        fh.write('{"event":"run_started","description":"old attempt"}\n')
        for _ in range(30_000):
            fh.write(json.dumps({'event': 'record', 'padding': 'x' * 100}) + '\n')
        fh.write('{"event":"run_started","description":"latest attempt"}\n')
        for _ in range(30_000):
            fh.write(json.dumps({'event': 'record', 'padding': 'y' * 100}) + '\n')
    ok("run list description finds a latest attempt in a large ledger",
       dashboard._summary_event(long_summary).get('description') == 'latest attempt',
       str(dashboard._summary_event(long_summary)))
    with open(long_summary, 'a', encoding='utf-8') as fh:
        fh.write('{"event":"run_started","description":"incremental attempt"}\n')
    ok("run list summary incrementally sees a later attempt",
       dashboard._summary_event(long_summary).get('description') == 'incremental attempt',
       str(dashboard._summary_event(long_summary)))

    # A terminal event must win over a recent file mtime. Otherwise every
    # successful run is labelled "running" for ACTIVE_S seconds after it ends.
    finished = os.path.join(state, 'finished-run.jsonl')
    with open(finished, 'w', encoding='utf-8') as fh:
        fh.write('{"ts":"2026-07-09T12:03:00Z","event":"run_started"}\n')
        fh.write('{"ts":"2026-07-09T12:03:01Z","event":"run_finished"}\n')
    runs = dashboard.list_runs()
    finished_meta = next((r for r in runs if r.get('label') == 'finished-run.jsonl'), {})
    ok("terminal run is not marked live just because it is recent", not finished_meta.get('live'),
       str(finished_meta))

    for auxiliary in ('chat.jsonl', 'controls.jsonl', 'write-sheet.receipts.jsonl'):
        with open(os.path.join(state, auxiliary), 'w', encoding='utf-8') as fh:
            fh.write('{"event":"record"}\n')
    visible = {run.get('label') for run in dashboard.list_runs()}
    ok("sidebar excludes chat, controls, and write-receipt state files",
       not {'chat.jsonl', 'controls.jsonl', 'write-sheet.receipts.jsonl'} & visible, str(visible))

    # A tail read while the writer has not yet emitted its newline must retry the
    # line on the next poll instead of advancing into the middle of it.
    partial = os.path.join(state, 'partial-run.jsonl')
    first = b'{"ts":"2026-07-09T12:02:00Z","event":"record","table":"companies","key":"partial","company":"acme'
    second = b'.example"}\n'
    with open(partial, 'wb') as fh:
        fh.write(first)
    partial_events, partial_offsets = dashboard.read_events('runguard:partial-run.jsonl', {})
    ok("partial JSONL line is deferred", not partial_events and list(partial_offsets.values())[0] == 0,
       str(partial_offsets))
    with open(partial, 'ab') as fh:
        fh.write(second)
    partial_events, partial_offsets = dashboard.read_events('runguard:partial-run.jsonl', partial_offsets)
    ok("completed partial JSONL line arrives on next poll",
       [e.get('key') for e in partial_events] == ['partial'], str(partial_events))

    bad_offsets = [{ledger: 'not-a-number'}, {ledger: -1}, []]
    for offset in bad_offsets:
        offset_events, offset_result = dashboard.read_events('runguard:large-run.jsonl', offset)
        ok(f"invalid dashboard offset is reset safely ({type(offset).__name__})",
           bool(offset_events) and list(offset_result.values())[0] >= 0,
           str(offset_result))

    # History is a review surface, not a fixed-size recent window.
    for n in range(45):
        path = os.path.join(state, f'history-{n:02d}.jsonl')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write('{"ts":"2026-07-09T12:00:00Z","event":"run_started"}\n')
        os.utime(path, (1000 + n, 1000 + n))
    history = dashboard.list_runs()
    ok("run list keeps older history accessible", len(history) >= 47,
       f"visible={len(history)}")

    ok("record headers remain below the table controls and only reserve subtabs when present",
       '.recordshell th{top:41px}' in dashboard.PAGE and '.recordshell.hasSubtabs th{top:84px}' in dashboard.PAGE)
    ok("table tabs stay visible during horizontal inspection", '.subtabs{position:sticky;top:0;left:0;' in dashboard.PAGE)
    ok("record tables use explicit head and body sections", '<thead><tr>' in dashboard.PAGE and '<tbody>' in dashboard.PAGE)
    ok("record maps safely accept special table and key names", 'Object.create(null)' in dashboard.PAGE and 'function hasOwn(obj,key)' in dashboard.PAGE)
    ok("successful retries clear stale record errors", 'function clearResolvedError(row,event)' in dashboard.PAGE and 'clearResolvedError(r,e);' in dashboard.PAGE)
    ok("table tabs serialize special names safely", 'onclick="setRecTab(${esc(JSON.stringify(t))})"' in dashboard.PAGE)
    ok("checkpoints retain the latest measured progress", 'const measuredProgress=[...progressEvents()].reverse().find' in dashboard.PAGE)
    ok("same-mode retries retain prior record rows", 'function recordWindowStart()' in dashboard.PAGE)
    ok("progress uses the source table rather than every derived row",
       'const primaryTable=started.progress_table||started.table||flatRecords[0]?.table;' in dashboard.PAGE)
    ok("dashboard control requests use a separate durable input channel",
       '/api/control' in dashboard.PAGE and 'CONTROL_FILE' in open(RUN_DASHBOARD, encoding='utf-8').read())
    ok("control icons distinguish requested from acknowledged worker actions",
       'function controlStates()' in dashboard.PAGE and 'control_acknowledged' in dashboard.PAGE and
       "controlIcon(state.accepted?'accepted':kind)" in dashboard.PAGE)
    ok("run monitor separates messages from control transport and only exposes useful controls",
       "filter(m=>m.kind!=='control')" in dashboard.PAGE and
       'function controlAvailability()' in dashboard.PAGE and
       "summary.finished&&summary.dryRun" in dashboard.PAGE)
    ok("table inspection stays quiet until the operator Command-clicks to message the agent",
       "if(ev.metaKey||ev.ctrlKey){" in dashboard.PAGE and
       'Command/Ctrl-click = chat' in dashboard.PAGE and '[data-col]{cursor:default}' in dashboard.PAGE)
    ok("structured record values open as pretty JSON on one click",
       'function jsonCell(v)' in dashboard.PAGE and 'class=jsonOpen' in dashboard.PAGE and
       "JSON.stringify(JSON.parse(jsonTrigger.dataset.json),null,2)" in dashboard.PAGE and
       'Open full JSON' in dashboard.PAGE)
    ok("run monitor provides a direct conversation after a pause or stop",
       "function openRunChat()" in dashboard.PAGE and "openChat('run','Run'" in dashboard.PAGE and
       'Message agent' in dashboard.PAGE and "!e.target.closest('.bridgeActions')" in dashboard.PAGE)
    ok("pause and stop request worker control immediately and open chat for context",
       'function openControlChat(kind,label,prompt)' in dashboard.PAGE and
       'What should the agent know before ${control.prompt}?' in dashboard.PAGE and
       'await requestControl(kind);' in dashboard.PAGE and
       "openChat('run',label,document.getElementById('locks'),{label,prompt});" in dashboard.PAGE)
    ok("dashboard accepts concurrent browser/API requests", 'ThreadingHTTPServer' in open(RUN_DASHBOARD, encoding='utf-8').read())
    ok("data tables omit repeated ledger mechanics",
       "'attempt','dry_run','operation_key','payload_sha256'" in dashboard.PAGE)
    ok("generic tables show stable ordinals while freezing the identity column",
       'const ROW_NUMBER_W=54;' in dashboard.PAGE and 'ordinals[key]=index+1' in dashboard.PAGE and
       '.recordshell th.datafirst{left:54px' in dashboard.PAGE)
    ok("generic rows render their prior value after an in-place update",
       'const previous=row.__prev?.[c];' in dashboard.PAGE and 'was ${esc(fmt(previous))}' in dashboard.PAGE)
    ok("completed runs surface reported credit spend without custom dashboard wiring",
       "'sheet_rows_appended','credits_spent','errors'" in dashboard.PAGE)
    ok("selected summary metrics advance from live metric events",
       'metricValues=Object.create(null)' in dashboard.PAGE and
       "if(a==='metric'&&e.metric)" in dashboard.PAGE and
       'const summaryValues=fin||metricValues;' in dashboard.PAGE)
    ok("nested terminal outcome totals remain visible as bounded headline metrics",
       'function numericSummaryEntries(value' in dashboard.PAGE and
       'numericSummaryEntries(fin).slice(0,8)' in dashboard.PAGE and
       'numericSummaryEntries(e).slice(0,3)' in dashboard.PAGE and
       'const summaryStart=chips.length;' in dashboard.PAGE)
    ok("generic outcome rows count as landed source progress",
       "!['running','queued','pending'].includes(status)" in dashboard.PAGE and
       '`${completedProgress} / ${started.todo}`' in dashboard.PAGE)
    ok("observed source schemas are readable in the timeline",
       "case 'schema_observed'" in dashboard.PAGE and 'JSON field paths' in dashboard.PAGE)
    ok("live data updates preserve the operator's table position",
       'function captureTableScroll()' in dashboard.PAGE and 'function restoreTableScroll(state)' in dashboard.PAGE and
       'restoreTableScroll(tableScroll);' in dashboard.PAGE)
    ok("record tables provide typed multi-column filters",
       "function filterKind(rows,column)" in dashboard.PAGE and 'function rowsMatchFilters(rows, table)' in dashboard.PAGE and
       'does not contain' in dashboard.PAGE and 'greater than or equal to' in dashboard.PAGE and
       'between' in dashboard.PAGE and 'Filter columns' in dashboard.PAGE)
    ok("column filters support AND clauses with nested OR groups",
       'state.and.every(filter=>matches(row,filter))' in dashboard.PAGE and
       'state.groups.every(group=>group.filters.some(filter=>matches(row,filter)))' in dashboard.PAGE and
       'New OR group' in dashboard.PAGE and 'All filters (AND)' in dashboard.PAGE)
    ok("boolean columns expose only true and false operators",
       "return 'boolean'" in dashboard.PAGE and "[['true','is true'],['false','is false']]" in dashboard.PAGE and
       "kind==='boolean'" in dashboard.PAGE)
    ok("attention is an explicit record error contract, not a keyword heuristic",
       "function isAttentionRecord(r){" in dashboard.PAGE and "String(r.error).trim()!==''" in dashboard.PAGE and
       'ATTENTION_RE' not in dashboard.PAGE)
    ok("large fresh ledgers catch up without a two-second pause between chunks",
       "'more': has_more_events(run_id, new_offsets)" in open(RUN_DASHBOARD, encoding='utf-8').read() and
       'setTimeout(poll,more?0:2000);' in dashboard.PAGE)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
