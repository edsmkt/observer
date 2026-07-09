# Run Observer Kit — locks, ledgers, and a live dashboard for batch scripts

A three-piece pattern for any project where scripts spend money (API credits) or
mutate shared state (CRM, database). Give this folder to a project agent and say
"replicate this" — everything is stdlib-only Python, no dependencies.

## The three pieces

1. **`runguard.py`** — exclusivity + audit trail
   - `acquire_lock(scope)` — a PID lockfile per scope. A second process on the
     same scope refuses to start (SystemExit) while the first is alive.
     Re-entrant within one process. Stale locks (dead PID) are taken over
     silently. Crash recovery is "just re-run" — never a manual cleanup.
   - `ledger(scope, event, **fields)` — appends one JSON line to a per-run
     ledger file. This is the local audit trail AND the dashboard's data feed.
   - `start_observed_run(name, ...)` — the boring default wrapper for new
     scripts: lock, run id, dry-run flag, generic step records, counters,
     checkpoints, and `success()` / `fail()` lifecycle closure.

2. **`run_dashboard.py`** — a localhost website (default :8484), a SAMPLE that
   tails the ledger files live. Read-only, zero-intrusion. It shows an
   at-a-glance activity strip, status chips, an Attention view for failures and
   refusals, and four core tabs:
   - **Per company** — one row per (entity, item): status pills flip from
     "searching…" to the found value in real time.
   - **Timeline** — plain-English event feed; raw API calls behind a toggle.
   - **Run info** — this run's identity + run-level progress (rounds, credits,
     start/finish), kept off the table so a huge run never buries it.
   - **How it works** — renders `EXPLAIN.md`: a plain-English + ASCII statement
     of intent the operator reads to verify the run before it spends.

   Table interactions: wide schemas scroll left/right with the **first column
   frozen**; **drag a header's right edge** to resize a column (persists per
   browser); cells stay a uniform single-line height (long text truncates with
   an ellipsis), and **double-click a cell** to read its full content in a popup.

3. **`example_worker.py`** — a minimal worker script showing the full pattern:
   lock, plan, spend ceiling, per-round processing, ledger events, release.

## Why it exists

Bulk writes go wrong when a second process starts while the first is still
running — nobody realizes — and the cleanup attempt makes it worse. The fix is
structural, not procedural:

- **A lock refusal is the guard working, not an error to bypass.** If you hit
  "REFUSING TO START", stop the named PID deliberately or wait. NEVER launch a
  parallel run to "fix" a stuck one.
- **Design scripts so there is no cleanup step.** Results are written to the
  durable store as they land; a re-run recomputes what's still missing from
  that store. Then a crash costs nothing and resume is always safe.
- **Put a hard spend ceiling in the code**, defaulting to the computed
  worst-case need of the plan — a loop bug then cannot overspend even in theory.
- **Never submit more work for one entity than its remaining need.** If the
  provider charges per result, in-flight ≤ need means worst-case spend = cap.

## The boring default contract

For new scripts, start here. This is the "small wrapper, not an operational
religion" path:

```python
from runguard import start_observed_run

run = start_observed_run(
    'enrich-leads',
    lock_key='hubspot-enrich-july-batch',
    dry_run=args.dry_run,
    description='Enrich July HubSpot leads and fill missing firmographics',
    todo=len(leads),
)

try:
    for lead in leads:
        with run.step('enrich_lead', table='companies', key=lead.id,
                      company=lead.domain):
            enriched = enrich_lead(lead)

            if not run.dry_run:
                update_crm_lead(lead.id, enriched)

            run.count('leads_enriched')
            run.checkpoint('last_lead', lead.id)

    run.success(processed=len(leads))
except Exception as exc:
    run.fail(exc)
    raise
```

That one helper enforces the minimum run shape:

- a lock is acquired before the first spend/write;
- the run has a dashboard id (`run.run_id`) and a JSONL ledger;
- `dry_run` is logged and available as `run.dry_run`;
- every `run.step(...)` writes a visible `record` row (`running` → `done` or
  `failed`);
- counters and checkpoints are carried into the final event;
- `success()` / `fail()` closes the lifecycle and releases the lock.

Use the lower-level `acquire_lock()` + `ledger()` primitives when a script needs
custom event vocabulary, but keep this shape unless there is a real reason not
to. If adding Observer Kit to a new risky script takes more than a few minutes,
the wrapper is too big.

## Event vocabulary (what the dashboard understands)

The dashboard renders any JSON events, but these names get first-class
treatment (plain-English lines + table columns + counters):

| event                | fields                                          | rendering |
|----------------------|-------------------------------------------------|-----------|
| `run_started`        | `companies`/`todo`, `worst_case_credits`        | run progress card |
| `run_finished`       | any stats                                       | run progress card |
| `bc_submitted`*      | `round`, `leads`, `contacts:[{name,company,tier}]` | marks rows "searching…" |
| `bc_credits`*        | `credits_consumed`, `credits_left`              | credit counters (single provider) |
| `credits`            | `provider`, `used`, `left`                      | one credit chip **per provider** — emit one per provider (blitz, ai-ark, moltsets…) |
| `phone_found`        | `company`, `name`, `phone`, `tier`              | green pill in Phone column |
| `phone_not_found`    | `company`, `name`                               | amber "not found" |
| `email_found`        | `company`, `name`, `email`, `source`            | green pill in Email column |
| `email_not_found`    | `company`, `name`                               | amber "not found" |

\* `bc_*` are example event names from a phone/email-enrichment use case; reuse
them for any provider, or add your own mapping in `humanize()` in `run_dashboard.py`.

Rules of thumb: always include `company` + `name` on per-record events (that's
the table's row key); anything without them lands in the "Run progress" card.
Give every run a human description: `ledger(scope, 'run_started',
description='Phone enrichment for July wholesale batch', ...)` — the dashboard
shows it in the run list and header (falls back to composing one from
companies/credits/table fields).
Generic events render fine too — `{"event": "whatever", ...fields}` becomes a
timeline line.

The dashboard also reads a second format automatically (the push-library style):
`events.jsonl` rows `{ts, level, verb, phase, action, details}` and
`api-calls.jsonl` rows `{ts, provider, endpoint, status_code, ...}` in
per-run subdirectories.

## Parallel datasets + shared-API throttling

Two runs on two DIFFERENT datasets may run side by side; the same dataset twice
must refuse. The pattern:

```python
acquire_lock(f'enrich-{table}')   # per-dataset scope: alpha ∥ beta, alpha×2 refuses
...
throttle('provider-name', 5)      # before EVERY request to a shared API
```

`throttle(resource, per_second)` is a CROSS-PROCESS rate limiter (flock-based,
POSIX): all concurrent runs calling it with the same resource string
collectively stay at `per_second`, first-come-first-served — verified: two
processes against a 5/s limit measured a combined 4.99/s with no slot
collisions. Use one resource string per provider ACCOUNT, since rate limits
are account-level, not per-script.

Two safety conditions before you parallelize:
1. The datasets must be PROVABLY disjoint (no shared records) — the
   "in-flight ≤ remaining need" credit invariant only holds within one
   process, so overlapping records across two runs can double-spend.
2. Every shared API gets `throttle()` — the per-dataset lock protects the
   data, the throttle protects the provider account.

Try it: `example_worker.py --table alpha` and `--table beta` in two terminals
(parallel, jointly throttled), then `--table alpha` in a third (refuses).

## Input/output sources — anything goes, with one rule

The "table" a worker runs over can be a CSV, a JSON file, a Supabase/Postgres
query, a Google Sheet, an API — the guard pieces never see it. Normalize
whatever you load into `entity → ordered candidates` and go. Two rules:

1. **Results must land in a durable, re-readable store** (DB row updates, a
   Sheet via API, or an append-only checkpoint file). Resume and never-re-buy
   work by re-reading that store at plan time and skipping anything that
   already has a value or an attempted-outcome marker. Never rewrite a whole
   CSV in place mid-run — a crash mid-rewrite loses state; append or patch.
2. **Derive the lock scope from the dataset's identity** (table name, sheet ID,
   file path) — e.g. `acquire_lock(f'enrich-{sheet_id}')` — so the same dataset
   refuses to run twice no matter which script or session starts it.

## How to adapt to a new project (agent checklist)

1. Copy `runguard.py` next to your scripts. Set `RUNGUARD_STATE_DIR` (env var)
   or edit `_STATE_DIR` — this is where locks and ledgers live.
2. In every script that spends or mutates:
   - `acquire_lock('<scope>')` before the first spend/write. One scope per
     resource (e.g. `crm-write`, `sourcing`, `phone-enrich`) — unrelated
     scripts must not block each other.
   - `ledger('<scope>', 'run_started', ...)` / `'run_finished'` and one event
     per meaningful outcome, following the vocabulary above.
   - If a shared client library makes the writes, acquire the lock INSIDE the
     library's mutating call (gate on HTTP method, exempt read-only POSTs like
     search endpoints) — then every future script inherits the guard for free.
3. Copy `run_dashboard.py`, edit the `SOURCES` dict at the top to point at your
   ledger/state directories, run it: `python3 run_dashboard.py` →
   http://localhost:8484.
4. If your provider charges per result: implement the spend rules from the
   "Why it exists" section (ceiling = worst-case need; in-flight ≤ remaining
   need per entity; never re-buy — skip records whose outcome column/field is
   already set from a previous run).

## Scaling path — do NOT migrate the ledgers to a database

The write path stays append-only JSONL files, deliberately. Reasons: N
concurrent processes append with zero contention (a DB would reintroduce
write-lock coordination between the very processes the locks keep apart);
a half-written line on crash is one skipped line, not a broken transaction;
events are schemaless (new fields cost nothing, no migrations across copies);
and the ledgers stay greppable, attachable, and portable ("copy this folder"
is the kit's superpower).

When you want CROSS-RUN analytics ("credits per provider this month",
"hit rate by tier across all runs"), add a QUERY layer on top instead of
changing storage — DuckDB reads the JSONL directly:

```sql
SELECT event, count(*), sum(credits_consumed)
FROM read_json_auto('.runguard/*.jsonl', filename=true)
GROUP BY event;
```

One `pip install duckdb` (or the CLI binary) and the whole ledger history is a
queryable database VIEW while the files remain the source of truth. Full SQL
storage only becomes right if runs go multi-machine or you need retention
policies over tens of thousands of runs. If you are an agent considering
"helpfully" migrating this to SQLite/Postgres: don't — read the paragraph
above first.

## Files

- `runguard.py` — the lock + ledger + throttle module (env-configurable dir)
- `run_dashboard.py` — the localhost observer, a SAMPLE (edit SOURCES + remap
  `humanize()` for your workflow)
- `EXPLAIN.md` — template for the plain-English + ASCII "statement of intent"
  the dashboard's "How it works" tab renders; the agent rewrites it per project
- `example_worker.py` — end-to-end example worker (parallel datasets + throttle)
- `sample-ledger.jsonl` — demo data; select it in the dashboard to see the table
  render without running anything
