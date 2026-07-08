---
name: observer-kit
description: >-
  Guardrails and a live localhost dashboard for any script that spends API
  credits or mutates shared state (CRM, database, spreadsheets). Use BEFORE
  writing or running batch jobs, enrichment or scraping runs, or bulk record
  writes. Adds crash-safe run locks (a second accidental run refuses to start,
  so nothing double-spends or corrupts data), cross-process rate limiting, an
  append-only audit ledger, a plain-English EXPLAIN.md the operator can verify,
  and a read-only web view of what each run is doing in real time. Run a small
  sample first and let the operator steer via inline chat on the results before
  the full run.
metadata:
  author: edsmkt
  tags: [batch, enrichment, safety, locks, rate-limiting, observability, credits]
---

# observer-kit

Wire safety and visibility into any script that **spends money or changes shared
state**, and give the operator a live window into every run.

## When to reach for this

Use it the moment you are about to write or run:
- a batch / enrichment / scraping job that calls a paid API (per-lookup credits),
- a bulk write to a CRM, database, or spreadsheet,
- anything where two accidental concurrent runs would double-spend or corrupt data.

Do it **before** the code spends or writes — the guardrails only help if they
are in place first.

## What to do

The kit is three stdlib-only files in this skill directory. Copy the ones you
need into the target project (vendor them; don't import from the skill dir):

1. **`runguard.py`** — the guard. In every script that spends or mutates:
   - `acquire_lock('<scope>')` before the first spend/write. Derive the scope
     from the dataset's identity (`f'enrich-{table}'`) so the same dataset
     refuses to run twice, while different datasets run in parallel.
   - `ledger('<scope>', 'run_started', description='...')`, one event per
     meaningful outcome, then `'run_finished'`. These events feed the dashboard.
   - `throttle('<provider>', <per_second>)` before every call to a shared API,
     so parallel runs share one rate budget (rate limits are per account).
   - If a shared client library performs the writes, call `acquire_lock` INSIDE
     its mutating method (gate on HTTP verb, exempt reads) — then every future
     script inherits the guard for free.

2. **`run_dashboard.py`** — the observer (a SAMPLE; adapt it). Point its
   `SOURCES` at the project's ledger directory, run `python3 run_dashboard.py`,
   open http://localhost:8484. Remap `humanize()` and the table columns to the
   events this workflow logs. It is read-only — it never touches a run.

3. **`EXPLAIN.md`** — WRITE THIS for the project, in the state dir, **before**
   the run spends anything. Plain English + one ASCII flow diagram of what THIS
   pipeline does: where the work list comes from, which providers it calls, the
   per-record cap, what it writes, and what it will NOT do. The dashboard's
   "How it works" tab renders it live so a non-technical operator can confirm
   the run is doing the right thing and stop it if not. Regenerate it whenever
   the pipeline changes — stale intent is worse than none. Use the bundled
   `EXPLAIN.md` as the template.

## The sample-first loop (MANDATORY)

Never run a full list first. Always:
1. Run a small **sample** (e.g. 5 items) with the guards on, logging to the ledger.
2. Call `runguard.wait_for_feedback(run_id)` — it **blocks** so the operator can
   review the sample in the dashboard and leave notes on specific columns/cells.
3. Read the notes, adjust the script/workflow, and run another sample if needed.
   Code changes always mean a **new run** — a running process can't change its own
   code — so re-running is how an adjustment takes effect.
4. Only once the sample looks right, run the full list. The full run polls
   `read_chat()` between rounds for a **STOP** signal (the one thing it can act on
   live); everything else is iterated on the sample, not mid-run.

Iterations show as **before/after** in the dashboard — a changed cell renders
"· was X" — so the operator sees exactly what your adjustment changed.

## Receiving operator feedback (inline chat)

The operator leaves notes anchored to a column or cell in the dashboard. They
arrive as a file-drop inbox you **pull** (there is no push into a running agent):
- `runguard.read_chat(run_id, author='user')` — notes waiting for you.
- `runguard.wait_for_feedback(run_id)` — block until new notes arrive (use after a sample).
- `runguard.post_chat(run_id, anchor, text)` — reply; shows in that cell's thread.
- `runguard.post_chat(run_id, anchor, text, resolved=True)` — mark a note handled;
  the cell's badge flips to a green ✓.

Address every note and resolve it, so the operator watches the loop close.

## Safety rules (do not skip)

- A lock refusal is the guard working, not an error to bypass. Stop the named
  PID deliberately; never launch a parallel run to "fix" a stuck one.
- Design so there is no cleanup step: write results to the durable store as they
  land, and resume by re-reading what is still missing. A crash then costs
  nothing and re-running is always safe.
- Put a hard spend ceiling in code, defaulting to the plan's worst-case need.
- Never submit more work for one entity than its remaining need (in-flight ≤
  need ⇒ worst-case spend = the cap), and never re-buy a record whose outcome
  is already recorded.

## Deeper reference (in this directory)

- `README.md` — the full pattern, event vocabulary, parallel-datasets + throttle.
- `BUILD-GUIDE.md` — rebuild the whole stack from scratch, with acceptance tests.
- `example_worker.py` — runnable end-to-end example (parallel datasets + shared
  throttle). Run two copies to watch the lock refuse the second.
- `sample-ledger.jsonl` — demo data; the dashboard renders it with no run needed.
