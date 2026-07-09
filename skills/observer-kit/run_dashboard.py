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
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

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
# Inline-chat inbox: the ONLY thing this dashboard writes. Users leave notes
# anchored to columns/cells; the agent watches this file and replies by appending
# lines with "author":"agent". It never touches run ledgers or run state.
CHAT_FILE = os.path.join(SOURCES['runguard'], 'chat.jsonl')
ACTIVE_S = 120   # a file touched in the last 2 min counts as live


def _first_event(path):
    try:
        with open(path, 'rb') as f:
            line = f.readline(8192).decode('utf-8', 'replace').strip()
        return json.loads(line) if line else {}
    except Exception:
        return {}


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
                             'desc': _describe(_first_event(ev)), 'kind': 'push',
                             'path': os.path.abspath(os.path.join(push_dir, d)),
                             'mtime': mtime, 'live': now - mtime < ACTIVE_S})
    for kind in ('enrich', 'runguard'):
        d = SOURCES[kind]
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith('.jsonl') and f != 'chat.jsonl':
                    p = os.path.join(d, f)
                    mtime = os.path.getmtime(p)
                    name, when = _nice_name(f, kind)
                    runs.append({'id': f'{kind}:{f}', 'label': f, 'name': name, 'when': when,
                                 'desc': _describe(_first_event(p)), 'kind': kind,
                                 'path': os.path.abspath(p),
                                 'mtime': mtime, 'live': now - mtime < ACTIVE_S})
    runs.sort(key=lambda r: -r['mtime'])
    return runs[:40]


def locks():
    out = []
    for d in set(SOURCES.values()):
        if not d or not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith('.lock'):
                try:
                    lock = json.load(open(os.path.join(d, f)))
                    pid = int(lock.get('pid', -1))
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
        off = int(offsets.get(path, 0))
        size = os.path.getsize(path)
        if size < off:
            off = 0  # rotated/truncated
        with open(path, 'rb') as f:
            f.seek(off)
            chunk = f.read(512 * 1024)
        new_offsets[path] = off + len(chunk)
        for line in chunk.decode('utf-8', 'replace').splitlines():
            line = line.strip()
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


PAGE = """<!doctype html><meta charset="utf-8"><title>Run observer</title>
<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iNyIgZmlsbD0iIzBmMTMxNyIvPjxnIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzU3Yzk4YSIgc3Ryb2tlLXdpZHRoPSIyLjIiIHN0cm9rZS1saW5lY2FwPSJyb3VuZCI+PGNpcmNsZSBjeD0iMTAuNSIgY3k9IjE3IiByPSI0LjYiLz48Y2lyY2xlIGN4PSIyMS41IiBjeT0iMTciIHI9IjQuNiIvPjxwYXRoIGQ9Ik0xNS4xIDE2IGgxLjgiLz48cGF0aCBkPSJNNS45IDE1LjUgTDMuNSAxMi44Ii8+PHBhdGggZD0iTTI2LjEgMTUuNSBMMjguNSAxMi44Ii8+PC9nPjwvc3ZnPg==">
<style>
:root{--bg:#0f1317;--panel:#181e25;--card:#1e262f;--txt:#e6ebf0;--dim:#8b96a3;--ok:#57c98a;--warn:#e5b95a;--err:#e5756a;--info:#6fa8e0;--line:#28313c}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--txt);display:flex;height:100vh}
#side{width:320px;min-width:320px;overflow-y:auto;background:var(--panel);padding:14px;border-right:1px solid #000}
#sideHead{display:flex;justify-content:flex-end;margin-bottom:2px}
#sideToggle{background:var(--card);border:none;color:var(--dim);border-radius:7px;padding:4px 10px;cursor:pointer;font-size:13px}
#sideToggle:hover{color:var(--txt)}
body.noside #side{width:42px;min-width:42px;padding:10px 5px;overflow:hidden}
body.noside #side > :not(#sideHead){display:none}
body.noside #sideHead{justify-content:center}
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
#content{flex:1;overflow-y:auto;padding:14px 20px}
h3{margin:10px 0 8px;font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em}
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
.tablewrap{overflow:auto;max-height:calc(100vh - 150px);border-radius:10px;background:var(--card);border:1px solid var(--line)}
.subtabs{position:sticky;top:0;z-index:8;display:flex;gap:6px;flex-wrap:wrap;padding:8px;background:#151c24;border-bottom:1px solid var(--line)}
.subtab{padding:5px 12px;border-radius:7px;background:#202a35;color:var(--dim);cursor:pointer;font-size:12.5px;border:1px solid transparent}
.subtab:hover{color:var(--txt);border-color:#3a4a5e}
.subtab.sel{background:#314052;color:var(--txt);border-color:#43566c}
table{table-layout:fixed;border-collapse:separate;border-spacing:0;background:var(--card)}
th{position:sticky;top:0;z-index:2;background:#242e3a;text-align:left;padding:9px 12px;font-size:11.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.recordshell th{top:43px}
td{padding:8px 12px;border-top:1px solid var(--line);vertical-align:top;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
tr:hover td{background:#232c36}
/* freeze the first column so it stays visible when scrolling a wide table right */
th:first-child{left:0;z-index:3}
td:first-child{position:sticky;left:0;z-index:1;background:var(--card)}
tr:hover td:first-child{background:#232c36}
/* drag handle on the right edge of each header cell to resize its column */
.rz{position:absolute;top:0;right:0;width:7px;height:100%;cursor:col-resize}
.rz:hover{background:#3a4a5e}
/* double-click a cell → full content (for long descriptions) */
#cellmodal{display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.55);align-items:center;justify-content:center}
#cellmodal.show{display:flex}
#cellmodalbox{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;max-width:min(680px,90vw);max-height:80vh;overflow:auto}
#cellmodalhead{color:var(--info);font-size:12.5px;margin-bottom:8px}
#cellmodalbody{white-space:pre-wrap;word-break:break-word;font-size:14px;line-height:1.55}
.pill{display:inline-block;padding:1px 9px;border-radius:99px;font-size:12px}
.pill.ok{background:#1d3a2b;color:var(--ok)}.pill.warn{background:#3a331d;color:var(--warn)}
.pill.err{background:#3a221d;color:var(--err)}.pill.dim{background:#242e39;color:var(--dim)}
.tabs{display:flex;gap:8px}
.tab{padding:5px 14px;border-radius:7px;background:var(--card);cursor:pointer;font-size:13px}
.tab.sel{background:#314052}
label{color:var(--dim);font-size:12.5px;margin-left:auto;align-self:center;cursor:pointer}
input[type=text]{width:100%;background:#0d1114;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:6px 10px;margin-bottom:8px;font-size:13px}
.lock{padding:6px 10px;border-radius:7px;margin-bottom:4px;background:var(--card);font-size:12.5px}
.empty{color:var(--dim);padding:30px;text-align:center}
.explain{max-width:820px;line-height:1.65}
.explain h2{font-size:18px;margin:16px 0 6px}.explain h3{font-size:15px;margin:12px 0 4px}
.explain p{margin:8px 0}.explain ul{margin:6px 0 6px 18px}.explain li{margin:3px 0}
.explain code{background:#0d1114;padding:1px 5px;border-radius:4px;font-size:12.5px}
pre.diagram{background:#0d1114;border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto;font:12.5px/1.45 ui-monospace,Menlo,monospace;color:var(--txt);white-space:pre}
[data-col]{cursor:pointer}
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
  <div id=sideHead><button id=sideToggle onclick="toggleSide()" title="Hide/show the run list">◀</button></div>
  <h3>Who is writing right now</h3><div id=locks class=empty>nothing running</div>
  <h3>Runs (newest first)</h3><input type=text id=q placeholder="filter…"><div id=runs></div>
</div>
<div id=main>
  <div id=topbar>
    <div class=tabs>
      <div class="tab sel" id=tabRecords onclick="view='records';render()">Data</div>
      <div class=tab id=tabAttention onclick="view='attention';render()">Attention</div>
      <div class=tab id=tabFeed onclick="view='feed';render()">Timeline</div>
      <div class=tab id=tabInfo onclick="view='info';render()">Run info</div>
      <div class=tab id=tabExplain onclick="view='explain';render()">How it works</div>
      <label title="Also show every raw HTTP request the run made (reads, polling). Failures always show, even unchecked."><input type=checkbox id=tech onchange="render()"> show raw API calls <span id=techCount style="color:var(--dim)"></span></label>
      <span style="color:var(--dim);font-size:10.5px;align-self:center" title="observer-kit v2 — inline chat">v2</span>
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
    <button class="chatbtn primary" onclick="sendChat()">Send to agent</button>
  </div>
</div>
<div id=cellmodal onclick="if(event.target.id==='cellmodal')closeCellModal()">
  <div id=cellmodalbox>
    <div id=cellmodalhead></div>
    <div id=cellmodalbody></div>
    <div style="text-align:right;margin-top:10px"><button class=chatbtn onclick="closeCellModal()">Close</button></div>
  </div>
</div>
<script>
let sel=null, offsets={}, all=[], view='records', chatByAnchor={}, chatOpenAnchor=null, colW={}, recTab=null;
const ATTENTION_RE=/(fail|error|refus|reject|timeout|exception|invalid|denied|blocked|stuck|\b[45]\d\d\b)/i;
function setRecTab(t){recTab=t;render();}
const COLW_DEFAULT={Company:190,Person:150,Tier:80,Phone:170,Email:230,'CRM id':120};
try{colW=JSON.parse(localStorage.getItem('observer_colw')||'{}')}catch(e){}
const content=document.getElementById('content');
function contentViewportHeight(){return Math.max(260, content.clientHeight-28);}
let autoscroll=true;
content.addEventListener('scroll',()=>{autoscroll=content.scrollTop+content.clientHeight>content.scrollHeight-60});

// --- inline chat (v2): click any column header or cell to leave the agent a note ---
// Writes to a file-drop inbox the agent watches (POST /api/chat). The dashboard
// never touches run data — the ONLY thing it writes is chat messages.
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
function openChat(anchor,label,el){
  chatOpenAnchor=anchor;
  const pop=document.getElementById('chatpop'), r=el.getBoundingClientRect();
  pop.style.display='block';
  pop.style.left=Math.max(8,Math.min(r.left,window.innerWidth-336))+'px';
  pop.style.top=Math.max(8,Math.min(r.bottom+6,window.innerHeight-300))+'px';
  document.getElementById('chatpopHead').textContent='💬 '+label;
  renderThread(true);
  const ti=document.getElementById('chatinput'); ti.value=''; ti.focus();
}
function closeChat(){chatOpenAnchor=null;document.getElementById('chatpop').style.display='none';}
function renderThread(forceBottom){
  const t=document.getElementById('chatthread');
  // only snap to the newest if you were already at the bottom; otherwise keep
  // your scroll position so you can read earlier messages while polls come in.
  const atBottom=t.scrollHeight-t.scrollTop-t.clientHeight<40;
  const prev=t.scrollTop;
  const msgs=chatByAnchor[chatOpenAnchor]||[];
  t.innerHTML=msgs.length
    ?msgs.map(m=>`<div class="msg ${m.author==='agent'?'agent':'user'}"><b>${m.author==='agent'?'agent':'you'}</b> <small style="color:var(--dim)">${(m.ts||'').slice(11,16)}</small>${m.resolved?' <small style="color:var(--ok)">✓ resolved</small>':''}<div>${esc(m.text)}</div></div>`).join('')
    :'<div style="color:var(--dim);font-size:12.5px">No notes here yet. Tell the agent what to change — it watches for your messages and can reply.</div>';
  t.scrollTop=(forceBottom||atBottom)?t.scrollHeight:prev;
}
async function sendChat(){
  const ti=document.getElementById('chatinput'), text=ti.value.trim();
  if(!text||!sel||!chatOpenAnchor)return;
  ti.value='';
  try{await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run:sel,anchor:chatOpenAnchor,text})});}catch(e){}
  await loadChat();
}
async function loadChat(){
  if(!sel){chatByAnchor={};return;}
  try{
    const msgs=await (await fetch('/api/chat?run='+encodeURIComponent(sel))).json();
    const by={}; for(const m of msgs){(by[m.anchor]=by[m.anchor]||[]).push(m);} chatByAnchor=by;
  }catch(e){}
  decorateChat();
  if(chatOpenAnchor)renderThread();
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
// single click = chat · double click = expand full cell · drag header edge = resize column
let clickTimer=null;
content.addEventListener('click',ev=>{
  if(ev.target.closest('.rz'))return;
  const cell=ev.target.closest('[data-col]'); if(!cell)return;
  const a=anchorFor(cell); if(!a)return;
  clearTimeout(clickTimer);
  clickTimer=setTimeout(()=>openChat(a,labelFor(cell),cell),220);
});
content.addEventListener('dblclick',ev=>{
  const cell=ev.target.closest('td[data-col]'); if(!cell)return;
  clearTimeout(clickTimer);
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
function openCellModal(cell){
  const clone=cell.cloneNode(true); const dot=clone.querySelector('.chatdot'); if(dot)dot.remove();
  const tr=cell.closest('tr'); const who=tr?(tr.dataset.name||tr.dataset.co||''):'';
  document.getElementById('cellmodalhead').textContent=(who?who+' · ':'')+cell.dataset.col;
  document.getElementById('cellmodalbody').textContent=(clone.textContent||'').trim()||'(empty)';
  document.getElementById('cellmodal').classList.add('show');
}
function closeCellModal(){document.getElementById('cellmodal').classList.remove('show');}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeCellModal();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeChat();});
document.addEventListener('click',e=>{const pop=document.getElementById('chatpop');if(pop.style.display==='block'&&!pop.contains(e.target)&&!e.target.closest('[data-col]'))closeChat();});

function esc(s){return String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function fmt(v){return v===true?'✓':v===false?'—':(v==null?'':(typeof v==='object'?JSON.stringify(v):String(v)));}
// Generic outcome coloring — classify a value into ok/warn/err/dim by a universal
// vocabulary (source, sink, status, condition all read the same way). No per-workflow
// hardcoding; returns '' for values that aren't outcome-ish (names, ids, free text).
function outcomeClass(v){
  const s=String(v).trim().toLowerCase();
  if(!s||s==='—'||s==='-'||s==='n/a'||s==='na')return 'dim';
  if(/(fail|error|refus|reject|timeout|exception|invalid|denied|✗|❌|\b[45]\d\d\b)/.test(s))return 'err';
  if(/(skip|not met|not_met|excluded|exclude|held|blocked|pending|queued|searching|missing|unmatched)/.test(s))return 'warn';
  if(/(done|ok|success|inserted|upserted|pushed|written|created|updated|added|appended|found|matched|sent|complete|synced|✓|^yes$|^true$)/.test(s))return 'ok';
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
  if(r.error)return true;
  const status=String(r.status??r.outcome??r.result??'');
  if(ATTENTION_RE.test(status))return true;
  for(const v of Object.values(r)){
    if(typeof v==='string'&&ATTENTION_RE.test(v))return true;
  }
  return false;
}
function recordGroups(){
  const SKIP=new Set(['ts','event','action','_file','key','table']);
  const groups={}, gorder=[];
  for(const e of all.filter(e=>(e.event||e.action)==='record')){
    const t=e.table||'records';
    if(!groups[t]){groups[t]={rows:{},order:[],cols:[]};gorder.push(t);}
    const g=groups[t];
    const k=String(e.key ?? e.company ?? e.name ?? JSON.stringify(e));
    let r=g.rows[k];
    if(!r){r=g.rows[k]={__prev:{}};g.order.push(k);}
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

// Turn a raw event into {icon, text, cls, company, detail} — plain English.
function humanize(e){
  const ev=e.action||e.event||'';
  const who=e.name?`<b>${esc(e.name)}</b>`:'';
  const co=e.company?` at ${esc(e.company)}`:'';
  switch(ev){
    case 'run_started': return {icon:'▶️',cls:'info',text:`Run started — ${e.companies??e.todo??'?'} companies`+(e.worst_case_credits?`, spend ceiling ${e.worst_case_credits} credits`:'')};
    case 'run_finished': return {icon:'🏁',cls:'info',text:`Run finished — `+Object.entries(e).filter(([k])=>!['ts','event','_file'].includes(k)).map(([k,v])=>`${k.replaceAll('_',' ')}: ${typeof v==='object'?JSON.stringify(v):v}`).join(', ')};
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
  return {icon:'·',cls:'dim',text:esc(JSON.stringify(e)),technical:true};
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

function render(){
  for(const [v,id] of Object.entries({records:'tabRecords',attention:'tabAttention',feed:'tabFeed',info:'tabInfo',explain:'tabExplain'}))
    document.getElementById(id).classList.toggle('sel',view===v);
  const tech=document.getElementById('tech').checked;
  const mapped=all.map(e=>({e,h:humanize(e)}));
  const nTech=mapped.filter(x=>x.h.technical).length;
  document.getElementById('techCount').textContent=
    !sel?'(pick a run first)'
    :nTech===0?'(none in this run)'
    :tech?`(showing ${nTech})`
    :`(${nTech} hidden)`;
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

  // run-level progress events (no per-record company+name) — kept OUT of the
  // table so a 10k-row run never buries them; shown in the Run info tab instead.
  const general=mapped.filter(({e})=>!(e.company&&e.name)).filter(x=>tech||!x.h.technical);

  if(view==='info'){
    let html='';
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

  const hs=mapped.filter(x=>tech||!x.h.technical);
  if(!hs.length){content.innerHTML='<div class=empty>No events yet — they appear here within ~2s of happening.</div>';return}
  if(view==='feed'||(view==='attention'&&!all.some(e=>(e.event||e.action)==='record'))){
    const shown=view==='attention'
      ? hs.filter(({e,h})=>h.cls==='err'||ATTENTION_RE.test(JSON.stringify(e)))
      : hs;
    if(!shown.length){content.innerHTML='<div class=empty>No attention items yet.</div>';return}
    content.innerHTML=shown.map(({e,h})=>`<div class=line><span class=when>${(e.ts||'').slice(11,19)}</span><span>${h.icon}</span><span class=${h.cls}>${h.text}${h.detail?`<br><small style="color:var(--dim)">${esc(h.detail)}</small>`:''}</span></div>`).join('');
    if(autoscroll)content.scrollTop=content.scrollHeight;
    return;
  }
  // GENERIC records table: any run that logs `record` events gets a table whose
  // columns are auto-derived from the fields on those events (first-seen order).
  // Works for ANY workflow — not just contact enrichment. First column frozen,
  // resize/expand/scroll/chat all apply. Falls through to the enrichment table below
  // when a run has no `record` events.
  const recEvents=all.filter(e=>(e.event||e.action)==='record');
  if(recEvents.length){
    // group records by their `table` field: a multi-step workflow emits different
    // shapes at each step (companies → contacts → enriched…), each its own table.
    const {groups,gorder}=recordGroups();
    const gwas=p=>p!==undefined?` <small style="color:var(--warn)">· was ${esc(fmt(p))}</small>`:'';
    // one SUB-TAB per table (companies / contacts / …) instead of stacking them —
    // a workflow step's output gets its own tab, not buried under the previous step's rows.
    if(!gorder.includes(recTab))recTab=gorder[0];
    let html='';
    const hasSubtabs=gorder.length>1;
    if(hasSubtabs)
      html+=`<div class=recordshell style="height:${contentViewportHeight()}px"><div class=subtabs>`+gorder.map(t=>`<span class="subtab ${t===recTab?'sel':''}" onclick="setRecTab('${esc(t)}')">${esc(t)} <small>· ${groups[t].order.length}</small></span>`).join('')+'</div>';
    const g=groups[recTab], cols=g.cols;
    const rowKeys=view==='attention' ? g.order.filter(k=>isAttentionRecord(g.rows[k])) : g.order;
    // categorical columns (status / source / sink / condition …) render as colored
    // outcome pills; free-text/id columns stay plain. Fully data-driven.
    const cats=catColumns(g.order.map(k=>g.rows[k]), cols);
    const gcell=(c,v)=>{
      const disp=esc(fmt(v));
      if(cats.has(c)&&v!=null&&v!=='')return `<span class="pill ${outcomeClass(v)||'dim'}">${disp}</span>`;
      return disp;
    };
    // width keyed by table::col so same-named cols in different tables resize independently
    const gbase=Object.fromEntries(cols.map(c=>[c,colW[recTab+'::'+c]??COLW_DEFAULT[c]??150]));
    if(!cols.some(c=>colW[recTab+'::'+c]!=null)){
      const avail=(content.clientWidth||1000)-4, sum=cols.reduce((s,c)=>s+gbase[c],0);
      if(sum<avail){const kk=avail/sum;cols.forEach(c=>gbase[c]=Math.round(gbase[c]*kk));}
    }
    const gtot=cols.reduce((s,c)=>s+gbase[c],0);
    if(view==='attention'&&!rowKeys.length){
      content.innerHTML='<div class=empty>No records need attention right now.</div>';
      return;
    }
    html+=`<div class=tablewrap><table style="width:${gtot}px"><tr>${cols.map(c=>`<th data-col="${esc(recTab+'::'+c)}" style="width:${gbase[c]}px">${esc(c)}<span class=rz></span></th>`).join('')}</tr>`+
      rowKeys.map(k=>{const r=g.rows[k];
        return `<tr data-key="${esc(recTab+'::'+k)}" data-co="${esc(k)}" data-name="${esc(k)}">`+
          cols.map(c=>`<td data-col="${esc(recTab+'::'+c)}">${gcell(c,r[c])}${gwas(r.__prev[c])}</td>`).join('')+`</tr>`;
      }).join('')+'</table></div>'+(hasSubtabs?'</div>':'');
    content.innerHTML=html||'<div class=empty>No records yet.</div>';
    decorateChat();
    return;
  }
  // records: one table row per (company, person); events fold into columns
  const rows={};
  const key=(co,name)=>co+'|'+(name||'—');
  for(const e of all){
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
    ?`<div class=tablewrap><table style="width:${totalW}px"><tr>${COLS.map(c=>`<th data-col="${c}" style="width:${base[c]}px">${c}<span class=rz></span></th>`).join('')}</tr>`+
      list.map((r,i)=>{
        const first=i===0||list[i-1].company!==r.company;
        return `<tr data-key="${esc(key(r.company,r.name))}" data-co="${esc(r.company||'')}" data-name="${esc(r.name||'')}">`+
        `<td data-col="Company">${first?`<b>${esc(r.company)}</b>`:''}</td><td data-col="Person">${esc(r.name)}</td>`+
        `<td data-col="Tier"><small>${tierLabel[r.tier]??''}${r.tierPrev?` <span style="color:var(--warn)">· was ${tierLabel[r.tierPrev]??r.tierPrev}</span>`:''}</small></td>`+
        `<td data-col="Phone">${pill(r.phoneState,r.phone,undefined,r.phonePrev)}</td><td data-col="Email">${pill(r.emailState,r.email,r.emailSource,r.emailPrev)}</td>`+
        `<td data-col="CRM id">${r.hs?`<span class="pill ok">${esc(r.hs)}</span>`+wasTag(r.hsPrev):'<span class="pill dim">—</span>'}</td></tr>`;
      }).join('')+'</table></div>'
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
  const prov={}; let errors=0;
  const recByTable={};   // table -> {key -> merged row}
  let enrichRun=false; const s={phones:0,emails:0,misses:0,writes:0,assoc:0};
  for(const e of all){
    const a=e.action||e.event||'';
    if(a==='record'){
      const t=e.table||'records', g=recByTable[t]=recByTable[t]||{};
      const k=String(e.key ?? e.company ?? e.name ?? JSON.stringify(e));
      g[k]=Object.assign(g[k]||{}, e);
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
    if(e.endpoint&&/POST|PATCH|PUT|DELETE/.test(e.endpoint)&&!/search/i.test(e.endpoint)&&e.status_code<300)s.writes++;
    if(/associat/i.test(e.endpoint||'')&&e.status_code<300)s.assoc++;
    if(/error|fail|timeout/i.test(a)||(e.status_code>=400))errors++;
  }
  const chips=[];
  const flatRecords=[];
  const tables=Object.keys(recByTable);
  if(tables.length){
    for(const t of tables){
      const rows=Object.values(recByTable[t]);
      flatRecords.push(...rows.map(r=>({table:t,row:r})));
      chips.push([t, rows.length]);   // companies, people, …
    }
    const statusCounts={running:0,done:0,failed:0,attention:0};
    for(const {row} of flatRecords){
      const st=String(row.status||'').toLowerCase();
      if(st==='running'||st==='queued'||st==='pending')statusCounts.running++;
      else if(st==='done'||st==='success'||st==='ok'||st==='complete')statusCounts.done++;
      else if(ATTENTION_RE.test(st)||row.error)statusCounts.failed++;
      if(isAttentionRecord(row))statusCounts.attention++;
    }
    if(statusCounts.running)chips.push(['running',statusCounts.running,'warn']);
    if(statusCounts.done)chips.push(['done',statusCounts.done,'ok']);
    if(statusCounts.attention)chips.push(['needs attention',statusCounts.attention,'err']);
    // plus the engineer's own headline totals from run_finished (numeric fields only) —
    // e.g. emails_found, total_contacts. A few final numbers, not a per-value breakdown;
    // the colored cells carry the "what landed where" detail.
    const fin=[...all].reverse().find(e=>(e.event||e.action)==='run_finished');
    if(fin) for(const [k,v] of Object.entries(fin)){
      if(['ts','event','action','_file','errors'].includes(k)||typeof v!=='number'||tables.includes(k))continue;
      chips.push([k.replace(/_/g,' '), v, outcomeClass(k)||'ok']);
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
  const last=all[all.length-1]||{};
  const started=[...all].find(e=>(e.event||e.action)==='run_started')||{};
  const finished=[...all].reverse().find(e=>['run_finished','run_failed'].includes(e.event||e.action));
  const dry=[...all].reverse().find(e=>e.dry_run!==undefined);
  const dryText=dry ? (dry.dry_run?'Dry run · no writes':'Live run · writes enabled') : 'Write mode unknown';
  const lastRecord=[...all].reverse().find(e=>(e.event||e.action)==='record')||{};
  const currentRow=flatRecords.find(({row})=>String(row.status||'').toLowerCase()==='running');
  const lastAge=relAge(last.ts);
  const stale=!finished && parseTs(last.ts) && Date.now()-parseTs(last.ts)>60000;
  let state='Waiting', cls='';
  if(finished){
    state=(finished.event||finished.action)==='run_failed'?'Failed':'Finished';
    cls=state==='Failed'?'failed':'done';
  }else if(stale){
    state='Stale';
    cls='stale';
  }else if((selMeta&&selMeta.live)||currentRow){
    state='Running';
    cls='live';
  }
  const current=currentRow
    ? `${currentRow.table}: ${currentRow.row.step||currentRow.row.key||currentRow.row.company||'record'}`
    : lastRecord.step
      ? `${lastRecord.table||'records'}: ${lastRecord.step}`
      : humanize(last).text?.replace(/<[^>]+>/g,'') || 'No events yet';
  const recordAttention=flatRecords.filter(({row})=>isAttentionRecord(row)).length;
  const attention=flatRecords.length ? recordAttention : errors;
  const progress=started.todo
    ? `${flatRecords.filter(({row})=>String(row.status||'').toLowerCase()==='done').length} / ${started.todo}`
    : flatRecords.length
      ? `${flatRecords.length} records`
      : all.length
        ? `${all.length} events`
        : 'No events yet';
  return `<div class="activity ${cls}">
    <div><span class=k>Status</span><span class="v ${cls==='failed'?'err':cls==='stale'?'warn':cls==='done'?'info':'ok'}">${state}</span></div>
    <div><span class=k>Now</span><span class=v title="${esc(current)}">${esc(current)}</span></div>
    <div><span class=k>Last event</span><span class=v>${esc(lastAge)}</span></div>
    <div><span class=k>Mode / progress</span><span class=v>${esc(dryText)} · ${esc(progress)}${attention?` · ${attention} attention`:''}</span></div>
  </div>`;
}

async function poll(){
  try{
    const lk=await (await fetch('/api/locks')).json();
    document.getElementById('locks').innerHTML=lk.length?lk.map(l=>
      `<div class=lock><span class=${l.alive?'live':'dead'}>${l.alive?'●':'○'}</span> <b>${esc(l.scope)}</b> — process ${l.pid}${l.alive?', running':' (stale, safe to ignore)'}<br><small style="color:var(--dim)">since ${esc(l.started||'?')}</small></div>`).join('')
      :'<div class=empty style="padding:6px">nothing running</div>';
    const runs=await (await fetch('/api/runs')).json();
    window._runs=runs;
    const q=(document.getElementById('q').value||'').toLowerCase();
    document.getElementById('runs').innerHTML=runs.filter(r=>(r.name+r.label+(r.desc||'')).toLowerCase().includes(q)).map(r=>
      `<div class="run ${sel===r.id?'sel':''}" onclick="pick('${r.id}')"><span class=${r.live?'live':'dead'}>${r.live?'● running':'○'}</span> <b>${esc(r.name||r.label)}</b><small>${esc(r.when||'')}${r.desc?' — '+esc(r.desc):''}</small></div>`
    ).join('');
    // deep link: restore the run named in the URL hash after the first runs load
    if(!sel&&location.hash.length>1){
      const want=decodeURIComponent(location.hash.slice(1));
      if(runs.some(r=>r.id===want))pick(want,true);
    }
    if(sel){
      const res=await (await fetch('/api/events?run='+encodeURIComponent(sel)+'&offsets='+encodeURIComponent(JSON.stringify(offsets)))).json();
      offsets=res.offsets;
      if(res.events.length){all.push(...res.events);render();}
    }
    await loadChat();
  }catch(err){/* server restarting — retry */}
  setTimeout(poll,2000);
}
function pick(id,fromHash){
  sel=id;selMeta=(window._runs||[]).find(r=>r.id===id)||null;offsets={};all=[];
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
  document.getElementById('sideToggle').textContent=collapsed?'▶':'◀';
}
if(localStorage.getItem('noside')){document.body.classList.add('noside');document.getElementById('sideToggle').textContent='▶';}
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
        if u.path == '/api/chat':
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length else b''
            try:
                data = json.loads(raw or b'{}')
            except json.JSONDecodeError:
                data = {}
            text = (data.get('text') or '').strip()[:2000]
            run = (data.get('run') or '')[:200]
            anchor = (data.get('anchor') or '')[:300]
            if text and run and anchor:
                os.makedirs(os.path.dirname(CHAT_FILE), exist_ok=True)
                rec = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'run': run,
                       'anchor': anchor, 'author': 'user', 'text': text}
                with open(CHAT_FILE, 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
                self._json({'ok': True})
            else:
                self._json({'ok': False, 'error': 'run, anchor, text required'})
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
            events, new_offsets = read_events(run_id, offsets)
            self._json({'events': events[-500:], 'offsets': new_offsets})
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == '__main__':
    print(f'run observer → http://localhost:{PORT}')
    HTTPServer(('127.0.0.1', PORT), Handler).serve_forever()
