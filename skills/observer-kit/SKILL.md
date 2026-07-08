---
name: observer-kit
description: >-
  Guardrails and a live localhost dashboard for any script that spends API
  credits or mutates shared state (CRM, database, spreadsheets). Use BEFORE
  writing or running batch jobs, enrichment or scraping runs, or bulk record
  writes — including scripts the engineer already wrote. Adds crash-safe run
  locks (a second accidental run refuses to start, so nothing double-spends or
  corrupts data), cross-process rate limiting, an append-only audit ledger, and
  a read-only web view of what each run is doing in real time. Optionally: a
  plain-English EXPLAIN.md the operator can verify, and a sample-first loop with
  inline chat to steer before the full run.
metadata:
  author: edsmkt
  tags: [batch, enrichment, safety, locks, rate-limiting, observability, credits]
---

# observer-kit

Take a batch script — one the engineer already wrote, or one you're about to
write — and make it **guarded and visible**: it can't collide with another run,
every action lands in an audit trail, and the operator watches it live in a
browser. Three stdlib-only files, zero dependencies.

## When to reach for this

The moment a script will:
- call a paid API in bulk (per-lookup credits),
- write in bulk to a CRM, database, or spreadsheet,
- or is one where two accidental concurrent runs would double-spend or corrupt data.

Wire it in **before** the code spends or writes — the guardrails only help if
they're in place first.

## Make an existing script show live + guarded (the core — 3 moves)

`runguard.py` and `run_dashboard.py` play different roles — treat them differently:
- **`runguard.py` is a library your script imports** → **vendor it** (copy it into
  the target project, next to the script). ~200 lines, stdlib-only.
- **`run_dashboard.py` is a standalone viewer** → **do NOT vendor it.** Run one
  instance, pointed at whatever project's ledger dir. One observer serves every project.

**First, agree the schema with the operator — propose, don't interrogate.** The
dashboard shows *exactly* the fields you log, so decide them together before wiring.
Read what the script does, **propose a sensible default**, and let them accept, edit,
add, or drop:

> "Here's what I'd surface — one row per **company**: `source · condition · supabase ·
> hubspot · status`, plus a **contacts** table (`name · title · tier · email`). Accept,
> or want columns added/removed?"

In the proposal, cover: **which entities/steps** (each becomes a `table` / sub-tab);
**where each row was pulled from and pushed to** (Supabase / HubSpot / Cloudflare /
CSV / webhook…), plus conditions and a status (each a column); and **any key fields**
they'll want to eyeball. Whatever they land on *is* your `record` schema — log those
field names verbatim. Re-propose whenever the pipeline changes.

Then add three things to the script that already exists:

**1. Lock it** — one line before the first spend/write:
```python
from runguard import acquire_lock, ledger
acquire_lock('my-scope')   # scope = the dataset's identity, e.g. f'enrich-{table}'
```
A second run on the same scope hard-refuses (SystemExit) while the first is
alive — nothing double-spends. Different scopes still run in parallel.

**2. Ledger it** — bracket the run, and log one `record` per unit of work, with the
fields the operator asked for:
```python
ledger('my-scope', 'run_started', description='what this run does')
for item in work:
    ...whatever the script already does...
    # field names below = exactly what they said they want to see
    ledger('my-scope', 'record', table='companies', key=item.id,
           company=item.domain, source='northdata',
           condition='met', supabase='inserted', hubspot='pushed', status='done')
ledger('my-scope', 'run_finished', processed=len(work))  # numeric fields become top-bar totals
```
The ledger is BOTH the audit trail and the dashboard's feed. `record` events are
the general path — the dashboard builds a table from whatever fields you log, no
column config. Group steps with `table=` (each becomes its own sub-tab:
companies → contacts → enriched); identify rows with `key=` (repeat a key to
update that row in place → renders `· was X`); every other field becomes a
column; booleans show ✓/—; the top counters are derived from the data.

**3. Watch it** — run the observer, pointed at that project's ledger dir (no
copying, no editing — just pass the dir):
```bash
python3 /path/to/observer-kit/run_dashboard.py <project>/.runguard   # http://localhost:8484
# or:  RUNGUARD_STATE_DIR=<project>/.runguard python3 run_dashboard.py
# add --port 8485 to observe a second project at the same time
```
Read-only — it tails the files and never touches a run. One instance can observe
any project; you don't vendor it per-project.

That's the whole core. The script now can't collide, has an audit trail, and
streams live — without changing what it actually does.

## Setup once per project (Claude Code): the run-started hook

So you don't have to *remember* to start the watcher, add a hook that reminds you
whenever a run begins. `runguard` prints an `OBSERVER_RUN_STARTED <run_id>` marker on
every `run_started`; the bundled `observer_hook.py` catches it and tells you to start
that run's watcher. **On setup, add this to the project's `.claude/settings.json`**
(merge with existing hooks — don't replace):

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Bash",
        "hooks": [ { "type": "command",
          "if": "Bash(python3 *)",
          "command": "python3 <path-to>/observer_hook.py 2>/dev/null || true" } ] } ]
  }
}
```

Keep it **specific so it never touches unrelated work** — three independent guards:
- **Scope to the project, never global.** Pick one:
  - `.claude/settings.local.json` — personal & gitignored (just you); use
    `~/.claude/skills/observer-kit/observer_hook.py` as the path.
  - `.claude/settings.json` — committed, so **everyone who works in this repo** gets it.
    For this, point at a **repo-vendored** path (e.g. `tools/observer-kit/observer_hook.py`),
    NOT `~/.claude/...` which won't exist on a teammate's machine, and keep the
    `2>/dev/null || true` so a teammate who doesn't use observer-kit sees nothing.

    A global `~/.claude/settings.json` hook would run on every Bash in EVERY project — don't.
- **`if: "Bash(python3 *)"`** — only executes on `python3 …` launches; `git`, `ls`, etc.
  never trigger it. (Adjust for other launch styles, e.g. `Bash(./run.sh *)`.)
- **Marker-gated** — even when it runs it's silent unless the output contains the specific
  `OBSERVER_RUN_STARTED runguard:<file>` marker. Silent hooks are invisible in the UI.
When a run starts, the hook nudges you to run `watch_chat.py <run_id>` — the run-scoped
watcher, so notes reach the right session. It's a **backstop**: reliable for foreground
launches; a background launch's marker may not be in the immediate tool output, so still
start the watcher yourself when you launch in the background.

## Optional add-ons (reach for the ones that fit)

- **Throttle a shared API** — `throttle('<provider>', <per_second>)` before each
  call, so several parallel runs share ONE rate budget (limits are per account).
- **State intent up front (`EXPLAIN.md`)** — write a plain-English + one-ASCII-diagram
  file in the state dir describing what the run will do and won't. The dashboard's
  "How it works" tab renders it so a non-technical operator can verify before any
  spend. Regenerate it when the pipeline changes. Template bundled.
- **Sample-first — for anything that spends credits.** Run a small sample, call
  `wait_for_feedback(run_id)` (it blocks while the operator reviews the sample in
  the dashboard and leaves notes on specific cells), adjust the script, re-sample,
  then run the full list. Iterations show before/after (`· was X`). Reply and
  resolve notes with `post_chat(run_id, anchor, text, resolved=True)` (badge → ✓).
  A running process can't change its own code, so all real iteration happens on
  the sample; the full run polls `read_chat()` between rounds only for a STOP.
- **Hear the operator while you're idle — start a RUN-SCOPED watcher (key for many
  sessions).** After launching a run, set up a watcher so the operator's dashboard
  notes reach you. Use the bundled `watch_chat.py <run_id>`: it prints new notes for
  that one run and exits, so your harness re-invokes you with them. Get the id from
  `runguard.current_run_id(scope)` (e.g. `runguard:2025-06-15-enrich.jsonl`).
  - In **Claude Code**, point the Monitor tool at `python3 watch_chat.py <run_id>`.
  - Otherwise, loop it (or poll `read_chat(run_id)` yourself).

  **Scope it to YOUR run_id.** All notes land in one shared `chat.jsonl`; with several
  sessions open, an *unscoped* watcher wakes every session on every note. A run-scoped
  watcher routes each note to only the session that launched that run — session A never
  wakes for a note left on session B's run.
- **Continuous vs separate lanes** — same scope name = one continuous run
  (before/after and chat persist across re-runs). Set `RUNGUARD_SESSION=<slug>`
  to open a separate lane (a dated weekly run, or a clean A/B).

## Safety rules (do not skip)

- A lock refusal is the guard working, not an error to bypass. Stop the named
  PID deliberately; never launch a parallel run to "fix" a stuck one.
- Design so there is no cleanup step: write results to the durable store as they
  land, and resume by re-reading what's still missing. A crash then costs
  nothing and re-running is always safe.
- Put a hard spend ceiling in code, and never re-buy a record whose outcome is
  already logged.

## Deeper reference (in this directory)

- `README.md` — the full pattern and the `record` convention.
- `BUILD-GUIDE.md` — rebuild the whole stack from scratch, with acceptance tests.
- `example_worker.py` — runnable end-to-end example; run two copies to watch the
  lock refuse the second.
- `sample-ledger.jsonl` — demo data; the dashboard renders it with no run needed.
