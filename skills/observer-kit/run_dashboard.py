#!/usr/bin/env python3
"""Local dashboard for generic Observer Kit JSONL runs.

The server tails append-only ledgers and derives tables from ``record`` events:
``table`` selects the table, ``key`` identifies a stable row, and every other
field becomes workflow-defined data. It also renders lifecycle, metric, error,
control, chat, schema, and Observer Flow events without assuming a business
domain. Dashboard chat and controls use separate side-channel files; run ledgers
remain append-only.

Two ledger layouts are supported: the normal flat runguard directory and an
optional directory of per-run folders containing ``events.jsonl``.

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
# One instance observes any ledger directory; chat and controls use side files.
BASE = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_JS_PATH = os.path.join(BASE, 'assets', 'dashboard.js')
try:
    with open(DASHBOARD_JS_PATH, encoding='utf-8') as _dashboard_js_file:
        DASHBOARD_JS = _dashboard_js_file.read()
except OSError as exc:
    raise RuntimeError(
        f'Observer dashboard asset is missing or unreadable: {DASHBOARD_JS_PATH}'
    ) from exc
_arg_dir = next((a for a in sys.argv[1:] if not a.startswith('-')), None)
_runguard_dir = _arg_dir or os.environ.get('RUNGUARD_STATE_DIR') or os.path.join(BASE, '.runguard')
SOURCES = {
    'runguard': _runguard_dir,                          # runguard ledgers + locks
    'push': os.path.join(BASE, 'runs'),                 # per-run subdirs (optional)
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
    n = first.get('todo') or (first.get('details') or {}).get('total')
    if n:
        bits.append(f'{n} items')
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
    """'2025-03-10T19-15-59Z-transform' → ('transform', 'Mar 10, 19:15');
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
    if not sep or not root or not name or os.path.basename(name) != name:
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
    for kind, d in SOURCES.items():
        if kind == 'push':
            continue
        if os.path.isdir(d):
            for f in os.listdir(d):
                if _is_run_ledger(f):
                    p = os.path.join(d, f)
                    summary = _summary_event(p)
                    if (summary.get('event') or summary.get('action')) != 'run_started':
                        continue
                    mtime = os.path.getmtime(p)
                    name, when = _nice_name(f, kind)
                    runs.append({'id': f'{kind}:{f}', 'label': f, 'name': name, 'when': when,
                                 'desc': _describe(summary), 'kind': kind,
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
    primary = _run_ledger_path(run_id)
    if not primary:
        return []
    if kind == 'push':
        d = os.path.dirname(primary)
        return [p for p in (primary, os.path.join(d, 'api-calls.jsonl'))
                if os.path.exists(p)]
    if not _is_run_ledger(name):
        return []
    return [primary] if os.path.exists(primary) else []


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
<script src="/assets/dashboard.js"></script>"""


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
        elif u.path == '/assets/dashboard.js':
            body = DASHBOARD_JS.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/javascript; charset=utf-8')
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
