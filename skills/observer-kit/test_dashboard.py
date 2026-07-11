#!/usr/bin/env python3
"""Acceptance tests for run_dashboard.py JSONL reading behavior."""
import importlib.util
import json
import os
import tempfile
import threading
from urllib.request import urlopen


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
DASHBOARD_SOURCE = dashboard.PAGE + '\n' + dashboard.DASHBOARD_JS

print(f"Testing run_dashboard.py at {RUN_DASHBOARD}\n")

with tempfile.TemporaryDirectory(prefix='rgdash-') as state:
    dashboard.SOURCES['runguard'] = state
    dashboard.SOURCES['push'] = os.path.join(state, 'missing-push')
    dashboard.CHAT_FILE = os.path.join(state, 'chat.jsonl')
    dashboard.CONTROL_FILE = os.path.join(state, 'controls.jsonl')
    dashboard.EVENT_READ_BYTES = 80
    server = dashboard.ThreadingHTTPServer(('127.0.0.1', 0), dashboard.Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        host, port = server.server_address
        with urlopen(f'http://{host}:{port}/', timeout=3) as response:
            page_body = response.read().decode('utf-8')
        with urlopen(f'http://{host}:{port}/assets/dashboard.js', timeout=3) as response:
            js_body = response.read().decode('utf-8')
            js_type = response.headers.get_content_type()
        ok("dashboard HTML loads its external JavaScript asset",
           '<script src="/assets/dashboard.js"></script>' in page_body and '<script>' not in page_body)
        ok("dashboard serves the complete JavaScript asset",
           js_type == 'application/javascript' and js_body == dashboard.DASHBOARD_JS)
        ok("standalone JavaScript does not retain Python-string double escapes",
           '\\\\' not in dashboard.DASHBOARD_JS)
        legacy_adapters = (
            'bc_submitted', 'bc_credits', 'bc_poll_timeout', 'phone_found',
            'phone_not_found', 'email_found', 'email_not_found', 'enrichRun',
            'CRM id', 'phones found', 'emails found', 'saas_true',
            'emails_enriched', 'sheet_rows_appended', 'with_contacts',
            'no_contacts', 'total_contacts', 'CRM:', 'POST /companies',
            'POST /contacts', 'COLW_DEFAULT', 'linkedin_status',
            'contact_status',
        )
        ok("dashboard has one domain-generic record contract",
           all(term not in DASHBOARD_SOURCE for term in legacy_adapters),
           ', '.join(term for term in legacy_adapters if term in DASHBOARD_SOURCE))
        ok("run identifiers use escaped data attributes and bound listeners",
           'data-run-id="${esc(r.id)}"' in dashboard.DASHBOARD_JS and
           "element.addEventListener('click',()=>pick(element.dataset.runId))" in dashboard.DASHBOARD_JS and
           'onclick="pick(' not in dashboard.DASHBOARD_JS)
        ok("flow unit counts backfill omitted aggregate fields",
           'reportedCounts' in dashboard.DASHBOARD_JS and
           'state.derived_counts' not in dashboard.DASHBOARD_JS)
        ok("run descriptions use generic source-item language",
           dashboard._describe({'todo': 12}) == '12 items' and
           'contact-enrichment' not in dashboard.__doc__ and
           'enrich_runs' not in open(RUN_DASHBOARD, encoding='utf-8').read())
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)
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

    output_path = os.path.join(state, 'durable-output.jsonl')
    with open(output_path, 'w', encoding='utf-8') as fh:
        fh.write('{"id":"row-1","status":"saved"}\n')
    visible_ids = {run['id'] for run in dashboard.list_runs()}
    ok("durable JSONL outputs do not appear as dashboard runs",
       'runguard:durable-output.jsonl' not in visible_ids)

    ok("record headers remain below the table controls and only reserve subtabs when present",
       '.recordshell th{top:41px}' in DASHBOARD_SOURCE and '.recordshell.hasSubtabs th{top:84px}' in DASHBOARD_SOURCE)
    ok("table controls mask rows after horizontal scrolling",
       '.tableTools{position:sticky;top:0;left:0;' in DASHBOARD_SOURCE and
       '.filterPanel{position:sticky;top:41px;left:0;' in DASHBOARD_SOURCE)
    ok("table tabs stay visible during horizontal inspection", '.subtabs{position:sticky;top:0;left:0;' in DASHBOARD_SOURCE)
    ok("record tables use explicit head and body sections", '<thead><tr>' in DASHBOARD_SOURCE and '<tbody>' in DASHBOARD_SOURCE)
    ok("record maps safely accept special table and key names", 'Object.create(null)' in DASHBOARD_SOURCE and 'function hasOwn(obj,key)' in DASHBOARD_SOURCE)
    ok("successful retries clear stale record errors", 'function clearResolvedError(row,event)' in DASHBOARD_SOURCE and 'clearResolvedError(r,e);' in DASHBOARD_SOURCE)
    ok("table tabs serialize special names safely", 'onclick="setRecTab(${esc(JSON.stringify(t))})"' in DASHBOARD_SOURCE)
    ok("checkpoints retain the latest measured progress", 'const measuredProgress=[...progressEvents()].reverse().find' in DASHBOARD_SOURCE)
    ok("same-mode retries retain prior record rows", 'function recordWindowStart()' in DASHBOARD_SOURCE)
    ok("progress uses the source table rather than every derived row",
       'const primaryTable=started.progress_table||started.table||flatRecords[0]?.table;' in DASHBOARD_SOURCE)
    ok("dashboard control requests use a separate durable input channel",
       '/api/control' in DASHBOARD_SOURCE and 'CONTROL_FILE' in open(RUN_DASHBOARD, encoding='utf-8').read())
    ok("control icons distinguish requested from acknowledged worker actions",
       'function controlStates()' in DASHBOARD_SOURCE and 'control_acknowledged' in DASHBOARD_SOURCE and
       "controlIcon(state.accepted?'accepted':kind)" in DASHBOARD_SOURCE)
    ok("run monitor separates messages from control transport and only exposes useful controls",
       "filter(m=>m.kind!=='control')" in DASHBOARD_SOURCE and
       'function controlAvailability()' in DASHBOARD_SOURCE and
       "summary.finished&&summary.dryRun" in DASHBOARD_SOURCE)
    ok("table inspection stays quiet until the operator Command-clicks to message the agent",
       "if(ev.metaKey||ev.ctrlKey){" in DASHBOARD_SOURCE and
       'Command/Ctrl-click = chat' in DASHBOARD_SOURCE and '[data-col]{cursor:default}' in DASHBOARD_SOURCE)
    ok("structured record values open as pretty JSON on one click",
       'function jsonCell(v)' in DASHBOARD_SOURCE and 'class=jsonOpen' in DASHBOARD_SOURCE and
       "JSON.stringify(JSON.parse(jsonTrigger.dataset.json),null,2)" in DASHBOARD_SOURCE and
       'Open full JSON' in DASHBOARD_SOURCE)
    ok("run monitor provides a direct conversation after a pause or stop",
       "function openRunChat()" in DASHBOARD_SOURCE and "openChat('run','Run'" in DASHBOARD_SOURCE and
       'Message agent' in DASHBOARD_SOURCE and "!e.target.closest('.bridgeActions')" in DASHBOARD_SOURCE)
    ok("pause and stop request worker control immediately and open chat for context",
       'function openControlChat(kind,label,prompt)' in DASHBOARD_SOURCE and
       'What should the agent know before ${control.prompt}?' in DASHBOARD_SOURCE and
       'await requestControl(kind);' in DASHBOARD_SOURCE and
       "openChat('run',label,document.getElementById('locks'),{label,prompt});" in DASHBOARD_SOURCE)
    ok("dashboard accepts concurrent browser/API requests", 'ThreadingHTTPServer' in open(RUN_DASHBOARD, encoding='utf-8').read())
    ok("data tables omit repeated ledger mechanics",
       "'attempt','dry_run','operation_key','payload_sha256'" in DASHBOARD_SOURCE)
    ok("generic tables show stable ordinals while freezing the identity column",
       'const ROW_NUMBER_W=54;' in DASHBOARD_SOURCE and 'ordinals[key]=index+1' in DASHBOARD_SOURCE and
       '.recordshell th.datafirst{left:54px' in DASHBOARD_SOURCE)
    ok("generic rows render their prior value after an in-place update",
       'const previous=row.__prev?.[c];' in DASHBOARD_SOURCE and 'was ${esc(fmt(previous))}' in DASHBOARD_SOURCE)
    ok("completed runs surface numeric outcomes without domain-specific defaults",
       'numericSummaryEntries(fin).slice(0,8)' in DASHBOARD_SOURCE)
    ok("selected summary metrics advance from live metric events",
       'metricValues=Object.create(null)' in DASHBOARD_SOURCE and
       "if(a==='metric'&&e.metric)" in DASHBOARD_SOURCE and
       'const summaryValues=fin||metricValues;' in DASHBOARD_SOURCE)
    ok("nested terminal outcome totals remain visible as bounded headline metrics",
       'function numericSummaryEntries(value' in DASHBOARD_SOURCE and
       'numericSummaryEntries(fin).slice(0,8)' in DASHBOARD_SOURCE and
       'numericSummaryEntries(e).slice(0,3)' in DASHBOARD_SOURCE and
       'const summaryStart=chips.length;' in DASHBOARD_SOURCE)
    ok("generic outcome rows count as landed source progress",
       "!['running','queued','pending'].includes(status)" in DASHBOARD_SOURCE and
       '`${completedProgress} / ${started.todo}`' in DASHBOARD_SOURCE)
    ok("observed source schemas are readable in the timeline",
       "case 'schema_observed'" in DASHBOARD_SOURCE and 'JSON field paths' in DASHBOARD_SOURCE)
    ok("live data updates preserve the operator's table position",
       'function captureTableScroll()' in DASHBOARD_SOURCE and 'function restoreTableScroll(state)' in DASHBOARD_SOURCE and
       'const latestScroll=captureTableScroll();' in DASHBOARD_SOURCE and
       'content.replaceChildren(shell);' in DASHBOARD_SOURCE and
       'restoreTableScroll(latestScroll);' in DASHBOARD_SOURCE and
       'if(html!==null){' in DASHBOARD_SOURCE)
    ok("large table refreshes keep the current table until the replacement is complete",
       DASHBOARD_SOURCE.index('const latestScroll=captureTableScroll();') < DASHBOARD_SOURCE.index('content.replaceChildren(shell);') and
       DASHBOARD_SOURCE.index('content.replaceChildren(shell);') < DASHBOARD_SOURCE.index('restoreTableScroll(latestScroll);'))
    ok("record tables provide typed multi-column filters",
       "function filterKind(rows,column)" in DASHBOARD_SOURCE and 'function rowsMatchFilters(rows, table)' in DASHBOARD_SOURCE and
       'does not contain' in DASHBOARD_SOURCE and 'greater than or equal to' in DASHBOARD_SOURCE and
       'between' in DASHBOARD_SOURCE and 'Filter columns' in DASHBOARD_SOURCE)
    ok("column filters support AND clauses with nested OR groups",
       'state.and.every(filter=>matches(row,filter))' in DASHBOARD_SOURCE and
       'state.groups.every(group=>group.filters.some(filter=>matches(row,filter)))' in DASHBOARD_SOURCE and
       'New OR group' in DASHBOARD_SOURCE and 'All filters (AND)' in DASHBOARD_SOURCE)
    ok("boolean columns expose only true and false operators",
       "return 'boolean'" in DASHBOARD_SOURCE and "[['true','is true'],['false','is false']]" in DASHBOARD_SOURCE and
       "kind==='boolean'" in DASHBOARD_SOURCE)
    ok("attention is an explicit record error contract, not a keyword heuristic",
       "function isAttentionRecord(r){" in DASHBOARD_SOURCE and "String(r.error).trim()!==''" in DASHBOARD_SOURCE and
       'ATTENTION_RE' not in DASHBOARD_SOURCE)
    ok("large fresh ledgers catch up without a two-second pause between chunks",
       "'more': has_more_events(run_id, new_offsets)" in open(RUN_DASHBOARD, encoding='utf-8').read() and
       'setTimeout(poll,more?0:2000);' in DASHBOARD_SOURCE)
    ok("flow runs reveal a dedicated visual graph tab",
       'id=tabFlow style="display:none"' in DASHBOARD_SOURCE and
       "eventName(e)==='flow_graph'" in DASHBOARD_SOURCE and
       "document.getElementById('tabFlow').style.display=hasFlow?'block':'none'" in DASHBOARD_SOURCE)
    ok("flow graph state is reconstructed from explicit graph node and unit events",
       "kind==='flow_node'" in DASHBOARD_SOURCE and "kind==='flow_unit'" in DASHBOARD_SOURCE and
       'function flowModel()' in DASHBOARD_SOURCE and 'function renderFlow(viewScroll)' in DASHBOARD_SOURCE and
       "['complete','completed','done','finished'" in DASHBOARD_SOURCE)
    ok("flow node cards expose universal outcomes instead of ambiguous routing language",
       all(label in DASHBOARD_SOURCE for label in ('succeeded</small>', 'skipped</small>', 'held</small>', 'failed</small>')) and
       'diverted</small>' not in DASHBOARD_SOURCE)
    ok("batch flow events remain row-oriented while exposing bounded request activity",
       "case 'flow_batch'" in DASHBOARD_SOURCE and "kind==='flow_batch'" in DASHBOARD_SOURCE and
       'Batch calls · ${selectedBatches.length}' in DASHBOARD_SOURCE and 'saved_units' in DASHBOARD_SOURCE and
       'reused response' in DASHBOARD_SOURCE)
    ok("flow row totals come from landed business records instead of active-node membership",
       'const businessKeys=new Set(recordEvents()' in DASHBOARD_SOURCE and
       'observedRows=Math.max(allKeys.size,businessKeys.size)' in DASHBOARD_SOURCE and
       '<b>${rowMetric}</b><small>rows observed</small>' in DASHBOARD_SOURCE)
    ok("flow view exposes live nodes branches and per-row traces",
       'class=flowGraph id=flowGraph' in DASHBOARD_SOURCE and 'function drawFlowEdges()' in DASHBOARD_SOURCE and
       'Rows at this node' in DASHBOARD_SOURCE and 'Latest durable path for this row' in DASHBOARD_SOURCE)
    ok("flow definitions and current row projections remain inspectable as JSON",
       'Inspect node JSON' in DASHBOARD_SOURCE and 'Inspect row JSON' in DASHBOARD_SOURCE and
       'function showFlowJson(title,value)' in DASHBOARD_SOURCE)
    ok("live flow updates preserve the operator's vertical position",
       "const flowScroll=view==='flow'?content.scrollTop:null;" in DASHBOARD_SOURCE and
       'if(viewScroll!==null&&viewScroll!==undefined)content.scrollTop=viewScroll;' in DASHBOARD_SOURCE)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
