#!/usr/bin/env python3
"""Local run observer — a SAMPLE live dashboard for any enrichment / batch run.

Zero-intrusion: reads the append-only JSONL ledgers your run scripts write (see
runguard.py) and renders them as a live per-record table + plain-English
timeline. Read-only — it can observe a run but never affect one.

This file is a starting point, not a fixed product. The table columns and the
humanize() event map are tuned for a contact-enrichment example (phone / email /
CRM id); keep them, or remap them to whatever your workflow logs — the
guard / ledger / observer machinery works with any events.

ADAPT HERE: point SOURCES at your project's ledger directories.
  - 'runguard' style: a flat dir of <timestamp>-<scope>.jsonl ledger files
    (what runguard.ledger() writes). Locks (*.lock) in the same dirs show in
    the "who is writing right now" panel.
  - 'push' style (optional): a dir of per-run SUBDIRECTORIES each containing
    events.jsonl (+ api-calls.jsonl) — delete the entry if you don't have this.

Usage:  python3 run_dashboard.py   (then open http://localhost:8484)
Stdlib only. Localhost only.
"""
import json
import os
import re
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# This is a run-once GLOBAL observer, not a per-project file to vendor. Point it at
# any project's ledger dir — no editing needed:
#     python3 run_dashboard.py /path/to/.runguard          (positional arg)
#     RUNGUARD_STATE_DIR=/path/to/.runguard python3 run_dashboard.py   (env)
#     python3 run_dashboard.py <dir> --port 8485
# It's read-only; one instance can observe whatever ledger dir you give it.
BASE = os.path.dirname(os.path.abspath(__file__))
_arg_dir = next((a for a in sys.argv[1:] if not a.startswith('-')), None)
_runguard_dir = _arg_dir or os.environ.get('RUNGUARD_STATE_DIR') or os.path.join(BASE, '.runguard')
SOURCES = {
    'runguard': _runguard_dir,                          # runguard ledgers + locks
    'push': os.path.join(BASE, 'runs'),                 # per-run subdirs (optional)
    'enrich': os.path.join(BASE, 'enrich_runs'),        # flat jsonl dir (optional)
}
PORT = int(os.environ.get('OBSERVER_PORT', '8484'))
if '--port' in sys.argv:
    PORT = int(sys.argv[sys.argv.index('--port') + 1])
# Operator requests are separate from run ledgers. Notes wake the harness; control
# requests are durable input for a script to acknowledge at a safe checkpoint.
CHAT_FILE = os.path.join(SOURCES['runguard'], 'chat.jsonl')
CONTROL_FILE = os.path.join(SOURCES['runguard'], 'controls.jsonl')
ACTIVE_S = 120   # a file touched in the last 2 min counts as live
EVENT_READ_BYTES = 512 * 1024
LAST_EVENT_READ_BYTES = 128 * 1024
_SUMMARY_CACHE = {}  # path -> {identity, offset, first, latest}
_AUXILIARY_JSONL = {'chat.jsonl', 'controls.jsonl'}
_CONTROL_LOCK = threading.Lock()


def _timestamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _first_event(path):
    try:
        with open(path, 'rb') as f:
            line = f.readline(8192).decode('utf-8', 'replace').strip()
        return json.loads(line) if line else {}
    except Exception:
        return {}


def _summary_event(path):
    """Return the newest run_started using a complete, incremental ledger scan.

    A lane can contain a large dry-run, a large full run, and several retries.
    Reading a fixed head or tail window eventually picks the wrong attempt. The
    first sidebar pass scans complete JSONL lines; later polls continue from the
    cached byte offset, so active ledgers cost only their newly appended lines.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return {}
    identity = (stat.st_dev, stat.st_ino)
    cached = _SUMMARY_CACHE.get(path)
    if not cached or cached['identity'] != identity or stat.st_size < cached['offset']:
        cached = {'identity': identity, 'offset': 0, 'first': {}, 'latest': {}}
    first, latest, offset = cached['first'], cached['latest'], cached['offset']
    try:
        with open(path, 'rb') as fh:
            fh.seek(offset)
            while True:
                line = fh.readline()
                if not line or not line.endswith(b'\n'):
                    break  # retry an in-flight final line on the next sidebar poll
                offset = fh.tell()
                try:
                    rec = json.loads(line.decode('utf-8', 'replace'))
                except json.JSONDecodeError:
                    continue
                if not first:
                    first = rec
                if (rec.get('event') or rec.get('action')) == 'run_started':
                    latest = rec
    except OSError:
        return latest or first
    _SUMMARY_CACHE[path] = {'identity': identity, 'offset': offset, 'first': first, 'latest': latest}
    return latest or first


def _last_event(path):
    """Read the latest complete ledger event without loading a whole large run."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            f.seek(max(0, size - LAST_EVENT_READ_BYTES))
            chunk = f.read()
        for line in reversed(chunk.decode('utf-8', 'replace').splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return {}


def _is_live_run(path, mtime, now):
    """A fresh terminal event means finished, even though the file is recent."""
    last = _last_event(path)
    event = last.get('event') or last.get('action')
    if event in {'run_finished', 'run_failed', 'run_abandoned', 'run_paused'}:
        return False
    return now - mtime < ACTIVE_S


def _describe(first):
    """Plain-language one-liner for a run, from its first ledger event.
    Scripts can set it explicitly: ledger(scope, 'run_started', description='...')."""
    if first.get('description'):
        return str(first['description'])
    bits = []
    n = first.get('companies') or first.get('todo') or (first.get('details') or {}).get('total')
    if n:
        bits.append(f'{n} companies')
    if first.get('worst_case_credits'):
        bits.append(f'max {first["worst_case_credits"]} credits')
    if first.get('table'):
        bits.append(f'table {first["table"]}')
    if first.get('input'):
        bits.append(os.path.basename(str(first['input'])))
    if first.get('verb'):
        bits.append(f'{first["verb"]} run')
    return ' · '.join(bits)


def _nice_name(raw, kind):
    """'2025-03-10T19-15-59Z-enrich' → ('enrich', 'Mar 10, 19:15');
    '2025-01-15-113016-my-run.jsonl' → ('my-run', 'Jan 15, 11:30')."""
    raw = re.sub(r'\.jsonl$', '', raw)
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})[T-](\d{2})[-:]?(\d{2})[-:]?\d{2}Z?-(.+)', raw)
    if not m:
        return raw, ''
    y, mo, d, h, mi, scope = m.groups()
    months = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    return scope.replace('-', ' '), f'{months[int(mo)]} {int(d)}, {h}:{mi}'


def _is_run_ledger(filename):
    """State side channels are JSONL too, but they are not dashboard runs."""
    return (filename.endswith('.jsonl') and filename not in _AUXILIARY_JSONL and
            not filename.endswith('.receipts.jsonl'))


def _run_ledger_path(run_id):
    """Resolve a dashboard run id to its ledger without permitting path escape."""
    kind, sep, name = str(run_id).partition(':')
    root = SOURCES.get(kind)
    if not sep or not root:
        return None
    if kind == 'push':
        candidate = os.path.join(root, os.path.basename(name), 'events.jsonl')
    else:
        candidate = os.path.join(root, os.path.basename(name))
    root = os.path.realpath(root)
    candidate = os.path.realpath(candidate)
    if kind == 'push':
        return candidate if os.path.dirname(os.path.dirname(candidate)) == root else None
    return candidate if os.path.dirname(candidate) == root else None


def _acknowledged_control_ids(run_id):
    path = _run_ledger_path(run_id)
    if not path or not os.path.isfile(path):
        return set()
    ids = set()
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (event.get('event') or event.get('action')) == 'control_acknowledged':
                    control_id = event.get('control_id')
                    if control_id:
                        ids.add(str(control_id))
    except OSError:
        pass
    return ids


def _pending_control(run_id, kind):
    """Return the latest unacknowledged request for this run action, if any."""
    if not os.path.isfile(CONTROL_FILE):
        return None
    acknowledged = _acknowledged_control_ids(run_id)
    pending = None
    try:
        with open(CONTROL_FILE, encoding='utf-8') as fh:
            for line in fh:
                try:
                    control = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if control.get('run') == run_id and control.get('kind') == kind:
                    if str(control.get('id')) not in acknowledged:
                        pending = control
    except OSError:
        return None
    return pending


def list_runs():
    runs = []
    now = time.time()
    push_dir = SOURCES['push']
    if os.path.isdir(push_dir):
        for d in os.listdir(push_dir):
            ev = os.path.join(push_dir, d, 'events.jsonl')
            if os.path.exists(ev):
                mtime = os.path.getmtime(ev)
                name, when = _nice_name(d, 'push')
                runs.append({'id': f'push:{d}', 'label': d, 'name': name, 'when': when,
                             'desc': _describe(_summary_event(ev)), 'kind': 'push',
                             'path': os.path.abspath(os.path.join(push_dir, d)),
                             'mtime': mtime, 'live': _is_live_run(ev, mtime, now)})
    for kind in ('enrich', 'runguard'):
        d = SOURCES[kind]
        if os.path.isdir(d):
            for f in os.listdir(d):
                if _is_run_ledger(f):
                    p = os.path.join(d, f)
                    mtime = os.path.getmtime(p)
                    name, when = _nice_name(f, kind)
                    runs.append({'id': f'{kind}:{f}', 'label': f, 'name': name, 'when': when,
                                 'desc': _describe(_summary_event(p)), 'kind': kind,
                                 'path': os.path.abspath(p),
                                 'mtime': mtime, 'live': _is_live_run(p, mtime, now)})
    runs.sort(key=lambda r: -r['mtime'])
    return runs


def locks():
    out = []
    for d in set(SOURCES.values()):
        if not d or not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith('.lock'):
                try:
                    with open(os.path.join(d, f), encoding='utf-8') as fh:
                        lock = json.load(fh)
                    pid = int(lock.get('pid', 0))
                    if pid <= 0:
                        continue
                    try:
                        os.kill(pid, 0)
                        alive = True
                    except Exception:
                        alive = False
                    out.append({'scope': lock.get('scope') or f, 'pid': pid,
                                'started': lock.get('started'), 'alive': alive})
                except Exception:
                    pass
    return out


def _files_for(run_id):
    kind, _, name = run_id.partition(':')
    if not re.fullmatch(r'[\w.\-:TZ]+', name):
        return []
    if kind == 'push':
        d = os.path.join(SOURCES['push'], name)
        return [p for p in (os.path.join(d, 'events.jsonl'), os.path.join(d, 'api-calls.jsonl'))
                if os.path.exists(p)]
    p = os.path.join(SOURCES.get(kind, ''), name)
    return [p] if os.path.exists(p) else []


def read_events(run_id, offsets):
    """Incremental tail: offsets = {path: byte_offset} from the client."""
    events, new_offsets = [], {}
    for path in _files_for(run_id):
        try:
            size = os.path.getsize(path)
            try:
                off = int(offsets.get(path, 0))
            except (AttributeError, TypeError, ValueError, OverflowError):
                off = 0
            off = max(0, off)
            read_limit = EVENT_READ_BYTES  # cap: fills in progressively across polls
            if size < off:
                off = 0  # rotated/truncated
            with open(path, 'rb') as f:
                f.seek(off)
                chunk = f.read(read_limit)
                end = f.tell()
                if end < size and chunk and not chunk.endswith(b'\n'):
                    chunk += f.readline()
                    end = f.tell()
        except OSError:
            continue  # writer rotated or removed the file between discovery and read
        lines = chunk.splitlines(keepends=True)
        if lines and not lines[-1].endswith(b'\n'):
            end -= len(lines.pop())
        new_offsets[path] = end
        for raw_line in lines:
            line = raw_line.decode('utf-8', 'replace').strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                rec['_file'] = os.path.basename(path)
                events.append(rec)
            except json.JSONDecodeError:
                pass
    events.sort(key=lambda e: e.get('ts') or '')
    return events, new_offsets


def has_more_events(run_id, offsets):
    """Whether an incremental client has more complete ledger bytes to fetch."""
    for path in _files_for(run_id):
        try:
            if int(offsets.get(path, 0)) < os.path.getsize(path):
                return True
        except (AttributeError, TypeError, ValueError, OverflowError, OSError):
            continue
    return False


PAGE = """<!doctype html><meta charset="utf-8"><title>Run observer</title>
<link id=favicon rel="icon" href="">
<style>
:root{--bg:#0f1317;--panel:#181e25;--card:#1e262f;--txt:#e6ebf0;--dim:#8b96a3;--ok:#57c98a;--warn:#e5b95a;--err:#e5756a;--info:#6fa8e0;--line:#28313c}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--txt);display:flex;height:100vh}
#side{width:320px;min-width:320px;overflow-y:auto;background:var(--panel);padding:14px;border-right:1px solid #000}
#sideHead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:12px}
#brand{display:flex;align-items:center;gap:10px;min-width:0}
#brandMark{width:38px;height:38px;border-radius:10px;background:#edf6ff;color:#0e1720;display:grid;place-items:center;box-shadow:0 0 0 1px rgba(255,255,255,.08),0 10px 22px rgba(0,0,0,.28)}
#brandMark svg{width:28px;height:28px;display:block}
#brandName{font-size:13px;font-weight:760;line-height:1.1;white-space:nowrap}
#brandSub{font-size:11px;color:var(--dim);line-height:1.2;margin-top:2px}
#sideToggle{width:34px;height:34px;display:grid;place-items:center;background:var(--card);border:1px solid var(--line);color:var(--dim);border-radius:8px;cursor:pointer}
#sideToggle:hover{color:var(--txt);border-color:#405064;background:#24303c}
#sideToggle svg{width:18px;height:18px;display:block}
body.noside #side{width:42px;min-width:42px;padding:10px 5px;overflow:hidden}
body.noside #side > :not(#sideHead){display:none}
body.noside #sideHead{justify-content:center}
body.noside #brand{display:none}
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
#topbar{padding:12px 20px;background:var(--panel);border-bottom:1px solid #000}
#stats{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}
.chip{background:#1b232d;border:1px solid #26313d;border-radius:8px;padding:7px 13px;text-align:center;min-width:84px}
.chip b{font-size:18px;display:block;line-height:1.1}
.chip small{color:var(--dim)}
.activity{flex-basis:100%;display:grid;grid-template-columns:1.15fr 1fr 1fr .8fr;gap:10px;background:#11171d;border:1px solid var(--line);border-left:4px solid #405064;border-radius:8px;padding:11px 13px;box-shadow:0 10px 28px rgba(0,0,0,.18)}
.activity .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em;display:block;line-height:1.2}
.activity .v{font-size:14px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block}
.activity.live{border-color:#28513d;border-left-color:var(--ok)}
.activity.stale{border-color:#5b4b22;border-left-color:var(--warn)}
.activity.failed{border-color:#5b2c28;border-left-color:var(--err)}
.activity.done{border-color:#29455e;border-left-color:var(--info)}
@media(max-width:900px){.activity{grid-template-columns:1fr 1fr}.activity .v{white-space:normal}}
@media(max-width:720px){
  body{position:relative;min-width:0}
  #main{min-width:0}
  body:not(.noside) #side{position:absolute;inset:0 auto 0 0;z-index:30;width:min(320px,86vw);min-width:min(320px,86vw);box-shadow:16px 0 32px rgba(0,0,0,.42)}
  #topbar{padding:10px;overflow-x:auto}
  .tabs{min-width:max-content}
  #content{padding:10px}
  .recordshell{height:calc(100vh - 196px)}
}
#content{flex:1;overflow-y:auto;padding:14px 20px}
h3{margin:10px 0 8px;font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em}
.bridge{background:#111820;border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:14px}
.bridgeTop{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px}
.bridgeTitle{font-size:13px;font-weight:700;color:var(--txt)}
.bridgeDesc{font-size:12px;color:var(--dim);line-height:1.3;margin-top:2px}
.bridgeBadge{font-size:11px;border-radius:99px;padding:2px 8px;background:#213126;color:var(--ok);white-space:nowrap}
.bridgeBadge.idle{background:#232b35;color:var(--dim)}
.bridgeBadge.done{background:#1f3347;color:var(--info)}
.bridgeBadge.live{background:#213126;color:var(--ok)}
.bridgeBadge.attn{background:#3a331d;color:var(--warn)}
.bridgeGrid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.bridgeMetric{background:#17202a;border:1px solid #26313d;border-radius:8px;padding:8px;min-height:58px}
.bridgeMetric b{display:block;font-size:16px;line-height:1.15}
.bridgeMetric small{display:block;color:var(--dim);font-size:11.5px;line-height:1.25;margin-top:3px}
.bridgeNote{margin-top:9px;color:var(--dim);font-size:12px;line-height:1.35}
.bridgeNote b{color:var(--txt)}
.bridgeLock{margin-top:8px;border-top:1px solid var(--line);padding-top:8px}
.bridgeActions{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.controlBtn{height:32px;display:inline-flex;align-items:center;gap:6px;padding:0 10px;background:#17202a;color:var(--dim);border:1px solid #334151;border-radius:7px;cursor:pointer;font-size:12px;font-weight:650}
.controlBtn:hover{color:var(--txt);background:#273441;border-color:#4b6178}
.controlBtn.warn:hover{color:var(--warn);border-color:#6a5525}
.controlBtn.requested{color:var(--warn);border-color:#6a5525}
.controlBtn.accepted{color:var(--ok);background:#1d3a2b;border-color:#356b4e}
.controlBtn:disabled{cursor:default;opacity:.82}
.controlBtn svg{width:16px;height:16px;display:block}
.controlState{margin-top:7px;color:var(--warn);font-size:11.5px}
.controlState .accepted{color:var(--ok)}
.run{padding:7px 10px;border-radius:7px;cursor:pointer;margin-bottom:4px;font-size:12.5px}
.run:hover{background:#242e39}.run.sel{background:#2c3948}
.run small{color:var(--dim);display:block}
.live{color:var(--ok)}.dead{color:var(--dim)}
.line{padding:5px 0;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:baseline}
.line .when{color:var(--dim);font-size:11.5px;min-width:56px;font-family:ui-monospace,monospace}
.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}.info{color:var(--info)}.dim{color:var(--dim)}
.card{background:var(--card);border-radius:10px;padding:12px 16px;margin-bottom:10px}
.card h4{margin:0 0 6px;font-size:14.5px}
.card .row{padding:3px 0;color:var(--txt)}
.card .row small{color:var(--dim)}
.recordshell{height:calc(100vh - 214px);overflow:auto;border-radius:10px;background:var(--card);border:1px solid var(--line)}
.recordshell .tablewrap{overflow:visible;max-height:none;border-radius:0}
.tableTools{position:sticky;top:0;left:0;z-index:8;display:flex;align-items:center;gap:7px;flex-wrap:wrap;padding:8px 10px;background:#151c24;border-bottom:1px solid var(--line)}
.filterToggle,.filterChip,.filterAction{background:#202a35;color:var(--txt);border:1px solid #344355;border-radius:7px;padding:5px 9px;cursor:pointer;font:12px -apple-system,"Segoe UI",sans-serif}
.filterToggle:hover,.filterAction:hover{background:#2b3948;border-color:#4d6580}
.filterChip{display:inline-flex;align-items:center;gap:5px;color:var(--dim);cursor:default}.filterChip button{border:0;background:transparent;color:var(--dim);padding:0;cursor:pointer;font-size:15px;line-height:1}.filterChip button:hover{color:var(--txt)}
.filterGroup{display:inline-flex;align-items:center;gap:5px;padding:4px 5px;border:1px solid #425063;border-radius:7px;background:#1a2530}.filterGroup small{color:var(--dim);white-space:nowrap}.filterJoin{font-size:10px;color:var(--info)}
.filterPanel{position:sticky;top:41px;left:0;z-index:8;display:grid;grid-template-columns:minmax(120px,1fr) minmax(112px,.8fr) minmax(105px,1fr) minmax(105px,1fr) minmax(130px,1fr) auto;gap:7px;align-items:center;padding:8px 10px;background:#121920;border-bottom:1px solid var(--line)}
.filterPanel select,.filterPanel input{min-width:0;width:100%;background:#0d1114;color:var(--txt);border:1px solid #344355;border-radius:6px;padding:6px 8px;font:12px -apple-system,"Segoe UI",sans-serif}
.filterPanel input:last-of-type[data-hidden="true"]{display:none}
@media(max-width:720px){.filterPanel{grid-template-columns:1fr 1fr}.filterPanel .filterAction{grid-column:span 2}}
.tablewrap{overflow:auto;max-height:calc(100vh - 150px);border-radius:10px;background:var(--card);border:1px solid var(--line)}
.subtabs{position:sticky;top:0;left:0;z-index:8;display:flex;gap:6px;flex-wrap:wrap;padding:8px;background:#151c24;border-bottom:1px solid var(--line)}
.subtab{padding:5px 12px;border-radius:7px;background:#202a35;color:var(--dim);cursor:pointer;font-size:12.5px;border:1px solid transparent}
.subtab:hover{color:var(--txt);border-color:#3a4a5e}
.subtab.sel{background:#314052;color:var(--txt);border-color:#43566c}
table{table-layout:fixed;border-collapse:separate;border-spacing:0;background:var(--card)}
th{position:sticky;top:0;z-index:2;background:#242e3a;text-align:left;padding:9px 12px;font-size:11.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.recordshell th{top:41px}
.recordshell.hasSubtabs .tableTools{top:43px}.recordshell.hasSubtabs .filterPanel{top:84px}
.recordshell.hasSubtabs th{top:84px}.recordshell.filtersOpen th{top:84px}.recordshell.hasSubtabs.filtersOpen th{top:127px}
td{padding:8px 12px;border-top:1px solid var(--line);vertical-align:top;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
tr:hover td{background:#232c36}
/* freeze the first column so it stays visible when scrolling a wide table right */
th:first-child{left:0;z-index:3}
td:first-child{position:sticky;left:0;z-index:1;background:var(--card)}
tr:hover td:first-child{background:#232c36}
/* Generic tables keep both the ordinal and the real row identity in view. */
.recordshell th.rownum,.recordshell td.rownum{width:54px;min-width:54px;max-width:54px;text-align:right;color:var(--dim);font-variant-numeric:tabular-nums}
.recordshell th.datafirst{left:54px;z-index:3}
.recordshell td.datafirst{position:sticky;left:54px;z-index:1;background:var(--card)}
.recordshell tr:hover td.datafirst{background:#232c36}
/* drag handle on the right edge of each header cell to resize its column */
.rz{position:absolute;top:0;right:0;width:7px;height:100%;cursor:col-resize}
.rz:hover{background:#3a4a5e}
/* click structured JSON or double-click any cell to inspect full content */
#cellmodal{display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.55);align-items:center;justify-content:center}
#cellmodal.show{display:flex}
#cellmodalbox{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;width:min(760px,90vw);max-height:80vh;display:flex;flex-direction:column;overflow:hidden}
#cellmodalhead{color:var(--info);font-size:12.5px;margin-bottom:8px}
#cellmodalbody{white-space:pre-wrap;word-break:break-word;font-size:14px;line-height:1.55;overflow:auto;min-height:0}
#cellmodalbody.json{font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;color:#dbe9f7;tab-size:2}
#cellmodalactions{flex:0 0 auto;text-align:right;margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
.jsonOpen{display:inline-flex;align-items:center;gap:7px;max-width:100%;padding:0;border:0;background:transparent;color:var(--info);font:inherit;cursor:pointer}
.jsonOpen:hover{text-decoration:underline}.jsonGlyph{font:12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dim)}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px}
.pill.ok{background:#1d3a2b;color:var(--ok)}.pill.warn{background:#3a331d;color:var(--warn)}
.pill.err{background:#3a221d;color:var(--err)}.pill.dim{background:#242e39;color:var(--dim)}
.tabs{display:flex;gap:8px}
.tab{padding:5px 14px;border-radius:7px;background:var(--card);cursor:pointer;font-size:13px}
.tab.sel{background:#314052}
.flowShell{display:flex;flex-direction:column;gap:12px;min-height:100%}
.flowHead{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;padding:2px 2px 10px;border-bottom:1px solid var(--line)}
.flowTitle{font-size:18px;font-weight:760;line-height:1.2}.flowSub{color:var(--dim);font-size:12.5px;margin-top:3px}
.flowPlan{font:11.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dim);text-align:right;max-width:310px;overflow-wrap:anywhere}
.flowSummary{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:8px}
.flowMetric{border:1px solid var(--line);border-radius:8px;padding:9px 11px;background:#151c23;min-height:62px}
.flowMetric b{display:block;font-size:17px;line-height:1.2}.flowMetric small{color:var(--dim);font-size:11.5px}
.flowCanvas{position:relative;overflow:auto;min-height:318px;border:1px solid var(--line);border-radius:8px;background:#12181e}
.flowGraph{position:relative;display:grid;grid-auto-flow:column;grid-auto-columns:220px;gap:74px;align-items:stretch;min-width:max-content;min-height:316px;padding:30px 34px}
.flowLevel{display:flex;flex-direction:column;justify-content:space-around;gap:16px;position:relative;z-index:2}
.flowEdges{position:absolute;inset:0;width:100%;height:100%;z-index:1;pointer-events:none;overflow:visible}
.flowNode{width:220px;min-height:158px;border:1px solid #35414e;border-left:4px solid #566576;border-radius:8px;background:#1b232b;padding:11px;cursor:pointer;box-shadow:0 8px 20px rgba(0,0,0,.18);transition:border-color .16s,background .16s,transform .16s}
.flowNode:hover{background:#222c35;border-color:#526173;transform:translateY(-1px)}
.flowNode.selected{outline:2px solid #d7e7f5;outline-offset:2px}.flowNode.running{border-left-color:var(--warn)}.flowNode.complete{border-left-color:var(--ok)}.flowNode.failed{border-left-color:var(--err)}.flowNode.held{border-left-color:#d69b63}.flowNode.pending{border-left-color:#566576}
.flowNode.running .flowNodeIcon{animation:flowPulse 1.5s ease-in-out infinite}@keyframes flowPulse{50%{box-shadow:0 0 0 5px rgba(229,185,90,.12)}}
.flowNodeTop{display:flex;align-items:center;gap:9px}.flowNodeIcon{width:30px;height:30px;flex:0 0 30px;border-radius:7px;display:grid;place-items:center;background:#26323d;color:#dce8f2;font:700 12px/1 ui-monospace,monospace}
.flowNodeName{font-size:13.5px;font-weight:720;line-height:1.2;overflow-wrap:anywhere}.flowNodeKind{font-size:10.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
.flowState{margin-left:auto;font-size:10.5px;border-radius:99px;padding:2px 7px;background:#28323c;color:var(--dim);white-space:nowrap}.flowState.running{background:#3a331d;color:var(--warn)}.flowState.complete{background:#1d3a2b;color:var(--ok)}.flowState.failed{background:#3a221d;color:var(--err)}.flowState.held{background:#3d2f25;color:#e5ab72}
.flowBar{height:4px;background:#0e1419;border-radius:99px;overflow:hidden;margin:11px 0 5px}.flowBar span{display:block;height:100%;background:var(--ok);transition:width .2s}.flowNode.running .flowBar span{background:var(--warn)}.flowNode.failed .flowBar span{background:var(--err)}
.flowProgressLine{font-size:9.5px;color:var(--dim);margin-bottom:7px;font-variant-numeric:tabular-nums}
.flowNodeStats{display:grid;grid-template-columns:repeat(4,1fr);gap:4px}.flowNodeStats b{display:block;font-size:12.5px}.flowNodeStats small{display:block;color:var(--dim);font-size:8.5px;white-space:nowrap}
.flowEdgeLabel{fill:#9aa7b4;font:10px -apple-system,'Segoe UI',sans-serif;paint-order:stroke;stroke:#12181e;stroke-width:5px;stroke-linejoin:round}
.flowInspector{display:grid;grid-template-columns:minmax(260px,.85fr) minmax(420px,1.6fr);border:1px solid var(--line);border-radius:8px;background:#151c23;overflow:hidden}
.flowDetail{padding:14px;border-right:1px solid var(--line)}.flowDetail h4,.flowRows h4{font-size:13.5px;margin:0 0 8px}.flowMeta{font-size:12px;color:var(--dim);margin-bottom:10px}
.flowPorts{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 11px}.flowPort{font:10.5px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;padding:4px 6px;border-radius:5px;background:#222c35;color:#cbd7e2}.flowPort.out{background:#203429;color:#8ed6ad}
.flowBatches{margin:11px 0}.flowBatchList{display:flex;flex-direction:column;gap:5px;max-height:144px;overflow:auto;margin-top:6px}.flowBatch{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 8px;padding:6px 7px;border:1px solid #2a3540;border-radius:6px;background:#192129}.flowBatch b{font-size:10.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.flowBatch span{font-size:10px;color:var(--dim);white-space:nowrap}.flowBatch small{grid-column:1/-1;color:var(--dim);font-size:9.5px}
.flowRows{min-width:0;padding:14px}.flowUnitList{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;max-height:202px;overflow:auto}
.flowUnit{display:flex;align-items:center;gap:8px;border:1px solid #2a3540;border-radius:7px;padding:7px 8px;background:#192129;cursor:pointer;min-width:0}.flowUnit:hover,.flowUnit.selected{border-color:#53677a;background:#222c35}.flowUnitKey{font-size:11.5px;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}.flowUnitState{font-size:10.5px;color:var(--dim)}
.flowTrace{grid-column:1/-1;border-top:1px solid var(--line);padding:13px 14px}.flowTraceHead{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:9px}.flowTracePath{display:flex;gap:7px;align-items:stretch;overflow-x:auto;padding-bottom:3px}.flowTraceStep{min-width:142px;max-width:190px;border:1px solid #2c3742;border-radius:7px;padding:7px 8px;background:#192129}.flowTraceStep b{display:block;font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.flowTraceStep small{font-size:10.5px;color:var(--dim)}.flowTraceArrow{align-self:center;color:#576573}
.flowStatusDot{width:7px;height:7px;border-radius:50%;display:inline-block;background:#66717d;margin-right:5px}.flowStatusDot.complete,.flowStatusDot.succeeded,.flowStatusDot.accepted{background:var(--ok)}.flowStatusDot.running{background:var(--warn)}.flowStatusDot.failed{background:var(--err)}.flowStatusDot.held{background:#e5ab72}
@media(max-width:900px){.flowSummary{grid-template-columns:1fr 1fr}.flowInspector{grid-template-columns:1fr}.flowDetail{border-right:0;border-bottom:1px solid var(--line)}}
@media(max-width:600px){.flowHead{flex-direction:column}.flowPlan{text-align:left}.flowUnitList{grid-template-columns:1fr}.flowGraph{grid-auto-columns:205px;gap:58px;padding:24px}.flowNode{width:205px}.flowSummary{grid-template-columns:1fr 1fr}}
label{color:var(--dim);font-size:12.5px;margin-left:auto;align-self:center;cursor:pointer}
input[type=text]{width:100%;background:#0d1114;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:6px 10px;margin-bottom:8px;font-size:13px}
.lock{padding:6px 10px;border-radius:7px;margin-bottom:4px;background:var(--card);font-size:12.5px}
.empty{color:var(--dim);padding:30px;text-align:center}
.explain{max-width:820px;line-height:1.65}
.explain h2{font-size:18px;margin:16px 0 6px}.explain h3{font-size:15px;margin:12px 0 4px}
.explain p{margin:8px 0}.explain ul{margin:6px 0 6px 18px}.explain li{margin:3px 0}
.explain code{background:#0d1114;padding:1px 5px;border-radius:4px;font-size:12.5px}
pre.diagram{background:#0d1114;border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto;font:12.5px/1.45 ui-monospace,Menlo,monospace;color:var(--txt);white-space:pre}
[data-col]{cursor:default}
th[data-col]:hover,td[data-col]:hover{outline:1px solid #34506e;outline-offset:-1px}
.chatdot{margin-left:6px;font-size:10px;opacity:.85}
.chatdot.resolved{color:var(--ok);opacity:1}
#chatpop{display:none;position:fixed;z-index:50;width:320px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;box-shadow:0 12px 34px rgba(0,0,0,.55)}
#chatpopHead{font-size:12.5px;color:var(--info);margin-bottom:8px}
#chatthread{max-height:220px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;margin-bottom:8px}
#chatthread .msg{font-size:13px;border-radius:8px;padding:6px 9px;max-width:88%}
#chatthread .msg.user{align-self:flex-end;background:#2c3948}
#chatthread .msg.agent{align-self:flex-start;background:var(--card)}
#chatinput{width:100%;background:#0d1114;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:7px 9px;font:13px/1.4 -apple-system,sans-serif;resize:vertical;min-height:44px}
.chatbtn{background:var(--card);border:none;color:var(--txt);border-radius:7px;padding:6px 12px;cursor:pointer;font-size:12.5px}
.chatbtn.primary{background:#2f6fb0;color:#fff}
</style>
<div id=side>
  <div id=sideHead>
    <div id=brand><div id=brandMark></div><div><div id=brandName>Observer Kit</div><div id=brandSub>run monitor</div></div></div>
    <button id=sideToggle onclick="toggleSide()" title="Collapse sidebar" aria-label="Collapse sidebar"></button>
  </div>
  <h3>Agent bridge</h3><div id=locks class=bridge></div>
  <h3>Runs (newest first)</h3><input type=text id=q placeholder="filter…"><div id=runs></div>
</div>
<div id=main>
  <div id=topbar>
    <div class=tabs>
      <div class="tab sel" id=tabRecords onclick="view='records';render()">Data</div>
      <div class=tab id=tabFlow style="display:none" onclick="view='flow';render()">Flow</div>
      <div class=tab id=tabAttention onclick="view='attention';render()">Attention</div>
      <div class=tab id=tabFeed onclick="view='feed';render()">Timeline</div>
      <div class=tab id=tabInfo onclick="view='info';render()">Run info</div>
      <div class=tab id=tabExplain onclick="view='explain';render()">How it works</div>
      <label id=techWrap title="Also show every raw HTTP request the run made (reads, polling). Failures always show, even unchecked." style="display:none"><input type=checkbox id=tech onchange="render()"> show raw API calls <span id=techCount style="color:var(--dim)"></span></label>
    </div>
    <div id=stats></div>
  </div>
  <div id=content><div class=empty>Pick a run on the left. ● = running now.</div></div>
</div>
<div id=chatpop>
  <div id=chatpopHead></div>
  <div id=chatthread></div>
  <textarea id=chatinput placeholder="Tell the agent what to change here… (Enter to send, Shift+Enter = newline)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat();}"></textarea>
  <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:6px">
    <button class=chatbtn onclick="closeChat()">Close</button>
    <button id=chatSend class="chatbtn primary" onclick="sendChat()">Send to agent</button>
  </div>
</div>
<div id=cellmodal onclick="if(event.target.id==='cellmodal')closeCellModal()">
  <div id=cellmodalbox>
    <div id=cellmodalhead></div>
    <div id=cellmodalbody></div>
    <div id=cellmodalactions><button class=chatbtn onclick="closeCellModal()">Close</button></div>
  </div>
</div>
<script>
let sel=null, offsets={}, all=[], view='records', chatByAnchor={}, chatOpenAnchor=null, pendingControl=null, controls=[], colW={}, recTab=null, currentLocks=[], _buildAbort=null;
let flowNodeSelected=null,flowRowSelected=null,_flowVersion=0,_lastFlowVersion=-1;
let tableFilters=Object.create(null), filterOpen=null, filterDraft=null, _filterVersion=0;
let _eventCount=-1, _lastView=null, _lastSel=null, _lastRecTab=null, _lastFilterVersion=-1, _recGroupsCache=null, _recGroupsVer=0;
function setRecTab(t){recTab=t;render();}
const COLW_DEFAULT={Company:190,Person:150,Tier:80,Phone:170,Email:230,'CRM id':120};
try{colW=JSON.parse(localStorage.getItem('observer_colw')||'{}')}catch(e){}
const content=document.getElementById('content');
function contentViewportHeight(){return Math.max(260, content.clientHeight-28);}
let autoscroll=true;
content.addEventListener('scroll',()=>{autoscroll=content.scrollTop+content.clientHeight>content.scrollHeight-60});

function captureTableScroll(){
  const shell=content.querySelector('.recordshell');
  return {contentTop:content.scrollTop, shellTop:shell?.scrollTop??0, shellLeft:shell?.scrollLeft??0};
}
function restoreTableScroll(state){
  if(!state)return;
  requestAnimationFrame(()=>{
    content.scrollTop=Math.min(state.contentTop, Math.max(0,content.scrollHeight-content.clientHeight));
    const shell=content.querySelector('.recordshell');
    if(!shell)return;
    shell.scrollTop=Math.min(state.shellTop, Math.max(0,shell.scrollHeight-shell.clientHeight));
    shell.scrollLeft=Math.min(state.shellLeft, Math.max(0,shell.scrollWidth-shell.clientWidth));
  });
}

// --- inline chat (v2): Command-click a column header or cell to leave an agent note ---
// Chat and durable control requests are side channels. The dashboard never
// writes the run ledger or alters a worker directly.
function anchorFor(cell){
  const col=cell.dataset.col; if(!col)return null;
  if(cell.tagName==='TH')return 'col:'+col;
  const tr=cell.closest('tr'); return 'cell:'+((tr&&tr.dataset.key)||'')+'|'+col;
}
function labelFor(cell){
  const col=cell.dataset.col;
  if(cell.tagName==='TH')return 'Column · '+col;
  const tr=cell.closest('tr'); const nm=(tr&&(tr.dataset.name||tr.dataset.co))||''; return (nm?nm+' · ':'')+col;
}
function openChat(anchor,label,el,control=null){
  pendingControl=control;
  chatOpenAnchor=anchor;
  const pop=document.getElementById('chatpop'), r=el.getBoundingClientRect();
  pop.style.display='block';
  pop.style.left=Math.max(8,Math.min(r.left,window.innerWidth-336))+'px';
  pop.style.top=Math.max(8,Math.min(r.bottom+6,window.innerHeight-300))+'px';
  document.getElementById('chatpopHead').textContent='💬 '+(control?control.label:label);
  renderThread(true);
  const ti=document.getElementById('chatinput');
  ti.value='';
  ti.placeholder=control?`What should the agent know before ${control.prompt}?`:'Tell the agent what to change here… (Enter to send, Shift+Enter = newline)';
  document.getElementById('chatSend').textContent=control?control.label:'Send to agent';
  ti.focus();
}
function openRunChat(){
  if(!sel)return;
  openChat('run','Run',document.getElementById('locks'));
}
async function openControlChat(kind,label,prompt){
  if(!sel||!controlAvailability()[kind])return;
  await requestControl(kind);
  openChat('run',label,document.getElementById('locks'),{label,prompt});
}
function closeChat(){chatOpenAnchor=null;pendingControl=null;document.getElementById('chatpop').style.display='none';}
function renderThread(forceBottom){
  const t=document.getElementById('chatthread');
  // only snap to the newest if you were already at the bottom; otherwise keep
  // your scroll position so you can read earlier messages while polls come in.
  const atBottom=t.scrollHeight-t.scrollTop-t.clientHeight<40;
  const prev=t.scrollTop;
  const msgs=(chatByAnchor[chatOpenAnchor]||[]).filter(m=>m.kind!=='control');
  t.innerHTML=msgs.length
    ?msgs.map(m=>`<div class="msg ${m.author==='agent'?'agent':'user'}"><b>${m.author==='agent'?'agent':'you'}</b> <small style="color:var(--dim)">${(m.ts||'').slice(11,16)}</small>${m.resolved?' <small style="color:var(--ok)">✓ resolved</small>':''}<div>${esc(m.text)}</div></div>`).join('')
    :'<div style="color:var(--dim);font-size:12.5px">No notes here yet. Tell the agent what to change — it watches for your messages and can reply.</div>';
  t.scrollTop=(forceBottom||atBottom)?t.scrollHeight:prev;
}
async function sendChat(){
  const ti=document.getElementById('chatinput'), text=ti.value.trim();
  if(!text||!sel||!chatOpenAnchor)return;
  ti.value='';
  const control=pendingControl;
  try{
    if(control){
      await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run:sel,anchor:'run',text:`${control.label}: ${text}`})});
      await loadChat();
      closeChat();
    }else{
      await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run:sel,anchor:chatOpenAnchor,text})});
      await loadChat();
    }
  }catch(e){}
}
async function loadChat(){
  if(!sel){chatByAnchor={};return;}
  try{
    const msgs=await (await fetch('/api/chat?run='+encodeURIComponent(sel))).json();
    const by={}; for(const m of msgs){(by[m.anchor]=by[m.anchor]||[]).push(m);} chatByAnchor=by;
  }catch(e){}
  renderBridge();
  decorateChat();
  if(chatOpenAnchor)renderThread();
}
async function loadControls(){
  if(!sel){controls=[];return;}
  try{controls=await (await fetch('/api/control?run='+encodeURIComponent(sel))).json();}catch(e){controls=[];}
}
function controlIcon(kind){
  if(kind==='pause')return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>';
  if(kind==='stop_after_record')return '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor"/></svg>';
  if(kind==='accepted')return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" fill="currentColor"/></svg>';
}
async function requestControl(kind,note='',notify=true){
  if(!sel||!controlAvailability()[kind])return;
  try{
    await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run:sel,kind,note,notify})});
    await Promise.all([loadControls(),loadChat()]);
    renderBridge();
  }catch(e){}
}
function decorateChat(){
  content.querySelectorAll('[data-col]').forEach(cell=>{
    const old=cell.querySelector('.chatdot'); if(old)old.remove();
    const a=anchorFor(cell), msgs=chatByAnchor[a]||[]; if(!msgs.length)return;
    const resolved=msgs.some(m=>m.resolved);
    const b=document.createElement('span');
    b.className='chatdot'+(resolved?' resolved':'');
    b.textContent=resolved?'✓':'💬'+msgs.length;
    cell.appendChild(b);
  });
}
// Command/Ctrl-click = chat · JSON click/double click = expand · drag = resize
content.addEventListener('click',ev=>{
  if(ev.target.closest('.rz'))return;
  const cell=ev.target.closest('[data-col]');
  if(ev.metaKey||ev.ctrlKey){
    if(!cell)return;
    const a=anchorFor(cell); if(!a)return;
    ev.preventDefault();
    openChat(a,labelFor(cell),cell);
    return;
  }
  const trigger=ev.target.closest('.jsonOpen');
  if(trigger&&cell){ev.preventDefault();openCellModal(cell,trigger);}
});
content.addEventListener('dblclick',ev=>{
  const cell=ev.target.closest('td[data-col]'); if(!cell)return;
  openCellModal(cell);
});
let rz=null;
content.addEventListener('mousedown',ev=>{
  const h=ev.target.closest('.rz'); if(!h)return;
  ev.preventDefault(); ev.stopPropagation();
  const th=h.closest('th'), table=th.closest('table');
  rz={th,table,col:th.dataset.col,x:ev.clientX,w:th.offsetWidth,tw:table.offsetWidth};
});
document.addEventListener('mousemove',ev=>{
  if(!rz)return;
  const w=Math.max(56, rz.w+ev.clientX-rz.x);
  rz.th.style.width=w+'px';
  rz.table.style.width=(rz.tw+(w-rz.w))+'px';   // grow/shrink the table with the column
  colW[rz.col]=w;
});
document.addEventListener('mouseup',()=>{ if(rz){localStorage.setItem('observer_colw',JSON.stringify(colW)); rz=null;} });
function openCellModal(cell,trigger=null){
  const clone=cell.cloneNode(true); const dot=clone.querySelector('.chatdot'); if(dot)dot.remove();
  const tr=cell.closest('tr'); const who=tr?(tr.dataset.name||tr.dataset.co||''):'';
  document.getElementById('cellmodalhead').textContent=(who?who+' · ':'')+cell.dataset.col;
  const body=document.getElementById('cellmodalbody'), jsonTrigger=trigger||cell.querySelector('.jsonOpen');
  body.classList.toggle('json',Boolean(jsonTrigger));
  if(jsonTrigger){
    try{body.textContent=JSON.stringify(JSON.parse(jsonTrigger.dataset.json),null,2);}
    catch(e){body.textContent=jsonTrigger.dataset.json||'(empty)';}
  }else body.textContent=(clone.textContent||'').trim()||'(empty)';
  document.getElementById('cellmodal').classList.add('show');
}
function closeCellModal(){document.getElementById('cellmodal').classList.remove('show');}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeCellModal();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeChat();});
document.addEventListener('click',e=>{const pop=document.getElementById('chatpop');if(pop.style.display==='block'&&!pop.contains(e.target)&&!e.target.closest('[data-col]')&&!e.target.closest('.bridgeActions'))closeChat();});

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function hasOwn(obj,key){return Object.prototype.hasOwnProperty.call(obj,key)}
function resolvesRecordError(event){
  return /^(done|success|ok|complete|completed|resolved|fixed|synced|written|appended)$/i
    .test(String(event.status??event.condition??event.outcome??''));
}
function clearResolvedError(row,event){
  if(resolvesRecordError(event)&&!hasOwn(event,'error')&&hasOwn(row,'error')){
    if(row.__prev)row.__prev.error=row.error;
    delete row.error;
  }
}
function fmt(v){return v===true?'✓':v===false?'—':(v==null?'':(typeof v==='object'?JSON.stringify(v):String(v)));}
function jsonCell(v){
  const raw=JSON.stringify(v), count=Array.isArray(v)?v.length:Object.keys(v||{}).length;
  const label=Array.isArray(v)?`${count} item${count===1?'':'s'}`:`${count} field${count===1?'':'s'}`;
  return `<button type=button class=jsonOpen data-json="${esc(raw)}" title="Open full JSON"><span class=jsonGlyph>{ }</span><span>${label}</span></button>`;
}
function sidebarIcon(collapsed){
  const d=collapsed?'M10 8l4 4-4 4':'M14 8l-4 4 4 4';
  return `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2.5" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M9 5v14" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="${d}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
const BRAND_MARK={
  bg:'#edf6ff',
  fg:'#101820',
  accent:'#4aa3ff',
  svg:`<svg viewBox="0 0 32 32" aria-hidden="true"><rect x="3" y="5" width="26" height="19" rx="6" fill="none" stroke="currentColor" stroke-width="2.6"/><circle cx="12" cy="15" r="4.4" fill="none" stroke="currentColor" stroke-width="2.6"/><path d="M16 15h4.8a4.8 4.8 0 1 0 0-2.8" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/><path d="M7 24l-2 3M25 24l2 3" fill="none" stroke="var(--mark-accent)" stroke-width="2.6" stroke-linecap="round"/></svg>`
};
function faviconHref(){
  const m=BRAND_MARK;
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><style>:root{--mark-accent:${m.accent}}</style><rect width="32" height="32" rx="7" fill="${m.bg}"/><g color="${m.fg}">${m.svg.replace('<svg viewBox="0 0 32 32" aria-hidden="true">','').replace('</svg>','')}</g></svg>`;
  return 'data:image/svg+xml,'+encodeURIComponent(svg);
}
function setBrandMark(){
  const m=BRAND_MARK;
  const box=document.getElementById('brandMark');
  box.style.background=m.bg; box.style.color=m.fg; box.style.setProperty('--mark-accent',m.accent);
  box.innerHTML=m.svg;
  document.getElementById('favicon').href=faviconHref();
}
function flatChat(){
  return Object.entries(chatByAnchor).flatMap(([anchor,msgs])=>msgs.map(m=>Object.assign({anchor},m)));
}
function bridgeSummary(){
  if(!sel)return {title:'No run selected',desc:'Pick a run to inspect.',state:'Idle',cls:'idle',finished:false,dryRun:false};
  const events=attemptEvents();
  const started=[...events].find(e=>(e.event||e.action)==='run_started')||{};
  const finished=[...events].reverse().find(e=>['run_finished','run_failed','run_abandoned','run_paused'].includes(e.event||e.action));
  const failed=finished&&['run_failed','run_abandoned'].includes(finished.event||finished.action);
  const desc=selMeta?.desc||started.description||started.name||selMeta?.name||selMeta?.label||'';
  const dryRun=Boolean(started.dry_run);
  if(failed)return {title:'Run needs attention',desc,state:'Failed',cls:'attn',finished:true,dryRun};
  if(finished&&(finished.event||finished.action)==='run_paused')return {title:'Run paused safely',desc,state:'Paused',cls:'attn',finished:true,dryRun};
  if(finished)return {title:dryRun?'Dry-run sample finished':'Run finished',desc,state:'Finished',cls:'done',finished:true,dryRun};
  if(currentLocks.some(l=>l.alive))return {title:'Run is writing now',desc,state:'Running',cls:'live',finished:false,dryRun};
  return {title:'Run selected',desc,state:'Ready',cls:'done',finished:false,dryRun};
}
function controlAvailability(){
  const summary=bridgeSummary();
  const active=currentLocks.some(l=>l.alive);
  return {pause:active,stop_after_record:active,approve_full_run:summary.finished&&summary.dryRun};
}
function controlStates(){
  const latest=Object.create(null), acknowledged=new Set();
  for(const control of controls)latest[control.kind]=control;
  for(const event of all){
    if(eventName(event)==='control_acknowledged'&&event.control_id)acknowledged.add(String(event.control_id));
  }
  return Object.fromEntries(['pause','stop_after_record','approve_full_run'].map(kind=>{
    const control=latest[kind];
    return [kind,{control,accepted:Boolean(control&&acknowledged.has(String(control.id)))}];
  }));
}
function renderBridge(){
  const box=document.getElementById('locks'); if(!box)return;
  const msgs=flatChat().filter(m=>m.kind!=='control');
  const userNotes=msgs.filter(m=>m.author==='user');
  const unresolved=userNotes.filter(m=>!msgs.some(r=>r.author==='agent'&&r.anchor===m.anchor&&r.resolved)).length;
  const last=userNotes[userNotes.length-1];
  const controlState=controlStates();
  const active=currentLocks.filter(l=>l.alive);
  const summary=bridgeSummary();
  const badge=active.length?'Live write':summary.state;
  const badgeCls='bridgeBadge '+(active.length?'live':summary.cls);
  const note=sel
    ? unresolved
      ? `<b>${unresolved} message${unresolved>1?'s':''} waiting for the agent.</b> The active session receives these through its watcher.`
      : last
        ? `Last message to the agent was ${esc(relAge(last.ts))}.`
        : `No messages for the agent yet.`
    : `Pick a run to see its status and messages.`;
  const lockHtml=active.length
    ? `<div class=bridgeLock>${active.map(l=>`<div class=lock><span class=live>●</span> <b>${esc(l.scope)}</b><br><small style="color:var(--dim)">process ${l.pid} · since ${esc(l.started||'?')}</small></div>`).join('')}</div>`
    : '';
  const available=controlAvailability();
  const controlButton=(kind,label)=>{
    const state=controlState[kind], mode=state.accepted?'accepted':state.control?'requested':(kind==='approve_full_run'?'':'warn');
    if(!available[kind]&&!state.accepted)return '';
    const title=state.accepted?`${label} accepted by the worker`:state.control?`${label} requested from the worker`:`Request ${label.toLowerCase()}`;
    const buttonLabel=state.accepted?`${label} accepted`:state.control?`${label} requested`:label;
    const action=state.control?`requestControl('${kind}')`:(kind==='pause'?`openControlChat('pause','Pause','pausing this run')`:kind==='stop_after_record'?`openControlChat('stop_after_record','Stop after this record','stopping after this record')`:`requestControl('${kind}')`);
    return `<button class="controlBtn ${mode}" title="${title}" aria-label="${title}" ${state.control?'disabled':''} onclick="${action}">${controlIcon(state.accepted?'accepted':kind)}<span>${buttonLabel}</span></button>`;
  };
  const controlsHtml=sel?[controlButton('pause','Pause'),controlButton('stop_after_record','Stop after this record'),controlButton('approve_full_run','Approve full run')].filter(Boolean).join(''):'';
  const actions=sel?`<div class=bridgeActions><button class="chatbtn" onclick="openRunChat()">Message agent</button>${controlsHtml}</div>`:'';
  box.innerHTML=`<div class=bridgeTop><div><div class=bridgeTitle>${esc(summary.title)}</div>${summary.desc?`<div class=bridgeDesc>${esc(summary.desc)}</div>`:''}</div><span class="${badgeCls}">${badge}</span></div>
    <div class=bridgeGrid>
      <div class=bridgeMetric><b>${active.length}</b><small>active process${active.length===1?'':'es'}</small></div>
      <div class=bridgeMetric><b>${unresolved}</b><small>message${unresolved===1?'':'s'} for agent</small></div>
    </div>
    <div class=bridgeNote>${note}</div>${actions}${lockHtml}`;
}
// Generic outcome coloring — classify a value into ok/warn/err/dim by a universal
// vocabulary (source, sink, status, condition all read the same way). No per-workflow
// hardcoding; returns '' for values that aren't outcome-ish (names, ids, free text).
function outcomeClass(v){
  const s=String(v).trim().toLowerCase();
  if(!s||s==='—'||s==='-'||s==='n/a'||s==='na')return 'dim';
  if(/\\b(?:fail\w*|error\w*|refus\w*|reject\w*|timeout|exception|invalid|denied)\\b|✗|❌|\\b[45]\d\d\\b/.test(s))return 'err';
  if(/(skip|not met|not_met|excluded|exclude|held|blocked|pending|queued|searching|missing|unmatched)/.test(s))return 'warn';
  if(/(done|ok|success|inserted|upserted|pushed|written|verified|created|updated|added|appended|found|matched|sent|complete|synced|✓|^yes$|^true$)/.test(s))return 'ok';
  return '';
}
function parseTs(ts){
  if(!ts)return 0;
  const t=Date.parse(String(ts).replace(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})$/,'$1Z'));
  return Number.isFinite(t)?t:0;
}
function relAge(ts){
  const t=parseTs(ts); if(!t)return 'unknown';
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  if(s<60)return s+'s ago';
  const m=Math.floor(s/60); if(m<60)return m+'m '+(s%60)+'s ago';
  const h=Math.floor(m/60); return h+'h '+(m%60)+'m ago';
}
function isAttentionRecord(r){
  return r.error!==undefined&&r.error!==null&&String(r.error).trim()!=='';
}
function recordGroups(events){
  // Ledger mechanics belong in Timeline/Run info, not as repeated data columns.
  const SKIP=new Set(['ts','event','action','_file','key','table','__prev','attempt','dry_run','operation_key','payload_sha256']);
  const groups=Object.create(null), gorder=[];
  for(const e of (events||attemptEvents()).filter(e=>(e.event||e.action)==='record')){
    const t=e.table||'records';
    if(!hasOwn(groups,t)){groups[t]={rows:Object.create(null),order:[],cols:[]};gorder.push(t);}
    const g=groups[t];
    const k=String(e.key ?? e.company ?? e.name ?? JSON.stringify(e));
    let r=g.rows[k];
    if(!hasOwn(g.rows,k)){r=Object.create(null);r.__prev=Object.create(null);g.rows[k]=r;g.order.push(k);}
    clearResolvedError(r,e);
    for(const f of Object.keys(e)){
      if(SKIP.has(f))continue;
      if(!g.cols.includes(f))g.cols.push(f);
      const v=e[f];
      if(r[f]!==undefined&&r[f]!==v)r.__prev[f]=r[f];
      r[f]=v;
    }
  }
  return {groups,gorder};
}
function filterKind(rows,column){
  const values=rows.map(r=>r[column]).filter(v=>v!==undefined&&v!==null&&v!=='');
  if(values.length&&values.every(v=>v===true||v===false||String(v).toLowerCase()==='true'||String(v).toLowerCase()==='false'))return 'boolean';
  if(values.length&&values.every(v=>Number.isFinite(Number(v))))return 'number';
  const distinct=new Set(values.map(v=>String(fmt(v))));
  return distinct.size<=12?'category':'text';
}
function filterOperators(kind){
  if(kind==='boolean')return [['true','is true'],['false','is false']];
  if(kind==='number')return [['eq','equal to'],['gt','greater than'],['lt','less than'],['gte','greater than or equal to'],['lte','less than or equal to'],['empty','is empty'],['not_empty','is not empty'],['between','between']];
  if(kind==='category')return [['contains','contains'],['not_contains','does not contain'],['empty','is empty'],['not_empty','is not empty'],['eq','equal to'],['neq','not equal to']];
  return [['contains','contains'],['not_contains','does not contain'],['empty','is empty'],['not_empty','is not empty'],['eq','equal to'],['neq','not equal to']];
}
function rowsMatchFilters(rows, table){
  const state=tableFilters[table];
  if(!state||(!state.and.length&&!state.groups.length))return rows;
  const matches=(row,filter)=>{
    const raw=row[filter.column], empty=raw===undefined||raw===null||raw==='';
    if(filter.kind==='boolean'){
      const value=raw===true||String(raw).toLowerCase()==='true';
      return filter.op==='true'?value:!value;
    }
    if(filter.op==='empty')return empty;
    if(filter.op==='not_empty')return !empty;
    if(empty)return false;
    if(filter.kind==='number'){
      const value=Number(raw), first=Number(filter.value), second=Number(filter.value2);
      if(!Number.isFinite(value))return false;
      if(filter.op==='eq')return value===first;
      if(filter.op==='gt')return value>first;
      if(filter.op==='gte')return value>=first;
      if(filter.op==='lt')return value<first;
      if(filter.op==='lte')return value<=first;
      return value>=Math.min(first,second)&&value<=Math.max(first,second);
    }
    const value=String(fmt(raw)).toLowerCase(), expected=String(filter.value??'').toLowerCase();
    if(filter.op==='contains')return value.includes(expected);
    if(filter.op==='not_contains')return !value.includes(expected);
    if(filter.op==='eq')return value===expected;
    return value!==expected;
  };
  return rows.filter(row=>state.and.every(filter=>matches(row,filter))&&
    state.groups.every(group=>group.filters.some(filter=>matches(row,filter))));
}
function toggleFilters(table, cols){
  if(filterOpen===table){filterOpen=null;filterDraft=null;}
  else {filterOpen=table;filterDraft={table,column:cols[0]||'',op:'contains',value:'',value2:'',target:'and'};}
  _filterVersion++;render();
}
function setFilterDraft(field, value){
  if(!filterDraft)return;
  filterDraft={...filterDraft,[field]:value};
  if(field==='column'){
    const kind=filterDraft.kindFor?.[value]||'text';
    filterDraft.op=filterOperators(kind)[0][0];filterDraft.value='';filterDraft.value2='';
  }
  _filterVersion++;render();
}
function setFilterDraftValue(field, value){
  if(filterDraft)filterDraft={...filterDraft,[field]:value};
}
function applyFilter(table, kinds){
  if(!filterDraft?.column)return;
  const kind=kinds[filterDraft.column]||'text';
  const op=filterDraft.op;
  if(kind!=='boolean'&&!['empty','not_empty'].includes(op)&&String(filterDraft.value??'')==='')return;
  if(op==='between'&&String(filterDraft.value2??'')==='')return;
  const state=tableFilters[table]||{and:[],groups:[]};
  const filter={id:`f${Date.now().toString(36)}`,column:filterDraft.column,op,kind,value:filterDraft.value,value2:filterDraft.value2};
  if(filterDraft.target==='and')state.and.push(filter);
  else if(filterDraft.target==='new_group')state.groups.push({id:`g${Date.now().toString(36)}`,filters:[filter]});
  else {
    const group=state.groups.find(g=>`group:${g.id}`===filterDraft.target);
    if(group)group.filters.push(filter);
    else state.and.push(filter);
  }
  tableFilters[table]=state;
  filterOpen=null;filterDraft=null;_filterVersion++;render();
}
function removeFilter(table, target, filterId){
  const state=tableFilters[table];if(!state)return;
  if(target==='and')state.and=state.and.filter(filter=>filter.id!==filterId);
  else {
    const group=state.groups.find(g=>`group:${g.id}`===target);
    if(group)group.filters=group.filters.filter(filter=>filter.id!==filterId);
    state.groups=state.groups.filter(group=>group.filters.length);
  }
  _filterVersion++;render();
}
function filterControls(table, cols, rows){
  const state=tableFilters[table]||{and:[],groups:[]};
  const kinds=Object.fromEntries(cols.map(c=>[c,filterKind(rows,c)]));
  const chip=(f,target)=>{
    const label=['empty','not_empty','true','false'].includes(f.op)?filterOperators(f.kind).find(x=>x[0]===f.op)?.[1]:`${filterOperators(f.kind).find(x=>x[0]===f.op)?.[1]||f.op} ${f.value}${f.op==='between'?` and ${f.value2}`:''}`;
    return `<span class=filterChip>${esc(f.column)} ${esc(label)}<button title="Remove filter" aria-label="Remove ${esc(f.column)} filter" onclick="removeFilter(${esc(JSON.stringify(table))},${esc(JSON.stringify(target))},${esc(JSON.stringify(f.id))})">×</button></span>`;
  };
  const andChips=state.and.map(f=>chip(f,'and')).join('');
  const groupChips=state.groups.map((group,index)=>`<span class=filterGroup><small>OR group ${index+1}</small>${group.filters.map(f=>chip(f,`group:${group.id}`)).join('<span class=filterJoin>OR</span>')}</span>`).join('');
  const toggle=`<button class=filterToggle onclick="toggleFilters(${esc(JSON.stringify(table))},${esc(JSON.stringify(cols))})">Filter columns</button>`;
  if(filterOpen!==table)return `<div class=tableTools>${toggle}${andChips}${groupChips}</div>`;
  if(!filterDraft||filterDraft.table!==table)filterDraft={table,column:cols[0]||'',op:'contains',value:'',value2:'',target:'and',kindFor:kinds};
  filterDraft.kindFor=kinds;
  const kind=kinds[filterDraft.column]||'text';
  const ops=filterOperators(kind);
  const noValue=['empty','not_empty'].includes(filterDraft.op)||kind==='boolean';
  const values=[...new Set(rows.map(r=>r[filterDraft.column]).filter(v=>v!==undefined&&v!==null&&v!=='').map(v=>String(fmt(v))))].sort();
  const valueField=kind==='boolean'
    ?'<span></span>'
    :kind==='category'
    ?`<select onchange="setFilterDraft('value',this.value)" ${noValue?'disabled':''}><option value="">Choose value</option>${values.map(v=>`<option value="${esc(v)}" ${v===filterDraft.value?'selected':''}>${esc(v)}</option>`).join('')}</select>`
    :`<input type="${kind==='number'?'number':'text'}" value="${esc(filterDraft.value)}" ${noValue?'disabled':''} placeholder="Value" oninput="setFilterDraftValue('value',this.value)">`;
  const second=kind==='number'&&filterDraft.op==='between'
    ?`<input type="number" value="${esc(filterDraft.value2)}" placeholder="And value" oninput="setFilterDraftValue('value2',this.value)">`
    :'<span></span>';
  const targets=[['and','All filters (AND)'],['new_group','New OR group'],...state.groups.map((group,index)=>[`group:${group.id}`,`OR group ${index+1}`])];
  return `<div class=tableTools>${toggle}${andChips}${groupChips}</div><div class=filterPanel><select onchange="setFilterDraft('column',this.value)">${cols.map(c=>`<option value="${esc(c)}" ${c===filterDraft.column?'selected':''}>${esc(c)}</option>`).join('')}</select><select onchange="setFilterDraft('op',this.value)">${ops.map(([value,label])=>`<option value="${value}" ${value===filterDraft.op?'selected':''}>${label}</option>`).join('')}</select>${valueField}${second}<select onchange="setFilterDraft('target',this.value)">${targets.map(([value,label])=>`<option value="${esc(value)}" ${value===filterDraft.target?'selected':''}>${esc(label)}</option>`).join('')}</select><button class=filterAction onclick="applyFilter(${esc(JSON.stringify(table))},${esc(JSON.stringify(kinds))})">Add filter</button></div>`;
}
function renderRecordTable(groups, gorder, label){
  if(!gorder.length)return null;
  if(!gorder.includes(recTab))recTab=gorder[0];
  const g=groups[recTab];
  const baseKeys=view==='attention' ? g.order.filter(k=>isAttentionRecord(g.rows[k])) : g.order;
  const allRows=baseKeys.map(k=>g.rows[k]);
  if(view==='attention'&&!baseKeys.length)return '<div class=empty>No records need attention right now.</div>';
  const always=new Set(['company','name','status','source_status','linkedin_status','contact_status','error']);
  let cols=g.cols.filter(c=>{
    const filled=allRows.filter(r=>r[c]!==undefined&&r[c]!==null&&r[c]!=='').length;
    if(!filled)return false;
    return always.has(c)||filled>=Math.max(1, Math.ceil(allRows.length*.02));
  });
  if(cols.includes('company')&&cols.includes('name')){
    const same=allRows.filter(r=>String(r.company??'')===String(r.name??'')).length;
    if(same>=allRows.length*.95)cols=cols.filter(c=>c!=='name');
  }
  if(!cols.length)return '<div class=empty>No populated columns for these rows yet.</div>';
  const filteredKeys=baseKeys.filter(k=>rowsMatchFilters([g.rows[k]],recTab).length);
  const rowKeys=filteredKeys;
  const visibleRows=rowKeys.map(k=>g.rows[k]);
  const cats=catColumns(allRows, cols);
  const gcell=(c,v,row)=>{
    const structured=v!==null&&typeof v==='object';
    const disp=structured?jsonCell(v):esc(fmt(v));
    const previous=row.__prev?.[c];
    // Status is the row's current lifecycle, while sink outcomes benefit from history.
    const was=!structured&&c!=='status'&&previous!==undefined&&previous!==v
      ? ` <small style="color:var(--warn)">· was ${esc(fmt(previous))}</small>`:'';
    if(cats.has(c)&&v!=null&&v!=='')return `<span class="pill ${outcomeClass(v)||'dim'}">${disp}</span>${was}`;
    return disp+was;
  };
  const ROW_NUMBER_W=54;
  const gbase=Object.fromEntries(cols.map(c=>[c,colW[recTab+'::'+c]??COLW_DEFAULT[c]??150]));
  if(!cols.some(c=>colW[recTab+'::'+c]!=null)){
    const avail=(content.clientWidth||1000)-4, sum=ROW_NUMBER_W+cols.reduce((s,c)=>s+gbase[c],0);
    if(sum<avail){const kk=avail/sum;cols.forEach(c=>gbase[c]=Math.round(gbase[c]*kk));}
  }
  const gtot=ROW_NUMBER_W+cols.reduce((s,c)=>s+gbase[c],0);
  const hasSubtabs=gorder.length>1;
  const subtabs=hasSubtabs
    ? `<div class=subtabs>`+gorder.map(t=>`<span class="subtab ${t===recTab?'sel':''}" onclick="setRecTab(${esc(JSON.stringify(t))})">${esc(t)} <small>· ${groups[t].order.length}</small></span>`).join('')+'</div>'
    : '';
  const tools=filterControls(recTab,cols,allRows);
  const ordinals=Object.create(null); g.order.forEach((key,index)=>{ordinals[key]=index+1;});
  const rrow=(k)=>{const r=g.rows[k], ordinal=ordinals[k];
    return `<tr data-key="${esc(recTab+'::'+k)}" data-co="${esc(k)}" data-name="${esc(k)}">`+
      `<td class=rownum>${ordinal}</td>`+
      cols.map((c,i)=>`<td class="${i===0?'datafirst':''}" data-col="${esc(recTab+'::'+c)}">${gcell(c,r[c],r)}</td>`).join('')+`</tr>`;
  };
  const thead=`<thead><tr><th class=rownum style="width:${ROW_NUMBER_W}px">#</th>${cols.map((c,i)=>`<th class="${i===0?'datafirst':''}" data-col="${esc(recTab+'::'+c)}" style="width:${gbase[c]}px">${esc(c)}<span class=rz></span></th>`).join('')}</tr></thead>`;
  // small tables (≤500 rows): build in one shot
  if(rowKeys.length<=500)
    return `${label?`<div class=card><h4>${esc(label)}</h4></div>`:''}<div class="recordshell${hasSubtabs?' hasSubtabs':''}${filterOpen===recTab?' filtersOpen':''}" style="height:${contentViewportHeight()}px">${subtabs}${tools}<div class=tablewrap><table style="width:${gtot}px">${thead}<tbody>${rowKeys.map(rrow).join('')}</tbody></table></div></div>`;
  // Large tables build off-screen in chunks. Keep the current table interactive
  // until the replacement is complete, then swap once and restore its latest
  // viewport. Restoring against an empty tbody clamps scrollTop to zero.
  const shell=document.createElement('div');
  shell.innerHTML=`${label?`<div class=card><h4>${esc(label)}</h4></div>`:''}<div class="recordshell${hasSubtabs?' hasSubtabs':''}${filterOpen===recTab?' filtersOpen':''}" style="height:${contentViewportHeight()}px">${subtabs}${tools}<div class=tablewrap><table style="width:${gtot}px">${thead}<tbody></tbody></table></div></div>`;
  const tbody=shell.querySelector('.tablewrap tbody');
  let aborted=false;
  const abort=()=>{aborted=true};
  _buildAbort=abort;
  const BATCH=500;let idx=0;
  function appendBatch(){
    if(aborted)return;
    const end=Math.min(idx+BATCH, rowKeys.length);
    let rows='';
    for(; idx<end; idx++)rows+=rrow(rowKeys[idx]);
    tbody.insertAdjacentHTML('beforeend', rows);
    if(idx<rowKeys.length)setTimeout(appendBatch, 0);
    else {
      if(_buildAbort===abort)_buildAbort=null;
      const latestScroll=captureTableScroll();
      content.replaceChildren(shell);
      decorateChat();
      restoreTableScroll(latestScroll);
    }
  }
  appendBatch();
  return null; // chunked — caller skips content.innerHTML assignment
}
// A column is "categorical" (worth coloring + counting) if it repeats values and
// has few distinct ones — that targets status/source/sink columns and skips names/ids.
function catColumns(rows, cols){
  const cats=new Set();
  for(const c of cols){
    const vals=rows.map(r=>r[c]).filter(v=>v!=null&&v!=='');
    if(!vals.length||vals.every(v=>typeof v==='number'))continue;
    const distinct=new Set(vals.map(v=>String(fmt(v))));
    if(distinct.size<=12 && distinct.size<vals.length)cats.add(c);
  }
  return cats;
}

const TERMINAL_META=new Set(['ts','event','action','_file','attempt','status','dry_run','checkpoints','summary_metrics']);
function numericSummaryEntries(value,prefix='',depth=0,out=[]){
  if(!value||typeof value!=='object'||Array.isArray(value)||depth>3)return out;
  for(const [key,item] of Object.entries(value)){
    if(!prefix&&TERMINAL_META.has(key))continue;
    const path=prefix?`${prefix} ${key}`:key;
    if(typeof item==='number'&&Number.isFinite(item))out.push([path,item]);
    else if(item&&typeof item==='object'&&!Array.isArray(item))numericSummaryEntries(item,path,depth+1,out);
  }
  return out;
}
function terminalSummaryText(event){
  const numeric=numericSummaryEntries(event).slice(0,8);
  if(numeric.length)return numeric.map(([key,value])=>`${esc(key.replaceAll('_',' '))}: ${esc(value)}`).join(', ');
  return Object.entries(event).filter(([key])=>!TERMINAL_META.has(key))
    .map(([key,value])=>`${esc(key.replaceAll('_',' '))}: ${esc(typeof value==='object'?JSON.stringify(value):value)}`).join(', ');
}

// Turn a raw event into {icon, text, cls, company, detail} — plain English.
function humanize(e){
  const ev=e.action||e.event||'';
  const who=e.name?`<b>${esc(e.name)}</b>`:'';
  const co=e.company?` at ${esc(e.company)}`:'';
  switch(ev){
    case 'run_started': return {icon:'▶️',cls:'info',text:`Run started — ${e.companies??e.todo??'?'} companies`+(e.worst_case_credits?`, spend ceiling ${e.worst_case_credits} credits`:'')};
    case 'run_finished': return {icon:'🏁',cls:'info',text:`Run finished — ${terminalSummaryText(e)}`};
    case 'run_abandoned': return {icon:'⚠',cls:'err',text:`Run abandoned — ${esc(e.error||'process exited before closing the run')}`};
    case 'run_paused': return {icon:'Ⅱ',cls:'warn',text:`Run paused safely — ${esc(e.reason||'operator or quality gate request')}`};
    case 'run_manifest': return {icon:'•',cls:'dim',text:`Run manifest recorded${e.destination?` · destination ${esc(e.destination)}`:''}${e.transform_version?` · transform ${esc(e.transform_version)}`:''}`};
    case 'input_changed': return {icon:'!',cls:'warn',text:'Input changed since the prior attempt — review before resuming'};
    case 'impact_preview': return {icon:'◌',cls:'info',text:`Impact preview — ${e.sample_count??0} sample row${e.sample_count===1?'':'s'}${e.estimates?` · ${esc(JSON.stringify(e.estimates))}`:''}`};
    case 'schema_violation': return {icon:'!',cls:'err',text:`Schema check blocked ${esc(e.key||'a record')} — ${esc((e.errors||[])[0]||'invalid data')}`};
    case 'policy_blocked': return {icon:'!',cls:'warn',text:`Policy blocked ${esc(e.key||'a write')} — ${esc((e.errors||[])[0]||'rule failed')}`};
    case 'quality_gate': return {icon:e.status==='failed'?'!':'✓',cls:e.status==='failed'?'warn':'ok',text:`Quality gate ${esc(e.gate||'check')} — ${esc(e.observed)}${e.status==='failed'?' (paused)':''}`};
    case 'write_intent': return {icon:'→',cls:'info',text:`Write reserved for ${esc(e.record_key||'record')} to ${esc(e.destination||'destination')}`};
    case 'write_preview': return {icon:'◌',cls:'info',text:`Dry-run write preview for ${esc(e.record_key||'record')} to ${esc(e.destination||'destination')}`};
    case 'write_receipt': return {icon:'✓',cls:'ok',text:`Write ${esc(e.status||'completed')} for ${esc(e.record_key||'record')} to ${esc(e.destination||'destination')}`};
    case 'write_skipped': return {icon:'•',cls:'warn',text:`Write skipped — ${esc(e.reason||'already recorded')}`};
    case 'write_blocked': return {icon:'!',cls:'warn',text:`Write blocked — ${esc(e.reason||'receipt needed')}`};
    case 'dead_letter': return {icon:'!',cls:'err',text:`Replay candidate: ${esc(e.record_key||'record')} — ${esc(e.error||'failed')}`};
    case 'reconciliation': return {icon:'✓',cls:'info',text:`Reconciliation — ${e.written??0} written, ${e.pending??0} pending, ${e.dead_letters??0} replay candidate${e.dead_letters===1?'':'s'}`};
    case 'control_acknowledged': return {icon:'•',cls:'info',text:`Control acknowledged — ${esc(String(e.control||'').replaceAll('_',' '))}`};
    case 'simulation': return {icon:'◌',cls:'info',text:`Simulation fixture loaded — ${esc(e.records??0)} records`};
    case 'schema_observed': return {icon:'{ }',cls:'info',text:`Observed ${Object.keys(e.paths||{}).length} JSON field paths for ${esc(e.table||'source data')} from ${esc(e.sample_count??1)} sample${e.sample_count===1?'':'s'}`};
    case 'flow_graph': return {icon:'◇',cls:'info',text:`Flow plan loaded — ${esc(e.graph?.label||e.graph?.id||e.graph_id||'dependency graph')}${e.rows_total!==undefined?` · ${esc(e.rows_total)} rows`:''}`};
    case 'flow_node': return {icon:e.status==='failed'?'!':e.status==='complete'?'✓':'↻',cls:e.status==='failed'?'err':e.status==='complete'?'ok':'info',text:`${esc(e.node_label||e.node_id||'Node')} — ${esc(flowStatusLabel(e.status))}${e.completed!==undefined&&e.total!==undefined?` · ${esc(e.completed)} / ${esc(e.total)}`:''}`};
    case 'flow_batch': return {icon:e.status==='failed'?'!':'▦',cls:e.status==='failed'?'err':e.status==='complete'?'ok':'info',text:`${esc(e.node_label||e.node_id||'Batch node')} · batch ${esc(e.position||e.batch_id||'?')}${e.total_batches?` / ${esc(e.total_batches)}`:''} — ${esc(flowStatusLabel(e.status))}${e.items!==undefined?` · ${esc(e.items)} rows`:''}${e.spend_units!==undefined?` · ${esc(e.spend_units)} units`:''}`};
    case 'flow_unit': return {icon:e.status==='failed'?'!':e.status==='held'?'Ⅱ':'·',cls:e.status==='failed'?'err':e.status==='held'?'warn':'dim',text:`${esc(e.node_label||e.node_id||'Node')} · ${esc(e.key||'row')} — ${esc(flowStatusLabel(e.status))}${e.reason?` <small>(${esc(e.reason)})</small>`:''}`,quiet:!['failed','held'].includes(String(e.status))};
    case 'progress': {
      const phase=esc(e.phase||'progress');
      const pct=(e.done!==undefined&&e.total)?` (${Math.round((Number(e.done)/Number(e.total))*100)}%)`:'';
      const amount=(e.done!==undefined&&e.total!==undefined)?`${e.done} / ${e.total}`:(e.done??e.value??'updated');
      return {icon:'↻',cls:'info',text:`${phase} — ${esc(amount)}${pct}`+(e.note?` <small>(${esc(e.note)})</small>`:'')};
    }
    case 'checkpoint': return {icon:'↻',cls:'info',text:`Checkpoint — ${esc(e.checkpoint||e.name||'progress')}${e.value!==undefined?`: ${esc(e.value)}`:''}`};
    case 'bc_submitted': return {icon:'📤',cls:'info',text:`Round ${e.round??'?'}: requested ${e.leads} lookup${e.leads>1?'s':''} from the provider`,detail:(e.contacts||[]).map(c=>`${c.name} (${c.company})`).join(', ')};
    case 'credits': return {icon:'💳',cls:'warn',text:`${e.provider||'Provider'} credits — used ${e.used??e.credits_consumed??'?'}${(e.left??e.credits_left)!==undefined?`, ${e.left??e.credits_left} left`:''}`};
    case 'bc_credits': return {icon:'💳',cls:'warn',text:`Provider credits — used ${e.credits_consumed??'?'}, remaining ${e.credits_left??'?'}`};
    case 'bc_poll_timeout': return {icon:'⏱',cls:'err',text:`The provider took too long to answer (request ${e.request_id})`};
    case 'phone_found': return {icon:'📞',cls:'ok',text:`Found phone for ${who}${co}: ${esc(e.phone)}`,company:e.company,record:{name:e.name,phone:e.phone}};
    case 'phone_not_found': return {icon:'▫️',cls:'warn',text:`No phone found for ${who}${co}`,company:e.company,record:{name:e.name,phone:false}};
    case 'email_found': return {icon:'✉️',cls:'ok',text:`Found email for ${who}${co}: ${esc(e.email)} <small>(via ${esc(e.source)})</small>`,company:e.company,record:{name:e.name,email:e.email,source:e.source}};
    case 'email_not_found': return {icon:'▫️',cls:'warn',text:`No email found for ${who}${co}`,company:e.company,record:{name:e.name,email:false}};
  }
  // push library events.jsonl: {verb, phase, action, details}
  if(e.phase!==undefined&&e.action!==undefined){
    const d=e.details||{};
    const bits=Object.entries(d).map(([k,v])=>`${k.replaceAll('_',' ')}: ${typeof v==='object'?JSON.stringify(v):v}`).join(', ');
    const co2=d.company_name||d.domain||d.company;
    return {icon:'•',cls:e.level==='error'?'err':'info',
      text:`${esc(e.verb??'')} — ${esc(e.phase)} ${esc(String(e.action).replaceAll('_',' '))}`+(bits?` <small>(${esc(bits)})</small>`:''),
      company:co2};
  }
  // api-calls.jsonl: {provider, endpoint, status_code}
  if(e.endpoint!==undefined){
    const bad=e.status_code>=400;
    const mut=/POST|PATCH|PUT|DELETE/.test(e.endpoint)&&!/search/i.test(e.endpoint);
    let text;
    if(/associat/i.test(e.endpoint)) text=`CRM: linked two records (${esc(e.endpoint)})`;
    else if(/POST \\/companies|POST \\/contacts/.test(e.endpoint)) text=`CRM: created a record (${esc(e.endpoint)})`;
    else if(/PATCH/.test(e.endpoint)) text=`${esc(e.provider)}: updated a record (${esc(e.endpoint)})`;
    else text=`${esc(e.provider)}: ${esc(e.endpoint)}`;
    text+= bad?` — <b class=err>FAILED (${e.status_code})</b>`:'';
    return {icon:mut?'✏️':'·',cls:bad?'err':(mut?'info':'dim'),text,technical:!mut&&!bad};
  }
  const label=esc(ev||'event');
  const fields=Object.entries(e)
    .filter(([k,v])=>!['ts','event','action','_file'].includes(k)&&v!==undefined&&v!==null&&typeof v!=='object')
    .slice(0,4)
    .map(([k,v])=>`${k.replaceAll('_',' ')}: ${esc(v)}`)
    .join(' · ');
  return {icon:'·',cls:'info',text:fields?`${label} — ${fields}`:label};
}

let selMeta=null;

// Minimal markdown -> HTML for the "How it works" tab. Fenced ``` blocks become
// <pre> (keeps ASCII diagrams monospaced); #/##/### headings, - bullets, **bold**,
// `code`, and blank-line paragraphs. Not a full engine — just enough for a
// plain-English + ASCII explainer a non-developer can read.
function mdToHtml(md){
  const inline=s=>esc(s).replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>').replace(/`(.+?)`/g,'<code>$1</code>');
  const parts=String(md).split(/```/);
  let out='';
  parts.forEach((chunk,i)=>{
    if(i%2===1){ out+=`<pre class=diagram>${esc(chunk.replace(/^\\n/,'').replace(/\\n$/,''))}</pre>`; return; }
    chunk.split(/\\n{2,}/).forEach(block=>{
      const t=block.replace(/^\\n+|\\n+$/g,''); if(!t)return;
      const h=t.match(/^(#{1,3})\\s+(.*)$/);
      if(h){ out+=`<h${h[1].length+1}>${inline(h[2])}</h${h[1].length+1}>`; return; }
      if(/^\\s*[-*]\\s+/.test(t)){
        out+='<ul>'+t.split(/\\n/).filter(l=>/^\\s*[-*]\\s+/.test(l)).map(l=>`<li>${inline(l.replace(/^\\s*[-*]\\s+/,''))}</li>`).join('')+'</ul>';
        return;
      }
      out+=`<p>${inline(t).replace(/\\n/g,'<br>')}</p>`;
    });
  });
  return out;
}
async function loadExplain(){
  // Always re-fetch. This is a live statement of intent the operator uses to
  // verify the agent — a cached/stale version would defeat the whole point.
  try{ const r=await (await fetch('/api/explain')).json(); return r.found?r.markdown:''; }
  catch(e){ return ''; }
}
function eventName(e){return e.action||e.event||'';}
function latestAttemptIndex(){
  let idx=-1;
  for(let i=0;i<all.length;i++){
    if(eventName(all[i])==='run_started')idx=i;
  }
  return idx;
}
function recordWindowStart(){
  const latest=latestAttemptIndex();
  if(latest<0)return 0;
  const dry=Boolean(all[latest].dry_run);
  let start=latest;
  for(let i=latest-1;i>=0;i--){
    if(eventName(all[i])==='run_started'){
      if(Boolean(all[i].dry_run)!==dry)break;
      start=i;
    }
  }
  return start;
}
function attemptEvents(){
  const idx=latestAttemptIndex();
  return idx>=0 ? all.slice(idx) : all;
}
function recordEvents(){return all.slice(recordWindowStart());}
function progressEvents(){
  return attemptEvents().filter(e=>{
    const a=eventName(e);
    return a==='progress'||a==='checkpoint'||e.done!==undefined||e.total!==undefined||e.phase!==undefined;
  });
}
function priorAttemptEvents(){
  const idx=latestAttemptIndex();
  return idx>0 ? all.slice(0,idx) : [];
}
function attemptBanner(){
  const n=priorAttemptEvents().length;
  return n
    ? `<div class=card><small>Showing the latest attempt. ${n} earlier ledger event${n===1?' is':'s are'} kept in the JSONL history.</small></div>`
    : '';
}

function flowStateClass(value){
  const s=String(value||'pending').toLowerCase();
  if(['complete','completed','done','finished','success','succeeded','cached','skipped'].includes(s))return 'complete';
  if(s==='running'||s==='ready')return 'running';
  if(s==='failed'||s==='error')return 'failed';
  if(s==='held'||s==='paused')return 'held';
  return 'pending';
}
function flowStatusLabel(value){
  const s=String(value||'pending').replaceAll('_',' ');
  return s.charAt(0).toUpperCase()+s.slice(1);
}
function flowModel(){
  const events=attemptEvents();
  const graphEvent=[...events].reverse().find(e=>eventName(e)==='flow_graph');
  if(!graphEvent)return null;
  const graph=graphEvent.graph||graphEvent;
  const nodes=Array.isArray(graph.nodes)?graph.nodes:[];
  const edges=Array.isArray(graph.edges)?graph.edges:[];
  const states=Object.create(null), units=Object.create(null), batches=Object.create(null);
  for(const node of nodes){
    states[node.id]={node_id:node.id,status:'pending',total:Number(graphEvent.rows_total||graph.rows_total||0),succeeded:0,skipped:0,held:0,failed:0,cached:0,spend_units:0};
    units[node.id]=Object.create(null);
    batches[node.id]=Object.create(null);
  }
  for(const event of events){
    const kind=eventName(event), id=event.node_id;
    if(kind==='flow_node'&&id&&states[id])Object.assign(states[id],event);
    if(kind==='flow_unit'&&id&&units[id]&&event.key!==undefined)units[id][String(event.key)]=event;
    if(kind==='flow_batch'&&id&&batches[id]){
      const batchId=String(event.batch_id||event.position||Object.keys(batches[id]).length+1);
      batches[id][batchId]={...(batches[id][batchId]||{}),...event};
    }
  }
  for(const node of nodes){
    const state=states[node.id], rows=Object.values(units[node.id]);
    if(rows.length){
      for(const key of ['succeeded','skipped','held','failed','cached']){
        if(state[key]===undefined||state.derived_counts)state[key]=rows.filter(row=>String(row.status)===key).length;
      }
      const terminal=rows.filter(row=>['succeeded','skipped','held','failed','cached'].includes(String(row.status))).length;
      state.completed=Math.max(Number(state.completed||0),terminal);
    }
    state.total=Number(state.total||graphEvent.rows_total||graph.rows_total||0);
  }
  return {graphEvent,graph,nodes,edges,states,units,batches};
}
function flowLevels(nodes,edges){
  const ids=new Set(nodes.map(n=>n.id)), incoming=Object.create(null), level=Object.create(null);
  for(const id of ids){incoming[id]=[];level[id]=0;}
  for(const edge of edges)if(ids.has(edge.from)&&ids.has(edge.to))incoming[edge.to].push(edge.from);
  for(let pass=0;pass<nodes.length;pass++){
    let changed=false;
    for(const node of nodes){
      const next=incoming[node.id].length?Math.max(...incoming[node.id].map(id=>level[id]+1)):0;
      if(next!==level[node.id]){level[node.id]=next;changed=true;}
    }
    if(!changed)break;
  }
  const groups=[];
  for(const node of nodes){const n=Math.min(level[node.id]||0,nodes.length);(groups[n]||(groups[n]=[])).push(node);}
  return groups.filter(Boolean);
}
function flowIcon(kind){
  return ({source:'IN',extract:'{}',transform:'ƒ',decision:'IF',enrichment:'API',batch:'▦',review:'?',route:'↳',sink:'OUT',join:'Σ',expand:'1:N'})[String(kind||'').toLowerCase()]||'•';
}
function flowConditionText(node){
  if(!node.when)return 'Runs when dependencies are ready';
  if(typeof node.when==='string')return node.when;
  const leaf=(node.when.all||node.when.any||[])[0];
  if(leaf?.field)return `${leaf.field} ${String(leaf.op||'equals').replaceAll('_',' ')} ${leaf.value===undefined?'':JSON.stringify(leaf.value)}`.trim();
  return JSON.stringify(node.when);
}
function selectFlowNode(id){flowNodeSelected=id;flowRowSelected=null;_flowVersion++;render();}
function selectFlowRow(key){flowRowSelected=key;_flowVersion++;render();}
function showFlowJson(title,value){
  document.getElementById('cellmodalhead').textContent=title;
  const body=document.getElementById('cellmodalbody');body.classList.add('json');body.textContent=JSON.stringify(value,null,2);
  document.getElementById('cellmodal').classList.add('show');
}
function flowRecordFor(model,key){
  const row=Object.create(null), table=model.graph.table||model.graphEvent.table;
  for(const event of recordEvents()){
    if(eventName(event)!=='record'||String(event.key)!==String(key))continue;
    if(table&&event.table&&event.table!==table)continue;
    for(const [field,value] of Object.entries(event))if(!['ts','event','action','_file','key','table','attempt','dry_run'].includes(field))row[field]=value;
  }
  return row;
}
let _flowEdges=[];
function drawFlowEdges(){
  const graph=document.getElementById('flowGraph'),svg=document.getElementById('flowEdges');
  if(!graph||!svg)return;
  const box=graph.getBoundingClientRect(),width=graph.scrollWidth,height=graph.scrollHeight,ns='http://www.w3.org/2000/svg';
  svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('width',width);svg.setAttribute('height',height);
  svg.innerHTML='<defs><marker id="flowArrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#65798c"></path></marker></defs>';
  const cards=[...graph.querySelectorAll('.flowNode')];
  for(const edge of _flowEdges){
    const from=cards.find(card=>card.dataset.nodeId===String(edge.from)),to=cards.find(card=>card.dataset.nodeId===String(edge.to));
    if(!from||!to)continue;
    const a=from.getBoundingClientRect(),b=to.getBoundingClientRect();
    const x1=a.right-box.left,y1=a.top-box.top+a.height/2,x2=b.left-box.left,y2=b.top-box.top+b.height/2,mx=x1+(x2-x1)*.5;
    const path=document.createElementNS(ns,'path');
    path.setAttribute('d',`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`);
    path.setAttribute('fill','none');path.setAttribute('stroke','#65798c');path.setAttribute('stroke-width','1.6');path.setAttribute('marker-end','url(#flowArrow)');
    svg.appendChild(path);
    const label=edge.label||edge.when;
    if(label){const text=document.createElementNS(ns,'text');text.setAttribute('x',mx);text.setAttribute('y',(y1+y2)/2-6);text.setAttribute('text-anchor','middle');text.setAttribute('class','flowEdgeLabel');text.textContent=String(label);svg.appendChild(text);}
  }
}
function renderFlow(viewScroll){
  const model=flowModel();
  if(!model){content.innerHTML='<div class=empty>This run has no flow graph events.</div>';return;}
  const {graphEvent,graph,nodes,edges,states,units,batches}=model;
  if(!flowNodeSelected||!nodes.some(node=>node.id===flowNodeSelected)){
    flowNodeSelected=nodes.find(node=>flowStateClass(states[node.id]?.status)==='running')?.id||nodes[0]?.id||null;
  }
  const selected=nodes.find(node=>node.id===flowNodeSelected),selectedState=selected?states[selected.id]:null;
  const levels=flowLevels(nodes,edges),allKeys=new Set();
  for(const byKey of Object.values(units))for(const key of Object.keys(byKey))allKeys.add(key);
  const businessKeys=new Set(recordEvents().filter(e=>eventName(e)==='record'&&(!graph.table||!e.table||e.table===graph.table)).map(e=>String(e.key)));
  const scopedRows=Number(graphEvent.rows_total||graph.rows_total||0),observedRows=Math.max(allKeys.size,businessKeys.size);
  const rowMetric=scopedRows&&observedRows<scopedRows?`${observedRows}/${scopedRows}`:(observedRows||scopedRows);
  const active=nodes.filter(node=>flowStateClass(states[node.id]?.status)==='running');
  const spend=nodes.reduce((sum,node)=>sum+Number(states[node.id]?.spend_units||0),0);
  const finished=nodes.filter(node=>flowStateClass(states[node.id]?.status)==='complete').length;
  const nodeHtml=node=>{
    const state=states[node.id]||{},cls=flowStateClass(state.status),total=Number(state.total||0),completed=Number(state.completed||0),pct=total?Math.min(100,Math.round(completed/total*100)):(cls==='complete'?100:0);
    const succeeded=Number(state.succeeded||0)+Number(state.cached||0);
    return `<div class="flowNode ${cls} ${node.id===flowNodeSelected?'selected':''}" data-node-id="${esc(node.id)}" onclick="selectFlowNode(${esc(JSON.stringify(node.id))})"><div class=flowNodeTop><div class=flowNodeIcon>${esc(flowIcon(node.kind||node.mode))}</div><div><div class=flowNodeName>${esc(node.label||node.id)}</div><div class=flowNodeKind>${esc(node.kind||node.mode||'node')} · v${esc(node.version||'1')}</div></div><span class="flowState ${cls}">${esc(flowStatusLabel(state.status))}</span></div><div class=flowBar><span style="width:${pct}%"></span></div><div class=flowProgressLine>${completed}/${total||'—'} processed</div><div class=flowNodeStats><span><b>${succeeded}</b><small>succeeded</small></span><span><b>${Number(state.skipped||0)}</b><small>skipped</small></span><span><b>${Number(state.held||0)}</b><small>held</small></span><span><b>${Number(state.failed||0)}</b><small>failed</small></span></div></div>`;
  };
  const graphHtml=levels.map(level=>`<div class=flowLevel>${level.map(nodeHtml).join('')}</div>`).join('');
  const selectedUnits=selected?Object.values(units[selected.id]||{}).sort((a,b)=>String(b.ts||'').localeCompare(String(a.ts||''))):[];
  const selectedBatches=selected?Object.values(batches[selected.id]||{}).sort((a,b)=>Number(b.position||0)-Number(a.position||0)):[];
  if(flowRowSelected&&!selectedUnits.some(unit=>String(unit.key)===String(flowRowSelected)))flowRowSelected=null;
  const unitHtml=selectedUnits.length?selectedUnits.slice(0,40).map(unit=>`<div class="flowUnit ${String(unit.key)===String(flowRowSelected)?'selected':''}" onclick="selectFlowRow(${esc(JSON.stringify(String(unit.key)))})"><span class="flowStatusDot ${esc(String(unit.status||'pending'))}"></span><span class=flowUnitKey title="${esc(unit.key)}">${esc(unit.key)}</span><span class=flowUnitState>${esc(flowStatusLabel(unit.status))}</span></div>`).join(''):'<div class=dim>No rows have reached this node yet.</div>';
  const ports=selected?`<div class=flowMeta>${esc(flowConditionText(selected))}</div><small class=dim>Inputs</small><div class=flowPorts>${(selected.inputs||[]).map(port=>`<span class=flowPort>${esc(port)}</span>`).join('')||'<span class=dim>none</span>'}</div><small class=dim>Outputs</small><div class=flowPorts>${(selected.outputs||[]).map(port=>`<span class="flowPort out">${esc(port)}</span>`).join('')||'<span class=dim>none</span>'}</div>`:'';
  const batchHtml=selectedBatches.length?`<div class=flowBatches><small class=dim>Batch calls · ${selectedBatches.length}</small><div class=flowBatchList>${selectedBatches.map(batch=>`<div class=flowBatch><b>${esc(batch.batch_id||`batch ${batch.position||'?'}`)}</b><span>${esc(flowStatusLabel(batch.status))}</span><small>${esc(batch.items||0)} rows · ${esc(batch.spend_units||0)} units${batch.saved_units!==undefined?` · ${esc(batch.saved_units)} saved`:''}${batch.reused_response?' · reused response':''}</small></div>`).join('')}</div></div>`:'';
  let trace='<div class=dim>Select a row above to inspect its path through the graph.</div>';
  if(flowRowSelected){
    const steps=nodes.map((node,index)=>{const unit=units[node.id]?.[flowRowSelected],status=unit?.status||'pending';return `${index?'<span class=flowTraceArrow>→</span>':''}<div class=flowTraceStep><b><span class="flowStatusDot ${esc(String(status))}"></span>${esc(node.label||node.id)}</b><small>${esc(flowStatusLabel(status))}${unit?.reason?` · ${esc(unit.reason)}`:''}</small></div>`;}).join('');
    const row=flowRecordFor(model,flowRowSelected);
    trace=`<div class=flowTraceHead><div><b>${esc(flowRowSelected)}</b><div class=dim style="font-size:11.5px">Latest durable path for this row</div></div><button class=chatbtn onclick='showFlowJson(${esc(JSON.stringify(String(flowRowSelected)+" · row data"))},${esc(JSON.stringify(row))})'>Inspect row JSON</button></div><div class=flowTracePath>${steps}</div>`;
  }
  const plan=String(graphEvent.plan_id||graph.plan_id||'unversioned');
  content.innerHTML=`<div class=flowShell><div class=flowHead><div><div class=flowTitle>${esc(graph.label||graph.id||'Observed flow')}</div><div class=flowSub>${esc(graph.description||'Live dependency graph and per-row execution state')}</div></div><div class=flowPlan>plan ${esc(plan.slice(0,18))}${plan.length>18?'…':''}<br>${esc(graphEvent.dry_run?'review sample':'full run')}</div></div><div class=flowSummary><div class=flowMetric><b>${rowMetric}</b><small>rows observed</small></div><div class=flowMetric><b>${active.length?esc(active.map(node=>node.label||node.id).join(', ')):'Idle'}</b><small>running now</small></div><div class=flowMetric><b>${finished}/${nodes.length}</b><small>nodes complete</small></div><div class=flowMetric><b>${spend}</b><small>spend units</small></div></div><div class=flowCanvas><div class=flowGraph id=flowGraph><svg class=flowEdges id=flowEdges aria-hidden=true></svg>${graphHtml}</div></div><div class=flowInspector><div class=flowDetail><h4>${esc(selected?.label||selected?.id||'Node')}</h4><div class=flowMeta>${esc(selected?.script||'')} ${selected?.recipe?`· recipe ${esc(selected.recipe)}`:''}</div>${ports}${batchHtml}<button class=chatbtn onclick='showFlowJson(${esc(JSON.stringify((selected?.label||selected?.id||"Node")+" · definition"))},${esc(JSON.stringify(selected||{}))})'>Inspect node JSON</button></div><div class=flowRows><h4>Rows at this node <span class=dim>· ${selectedUnits.length}</span></h4><div class=flowUnitList>${unitHtml}</div></div><div class=flowTrace>${trace}</div></div></div>`;
  _flowEdges=edges;
  requestAnimationFrame(()=>{drawFlowEdges();if(viewScroll!==null&&viewScroll!==undefined)content.scrollTop=viewScroll;});
}

function render(){
  // skip full re-render when no new events and same view — avoids rebuilding 15k-row table every 2s
  if(all.length===_eventCount&&view===_lastView&&sel===_lastSel&&recTab===_lastRecTab&&_filterVersion===_lastFilterVersion&&_flowVersion===_lastFlowVersion)return;
  _buildAbort && _buildAbort();
  _buildAbort=null;
  const tableScroll=(view==='records'||view==='attention')?captureTableScroll():null;
  const flowScroll=view==='flow'?content.scrollTop:null;
  _eventCount=all.length;_lastView=view;_lastSel=sel;_lastRecTab=recTab;_lastFilterVersion=_filterVersion;_lastFlowVersion=_flowVersion;
  const hasFlow=attemptEvents().some(e=>eventName(e)==='flow_graph');
  document.getElementById('tabFlow').style.display=hasFlow?'block':'none';
  for(const [v,id] of Object.entries({records:'tabRecords',flow:'tabFlow',attention:'tabAttention',feed:'tabFeed',info:'tabInfo',explain:'tabExplain'}))
    document.getElementById(id).classList.toggle('sel',view===v);
  const tech=document.getElementById('tech').checked;
  const mapped=all.map(e=>({e,h:humanize(e)}));
  const nTech=mapped.filter(x=>x.h.technical).length;
  const techWrap=document.getElementById('techWrap');
  techWrap.style.display=(sel&&nTech)?'block':'none';
  document.getElementById('techCount').textContent=tech?`(showing ${nTech})`:`(${nTech} hidden)`;
  renderStats();

  // How it works — a static, non-technical explainer of the whole pipeline.
  if(view==='explain'){
    loadExplain().then(md=>{
      content.innerHTML = md
        ? `<div class="card explain">${mdToHtml(md)}</div>`
        : '<div class=empty>No explainer yet.<br><br>The agent should write an <b>EXPLAIN.md</b> here — a plain-English + ASCII statement of what this run WILL do — <b>before</b> it spends or writes anything, so you can confirm it is doing the right thing and stop it if not.<br>The observer-kit skill generates one for your pipeline.</div>';
    });
    return;
  }

  if(!sel){content.innerHTML='<div class=empty>Pick a run on the left. ● = running now.</div>';return}

  if(view==='flow'){
    renderFlow(flowScroll);
    return;
  }

  // run-level progress events (no per-record company+name) — kept OUT of the
  // table so a 10k-row run never buries them; shown in the Run info tab instead.
  const attemptMapped=attemptEvents().map(e=>({e,h:humanize(e)}));
  const general=attemptMapped.filter(({e})=>!(e.company&&e.name)).filter(x=>!x.h.quiet).filter(x=>tech||!x.h.technical);

  if(view==='info'){
    let html=attemptBanner();
    if(selMeta){
      html+=`<div class=card><h4>${esc(selMeta.name||'run')}</h4>`;
      if(selMeta.desc)html+=`<div class=row>${esc(selMeta.desc)}</div>`;
      html+=`<div class=row><small>${esc(selMeta.when||'')}${selMeta.kind?' · '+esc(selMeta.kind):''}</small></div>`;
      if(selMeta.path)html+=`<div class=row><small style="font-family:ui-monospace,monospace;opacity:.75">${esc(selMeta.path)}</small></div>`;
      html+='</div>';
    }
    html+=`<div class=card><h4 style="color:var(--dim)">Run progress</h4>`+
      (general.length?general.map(({h})=>`<div class=row><span class=${h.cls}>${h.icon} ${h.text}${h.detail?` <small style="color:var(--dim)">— ${esc(h.detail)}</small>`:''}</span></div>`).join('')
        :'<div class=row><small>no progress events yet</small></div>')+'</div>';
    content.innerHTML=html;
    return;
  }

  const hs=attemptMapped.filter(x=>!x.h.quiet).filter(x=>tech||!x.h.technical);
  if(!hs.length){content.innerHTML='<div class=empty>No events yet — they appear here within ~2s of happening.</div>';return}
  if(view==='feed'||(view==='attention'&&!attemptEvents().some(e=>(e.event||e.action)==='record'))){
    const shown=view==='attention'
      ? hs.filter(({e})=>e.error!==undefined&&e.error!==null&&String(e.error).trim()!=='')
      : hs;
    if(!shown.length){content.innerHTML='<div class=empty>No attention items yet.</div>';return}
    content.innerHTML=attemptBanner()+shown.map(({e,h})=>`<div class=line><span class=when>${(e.ts||'').slice(11,19)}</span><span>${h.icon}</span><span class=${h.cls}>${h.text}${h.detail?`<br><small style="color:var(--dim)">${esc(h.detail)}</small>`:''}</span></div>`).join('');
    if(autoscroll)content.scrollTop=content.scrollHeight;
    return;
  }
  // GENERIC records table: any run that logs `record` events gets a table whose
  // columns are auto-derived from the fields on those events (first-seen order).
  // Works for ANY workflow — not just contact enrichment. First column frozen,
  // resize/expand/scroll/chat all apply. Falls through to the enrichment table below
  // when a run has no `record` events.
  const recEvents=recordEvents().filter(e=>(e.event||e.action)==='record');
  if(recEvents.length){
    const recVer=`${recordWindowStart()}:${all.length}`;
    if(recVer!==_recGroupsVer||!_recGroupsCache)_recGroupsCache=recordGroups(recordEvents()),_recGroupsVer=recVer;
    const {groups,gorder}=_recGroupsCache;
    const html=renderRecordTable(groups,gorder,'');
    if(html!==null){
      content.innerHTML=html;
      decorateChat();
      restoreTableScroll(tableScroll);
    }
    return;
  }
  const progEvents=progressEvents();
  if(progEvents.length&&view!=='attention'){
    content.innerHTML='<div class=empty>No data rows yet. Progress is visible in the status strip, Timeline, and Run info.</div>';
    return;
  }
  // records: one table row per (company, person); events fold into columns
  const rows={};
  const key=(co,name)=>co+'|'+(name||'—');
  for(const e of attemptEvents()){
    const a=e.action||e.event||'';
    if(a==='bc_submitted'){
      for(const c of (e.contacts||[])){
        const r=rows[key(c.company,c.name)]=rows[key(c.company,c.name)]||{company:c.company,name:c.name};
        r.tier=c.tier; r.phoneState=r.phoneState||'searching…';
      }
      continue;
    }
    if(!e.company||!e.name)continue;
    const r=rows[key(e.company,e.name)]=rows[key(e.company,e.name)]||{company:e.company,name:e.name};
    // before/after: when a value changes across iterations, remember the prior one
    if(e.tier!==undefined){if(r.tier!==undefined&&r.tier!==e.tier)r.tierPrev=r.tier;r.tier=e.tier;}
    if(a==='phone_found'){if(r.phone&&r.phone!==e.phone)r.phonePrev=r.phone;r.phone=e.phone;r.phoneState='found'}
    if(a==='phone_not_found'){r.phoneState='none'}
    if(a==='email_found'){if(r.email&&r.email!==e.email)r.emailPrev=r.email;r.email=e.email;r.emailSource=e.source;r.emailState='found'}
    if(a==='email_not_found'){r.emailState='none'}
    if(e.crm_id){if(r.hs&&r.hs!==e.crm_id)r.hsPrev=r.hs;r.hs=e.crm_id;}
  }
  const list=Object.values(rows).sort((a,b)=>(a.company||'').localeCompare(b.company||'')||(a.tier??9)-(b.tier??9));
  const wasTag=p=>p?` <small style="color:var(--warn)">· was ${esc(p)}</small>`:'';
  const pill=(state,val,extra,prev)=>{
    if(val)return `<span class="pill ok">${esc(val)}</span>${extra?` <small style="color:var(--dim)">${esc(extra)}</small>`:''}${wasTag(prev)}`;
    if(state==='none')return '<span class="pill warn">not found</span>';
    if(state)return `<span class="pill dim">${esc(state)}</span>`;
    return '<span class="pill dim">—</span>';
  };
  const tierLabel={1:'Tier 1',2:'Tier 2',3:'Tier 3',4:'Tier 4',5:'Tier 5'};
  const COLS=['Company','Person','Tier','Phone','Email','CRM id'];
  const base=Object.fromEntries(COLS.map(c=>[c,colW[c]??COLW_DEFAULT[c]??160]));
  if(!COLS.some(c=>colW[c]!=null)){            // fresh load: scale defaults to fill the pane
    const avail=(content.clientWidth||1000)-4, sum=COLS.reduce((s,c)=>s+base[c],0);
    if(sum<avail){const k=avail/sum; COLS.forEach(c=>base[c]=Math.round(base[c]*k));}
  }
  const totalW=COLS.reduce((s,c)=>s+base[c],0);
  content.innerHTML=list.length
    ?`<div class=tablewrap><table style="width:${totalW}px"><thead><tr>${COLS.map(c=>`<th data-col="${c}" style="width:${base[c]}px">${c}<span class=rz></span></th>`).join('')}</tr></thead><tbody>`+
      list.map((r,i)=>{
        const first=i===0||list[i-1].company!==r.company;
        return `<tr data-key="${esc(key(r.company,r.name))}" data-co="${esc(r.company||'')}" data-name="${esc(r.name||'')}">`+
        `<td data-col="Company">${first?`<b>${esc(r.company)}</b>`:''}</td><td data-col="Person">${esc(r.name)}</td>`+
        `<td data-col="Tier"><small>${tierLabel[r.tier]??''}${r.tierPrev?` <span style="color:var(--warn)">· was ${tierLabel[r.tierPrev]??r.tierPrev}</span>`:''}</small></td>`+
        `<td data-col="Phone">${pill(r.phoneState,r.phone,undefined,r.phonePrev)}</td><td data-col="Email">${pill(r.emailState,r.email,r.emailSource,r.emailPrev)}</td>`+
        `<td data-col="CRM id">${r.hs?`<span class="pill ok">${esc(r.hs)}</span>`+wasTag(r.hsPrev):'<span class="pill dim">—</span>'}</td></tr>`;
      }).join('')+'</tbody></table></div>'
    :'<div class=empty>No per-person results yet — see the Run info tab for progress.</div>';
  decorateChat();
}

function renderStats(){
  // Fully data-driven — NOTHING hardcoded to phones/emails/CRM. For runs that emit
  // generic `record` events, the counters are derived from the records themselves:
  // one chip per table (row count) + the ACTIVE table's boolean columns as coverage
  // counts (e.g. "62 linkedin", "5 fallback"). Per-provider credits and errors always
  // show (any run can spend or fail). Enrichment runs (phone/email events) keep their
  // familiar chips as a fallback.
  const prov=Object.create(null), metricValues=Object.create(null); let errors=0;
  const recByTable=Object.create(null);   // table -> {key -> merged row}
  let enrichRun=false; const s={phones:0,emails:0,misses:0,writes:0,assoc:0};
  const events=attemptEvents();
  const tableEvents=recordEvents().filter(e=>(e.action||e.event||'')==='record');
  for(const e of [...tableEvents,...events.filter(e=>(e.action||e.event||'')!=='record')]){
    const a=e.action||e.event||'';
    if(a==='record'){
      const t=e.table||'records';
      if(!hasOwn(recByTable,t))recByTable[t]=Object.create(null);
      const g=recByTable[t];
      const k=String(e.key ?? e.company ?? e.name ?? JSON.stringify(e));
      if(!hasOwn(g,k))g[k]=Object.create(null);
      clearResolvedError(g[k],e);
      Object.assign(g[k], e);
    }
    if(a==='phone_found'){s.phones++;enrichRun=true;}
    if(a==='email_found'){s.emails++;enrichRun=true;}
    if(a==='bc_submitted'||a==='phone_not_found'||a==='email_not_found')enrichRun=true;
    if(/not_found/.test(a))s.misses++;
    if(a==='credits'||a==='bc_credits'){
      const p=e.provider||'provider', c=prov[p]=prov[p]||{};
      const used=e.used??e.credits_consumed, left=e.left??e.credits_left;
      if(used!==undefined)c.used=used;
      if(left!==undefined)c.left=left;
    }
    if(a==='metric'&&e.metric){
      const name=String(e.metric);
      if(e.value!==undefined)metricValues[name]=e.value;
      else if(e.increment!==undefined)metricValues[name]=(metricValues[name]||0)+Number(e.increment);
    }
    if(e.endpoint&&/POST|PATCH|PUT|DELETE/.test(e.endpoint)&&!/search/i.test(e.endpoint)&&e.status_code<300)s.writes++;
    if(/associat/i.test(e.endpoint||'')&&e.status_code<300)s.assoc++;
    if(/error|fail|timeout/i.test(a)||(e.status_code>=400))errors++;
  }
  const chips=[];
  const flatRecords=[];
  const tables=Object.keys(recByTable);
  const started=[...events].find(e=>(e.event||e.action)==='run_started')||{};
  const fin=[...events].reverse().find(e=>['run_finished','run_failed','run_abandoned','run_paused'].includes(e.event||e.action));
  const wantedSummary=Array.isArray(started.summary_metrics)?started.summary_metrics:[];
  const pushMetric=(label,value,cls)=>{
    if(value!==undefined&&value!==null&&value!=='')chips.push([label,value,cls]);
  };
  if(tables.length){
    for(const t of tables){
      const rows=Object.values(recByTable[t]);
      flatRecords.push(...rows.map(r=>({table:t,row:r})));
    }
    const statusCounts={running:0,done:0,failed:0,attention:0};
    for(const {row} of flatRecords){
      const st=String(row.status||'').toLowerCase();
      if(st==='running'||st==='queued'||st==='pending')statusCounts.running++;
      else if(st==='done'||st==='success'||st==='ok'||st==='complete')statusCounts.done++;
      else if(isAttentionRecord(row))statusCounts.failed++;
      if(isAttentionRecord(row))statusCounts.attention++;
    }
    if(statusCounts.running)chips.push(['running',statusCounts.running,'warn']);
    if(statusCounts.attention)chips.push(['needs attention',statusCounts.attention,'err']);
    const summaryStart=chips.length;
    if(wantedSummary.length){
      const summaryValues=fin||metricValues;
      for(const item of wantedSummary){
        const key=typeof item==='string'?item:item.key;
        const label=typeof item==='string'?key.replace(/_/g,' '):(item.label||key.replace(/_/g,' '));
        pushMetric(label, summaryValues[key], item.cls||outcomeClass(key)||'ok');
      }
    }
    if(fin&&!wantedSummary.length){
      const defaults=['processed','qualified','saas_true','emails_enriched','sheet_rows_appended','credits_spent','errors'];
      for(const key of defaults){
        if(fin[key]!==undefined)pushMetric(key.replace(/_/g,' '),fin[key],key==='errors'?'err':'ok');
      }
    }
    if(fin&&chips.length===summaryStart){
      for(const [path,value] of numericSummaryEntries(fin).slice(0,8))
        pushMetric(path.replace(/_/g,' '),value,outcomeClass(path)||'ok');
    }
    if(!fin&&chips.length===summaryStart){
      const primary=recTab&&recByTable[recTab]?recTab:tables[0];
      if(primary)pushMetric(`${primary} rows`, Object.keys(recByTable[primary]).length);
    }
  } else if(wantedSummary.length&&(fin||Object.keys(metricValues).length)){
    const summaryValues=fin||metricValues;
    for(const item of wantedSummary){
      const key=typeof item==='string'?item:item.key;
      const label=typeof item==='string'?key.replace(/_/g,' '):(item.label||key.replace(/_/g,' '));
      pushMetric(label,summaryValues[key],item.cls||outcomeClass(key)||'ok');
    }
  } else if(progressEvents().length){
    const progress=progressEvents();
    const latest=[...progress].reverse().find(e=>e.done!==undefined&&e.total!==undefined)||progress[progress.length-1];
    const phase=latest.phase||latest.checkpoint||'progress';
    if(latest.done!==undefined&&latest.total!==undefined){
      chips.push([phase, `${latest.done}/${latest.total}`, 'info']);
      chips.push(['complete', Math.round((Number(latest.done)/Number(latest.total))*100)+'%', 'ok']);
    }else{
      chips.push([phase, fmt(latest.value??latest.done??latest.total??'live'), 'info']);
    }
  } else if(enrichRun){
    chips.push(['phones found',s.phones],['emails found',s.emails]);
    if(s.misses)chips.push(['no result',s.misses]);
    if(s.writes)chips.push(['CRM writes',s.writes]);
    if(s.assoc)chips.push(['associations',s.assoc]);
  }
  for(const [p,c] of Object.entries(prov))          // one chip per provider
    chips.push([`${p} credits${c.left!==undefined?` · ${c.left} left`:''}`, c.used??0]);
  if(errors)chips.push(['errors',errors,'err']);
  document.getElementById('stats').innerHTML=activityStrip(flatRecords, errors)+chips
    .filter(([,v])=>v!==undefined)
    .map(([k,v,cls])=>`<span class=chip><b class="${cls||'ok'}">${v}</b><small>${esc(k)}</small></span>`).join('');
}

function activityStrip(flatRecords, errors){
  if(!sel)return '';
  const events=attemptEvents();
  const last=events[events.length-1]||{};
  const started=[...events].find(e=>(e.event||e.action)==='run_started')||{};
  const finished=[...events].reverse().find(e=>['run_finished','run_failed','run_abandoned','run_paused'].includes(e.event||e.action));
  const dry=[...events].reverse().find(e=>e.dry_run!==undefined);
  const dryText=dry ? (dry.dry_run?'Dry run · no writes':'Live run · writes enabled') : 'Write mode unknown';
  const lastRecord=[...events].reverse().find(e=>(e.event||e.action)==='record')||{};
  const currentRow=flatRecords.find(({row})=>String(row.status||'').toLowerCase()==='running');
  const lastAge=relAge(last.ts);
  const stale=!finished && parseTs(last.ts) && Date.now()-parseTs(last.ts)>60000;
  let state='Waiting', cls='';
  if(finished){
    state=['run_failed','run_abandoned'].includes(finished.event||finished.action)?'Failed':(finished.event||finished.action)==='run_paused'?'Paused':'Finished';
    cls=state==='Failed'?'failed':state==='Paused'?'stale':'done';
  }else if(stale){
    state='Stale';
    cls='stale';
  }else if((selMeta&&selMeta.live)||currentRow){
    state='Running';
    cls='live';
  }
  const terminalSummary=(e)=>{
    if(!e)return '';
    if(['run_failed','run_abandoned'].includes(e.event||e.action))return `Failed · ${String(e.error||'see Run info').slice(0,90)}`;
    if((e.event||e.action)==='run_paused')return `Paused · ${String(e.reason||'safe checkpoint').slice(0,90)}`;
    const priority=['with_contacts','no_contacts','total_contacts','processed','credits_spent','errors'];
    const parts=[];
    for(const k of priority){
      if(e[k]!==undefined)parts.push(`${e[k]} ${k.replace(/_/g,' ')}`);
      if(parts.length>=3)break;
    }
    if(!parts.length){
      for(const [path,value] of numericSummaryEntries(e).slice(0,3))
        parts.push(`${value} ${path.replace(/_/g,' ')}`);
    }
    if(!parts.length){
      for(const [k,v] of Object.entries(e)){
        if(['ts','event','action','_file','attempt','status','dry_run','checkpoints','summary_metrics'].includes(k)||typeof v==='object')continue;
        parts.push(`${v} ${k.replace(/_/g,' ')}`);
        if(parts.length>=3)break;
      }
    }
    return parts.length ? `Finished · ${parts.join(' · ')}` : 'Finished · see Run info';
  };
  const current=finished
    ? terminalSummary(finished)
    : currentRow
      ? `${currentRow.table}: ${currentRow.row.step||currentRow.row.key||currentRow.row.company||'record'}`
      : lastRecord.step
        ? `${lastRecord.table||'records'}: ${lastRecord.step}`
        : humanize(last).text?.replace(/<[^>]+>/g,'') || 'No events yet';
  const recordAttention=flatRecords.filter(({row})=>isAttentionRecord(row)).length;
  const attention=flatRecords.length ? recordAttention : errors;
  // `todo` normally describes the source items (for example, companies), not
  // every derived record table (contacts, writes, etc.). Count the source table
  // so a 3,000-company run with 20 emitted contacts does not read 3020 / 3000.
  const primaryTable=started.progress_table||started.table||flatRecords[0]?.table;
  const progressRecords=primaryTable
    ? flatRecords.filter(({table})=>table===primaryTable)
    : flatRecords;
  const completedProgress=progressRecords.filter(({row})=>{
    const status=String(row.status||'').toLowerCase();
    return !['running','queued','pending'].includes(status);
  }).length;
  const measuredProgress=[...progressEvents()].reverse().find(e=>e.done!==undefined&&e.total!==undefined);
  const progress=started.todo
    ? (measuredProgress
        ? `${measuredProgress.done} / ${measuredProgress.total}`
        : `${completedProgress} / ${started.todo}`)
    : flatRecords.length
      ? `${flatRecords.length} records`
      : measuredProgress
        ? `${measuredProgress.done} / ${measuredProgress.total}`
      : events.length
        ? `${events.length} events`
        : 'No events yet';
  return `<div class="activity ${cls}">
    <div><span class=k>Status</span><span class="v ${cls==='failed'?'err':cls==='stale'?'warn':cls==='done'?'info':'ok'}">${state}</span></div>
    <div><span class=k>Now</span><span class=v title="${esc(current)}">${esc(current)}</span></div>
    <div><span class=k>Last event</span><span class=v>${esc(lastAge)}</span></div>
    <div><span class=k>Mode / progress</span><span class=v>${esc(dryText)} · ${esc(progress)}${attention?` · ${attention} attention`:''}</span></div>
  </div>`;
}

async function poll(){
  let more=false;
  try{
    currentLocks=await (await fetch('/api/locks')).json();
    renderBridge();
    const runs=await (await fetch('/api/runs')).json();
    window._runs=runs;
    const q=(document.getElementById('q').value||'').toLowerCase();
    document.getElementById('runs').innerHTML=runs.filter(r=>(r.name+r.label+(r.desc||'')).toLowerCase().includes(q)).map(r=>
      `<div class="run ${sel===r.id?'sel':''}" onclick="pick('${r.id}')"><span class=${r.live?'live':'dead'}>${r.live?'● running':'○'}</span> <b>${esc(r.name||r.label)}</b><small>${esc(r.when||'')}${r.desc?' — '+esc(r.desc):''}</small></div>`
    ).join('');
    if(sel&&!runs.some(r=>r.id===sel)){
      sel=null;offsets={};all=[];chatByAnchor={};selMeta=null;
      if(runs.length)pick(runs[0].id);
      else {location.hash='';render();}
    }
    // deep link: restore the run named in the URL hash after the first runs load
    if(!sel&&location.hash.length>1){
      const want=decodeURIComponent(location.hash.slice(1));
      if(runs.some(r=>r.id===want))pick(want,true);
      else if(runs.length)pick(runs[0].id);
    }else if(!sel&&runs.length){
      pick(runs[0].id);
    }
    if(sel){
      const res=await (await fetch('/api/events?run='+encodeURIComponent(sel)+'&offsets='+encodeURIComponent(JSON.stringify(offsets)))).json();
      offsets=res.offsets;
      more=Boolean(res.more);
      if(res.events.length){all.push(...res.events);render();}
    }
    await loadControls();
    await loadChat();
  }catch(err){/* server restarting — retry */}
  setTimeout(poll,more?0:2000);
}
function pick(id,fromHash){
  sel=id;selMeta=(window._runs||[]).find(r=>r.id===id)||null;offsets={};all=[];controls=[];flowNodeSelected=null;flowRowSelected=null;_flowVersion++;
  if(!fromHash)location.hash=encodeURIComponent(id);
  render();
}
window.addEventListener('hashchange',()=>{
  const want=decodeURIComponent(location.hash.slice(1));
  if(want&&want!==sel&&(window._runs||[]).some(r=>r.id===want))pick(want,true);
});
function toggleSide(){
  const collapsed=document.body.classList.toggle('noside');
  localStorage.setItem('noside', collapsed?'1':'');
  const btn=document.getElementById('sideToggle');
  btn.innerHTML=sidebarIcon(collapsed);
  btn.title=collapsed?'Expand sidebar':'Collapse sidebar';
  btn.setAttribute('aria-label',btn.title);
}
document.getElementById('sideToggle').innerHTML=sidebarIcon(false);
if(localStorage.getItem('noside')||window.innerWidth<=720){document.body.classList.add('noside');const b=document.getElementById('sideToggle');b.innerHTML=sidebarIcon(true);b.title='Expand sidebar';b.setAttribute('aria-label','Expand sidebar');}
setBrandMark();
renderBridge();
render();
poll();
</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        try:
            data = json.loads(raw or b'{}')
        except json.JSONDecodeError:
            data = {}
        if u.path == '/api/chat':
            text = (data.get('text') or '').strip()[:2000]
            run = (data.get('run') or '')[:200]
            anchor = (data.get('anchor') or '')[:300]
            if text and run and anchor:
                os.makedirs(os.path.dirname(CHAT_FILE), exist_ok=True)
                rec = {'ts': _timestamp(), 'run': run,
                       'anchor': anchor, 'author': 'user', 'text': text}
                with open(CHAT_FILE, 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                self._json({'ok': True})
            else:
                self._json({'ok': False, 'error': 'run, anchor, text required'})
        elif u.path == '/api/control':
            run = str(data.get('run') or '')[:200]
            kind = str(data.get('kind') or '')
            note = str(data.get('note') or '').strip()[:1000]
            notify = data.get('notify') is not False
            if run and kind in {'pause', 'stop_after_record', 'approve_full_run'}:
                os.makedirs(os.path.dirname(CONTROL_FILE), exist_ok=True)
                with _CONTROL_LOCK:
                    pending = _pending_control(run, kind)
                    if pending:
                        self._json({'ok': True, 'duplicate': True, 'control': pending})
                        return
                    rec = {'id': f'{time.time_ns():x}', 'ts': _timestamp(), 'run': run,
                           'kind': kind, 'note': note}
                    with open(CONTROL_FILE, 'a', encoding='utf-8') as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                    if notify:
                        # Control transport wakes the watcher without posing as an operator note.
                        chat = {'ts': rec['ts'], 'run': run, 'anchor': 'run', 'author': 'system',
                                'kind': 'control', 'control_id': rec['id'],
                                'text': f"Control request: {kind.replace('_', ' ')}"}
                        with open(CHAT_FILE, 'a', encoding='utf-8') as fh:
                            fh.write(json.dumps(chat, ensure_ascii=False) + '\n')
                self._json({'ok': True, 'control': rec})
            else:
                self._json({'ok': False, 'error': 'run and a supported control kind required'})
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if u.path == '/':
            body = PAGE.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == '/api/runs':
            self._json(list_runs())
        elif u.path == '/api/locks':
            self._json(locks())
        elif u.path == '/api/chat':
            q = parse_qs(u.query)
            run = (q.get('run') or [''])[0]
            msgs = []
            if os.path.isfile(CHAT_FILE):
                with open(CHAT_FILE, encoding='utf-8') as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            m = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not run or m.get('run') == run:
                            msgs.append(m)
            self._json(msgs)
        elif u.path == '/api/control':
            q = parse_qs(u.query)
            run = (q.get('run') or [''])[0]
            controls = []
            if os.path.isfile(CONTROL_FILE):
                with open(CONTROL_FILE, encoding='utf-8') as fh:
                    for line in fh:
                        try:
                            control = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not run or control.get('run') == run:
                            controls.append(control)
            self._json(controls)
        elif u.path == '/api/explain':
            # Read EXPLAIN.md fresh every time (statement of intent must be current).
            found, md = False, ''
            seen = set()
            for d in [os.environ.get('RUNGUARD_STATE_DIR')] + list(SOURCES.values()) + [BASE]:
                if not d:
                    continue
                p = os.path.abspath(os.path.join(d, 'EXPLAIN.md'))
                if p in seen or not os.path.isfile(p):
                    seen.add(p)
                    continue
                seen.add(p)
                try:
                    with open(p, encoding='utf-8') as fh:
                        md, found = fh.read(), True
                    break
                except OSError:
                    pass
            self._json({'found': found, 'markdown': md})
        elif u.path == '/api/events':
            q = parse_qs(u.query)
            run_id = (q.get('run') or [''])[0]
            try:
                offsets = json.loads((q.get('offsets') or ['{}'])[0])
            except json.JSONDecodeError:
                offsets = {}
            if not isinstance(offsets, dict):
                offsets = {}
            events, new_offsets = read_events(run_id, offsets)
            self._json({'events': events, 'offsets': new_offsets,
                        'more': has_more_events(run_id, new_offsets)})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == '__main__':
    print(f'run observer → http://localhost:{PORT}')
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
