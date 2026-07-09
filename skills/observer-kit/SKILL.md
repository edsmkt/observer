---
name: observer-kit
description: Guardrails and a live localhost dashboard for scripts that spend API credits, scrape in bulk, send messages, or mutate shared state such as CRM, database, and spreadsheet records. Use when the user asks to "use observer-kit", "wire in observer-kit", "run observer kit", "make this script safe", "add locks/ledger/dashboard", "add dry-run sample gating", build a workflow or pipeline, push data, pull data, sync records, backfill, import/export data, enrich leads/contacts/accounts, contact source, scrape, run a CRM push, or before writing/running any batch job where duplicate runs, hidden failures, or full-run execution without review could cost money or corrupt data.
---
**observer-kit**

Use Observer Kit to make risky batch scripts guarded, observable, and reviewable.
Default to the smallest safe integration: a lock, append-only ledger, dry-run
sample, dashboard review, and explicit confirmation before the full run.

## Required Guardrails

Run `python3 references/lint_emit.py <script.py>` before the full run. Exit 1
means the script has the common buffered-flush observability bug and must be
fixed before continuing. To pass, do these three things:

1. **Emit each `record` row when its item is processed.** Call
   `ledger(scope, 'record', ...)` or `run.step(...)` from inside the same loop
   that does the work, with stable `table=` and `key=` values. For merged or
   threaded results, emit inside the completion block, such as an
   `as_completed(...)` loop.
2. **Give every slow loop visible ledger output.** Provider batches, thread
   pools, scraper pages, cache fills, and external write phases should emit
   progress while work happens, not only after the run finishes.
3. **Run a `--dry-run` sample first.** See Non-negotiable gate below.

Writing a row per item as it completes keeps the dashboard live and means a
crash mid-run loses at most the last partial batch instead of everything.

## Non-negotiable gate

For any workflow that spends credits, scrapes in bulk, sends messages, or writes
to a shared system:

1. Add `--dry-run` plus `--limit` or `--sample-size`.
2. Run a representative sample first, usually 5-25 records.
3. Review the dashboard and summarize writes, skips, failures, schema issues,
   and estimated spend.
4. Wait for explicit confirmation before the full dataset.
5. Make the full run intentional, e.g. require `--full-run`.

Treat silence as no approval.

## Preferred path

If the CLI is available:

```bash
observer-kit init .
observer-kit dashboard .runguard
observer-kit watch .runguard --all --follow
observer-kit run --state-dir .runguard -- python3 workflow.py --dry-run --limit 10
```

Use one long-lived dashboard per state directory. The watcher is only an I/O
bridge: it emits dashboard notes to the active harness; the harness remains the
brain that inspects data, edits scripts, reruns, and replies.

Without the CLI, vendor `runguard.py`, run `run_dashboard.py <project>/.runguard`,
and use `watch_chat.py` when dashboard notes need to wake a harness.

## Wrapper pattern

For new Python scripts, use `start_observed_run()` unless the workflow needs
custom low-level events.

```python
from runguard import start_observed_run

run = start_observed_run(
    'workflow-name',
    source=args.input,  # actual CSV path, sheet ID, table ID, or API export ID
    dry_run=args.dry_run,
    description='What this run does',
    todo=len(items),
    progress_table='companies',  # table counted against todo when there are multiple tables
    summary_metrics=[
        {'key': 'processed', 'label': 'processed'},
        {'key': 'qualified', 'label': 'qualified'},
    ],
)

try:
    for item in items:
        with run.step('step_name', table='companies', key=item.id,
                      company=item.domain, condition='running'):
            result = do_work(item)
            if not run.dry_run:
                write_result(item, result)
            run.count('processed')
            run.checkpoint('last_item', item.id)

    run.success(processed=len(items))
except Exception as exc:
    run.fail(exc)
    raise
```

Stable `table=` and `key=` values are what let reruns update rows in place and
show before/after values.

`source=` is the preferred lock boundary for new workflows. Pass the actual
source identity, never a mutable display label such as `csv-july`, a date, or a
run nickname. Observer Kit derives a stable scope from it, so the same source
refuses a second start while a genuinely separate source can run in parallel.

## Live observability contract

The dashboard shows live progress when the script writes ledger events while
work is happening. Write each `record` row from the same loop that does the
work, as the item completes.

For every slow loop, provider batch, thread pool, scraper page, cache fill, or
external write phase:

- emit a visible `run.step(...)` row when an item starts and finishes;
- call `run.count(...)` and `run.checkpoint(...)` inside the loop, not only at
  the end;
- keep output/logs unbuffered for long runs, e.g. `python3 -u` or `flush=True`;
- if using low-level `ledger(...)`, emit progress events with stable `table=`
  and `key=` values from the same loop that spends, scrapes, or mutates.

Write a row per item as it completes, and the dashboard stays live and the run
survives a crash. If a dashboard looks stale while logs/cache files change, add
incremental ledger emits to the script before continuing the full run.

### Emit records as work lands (the #1 observer-kit requirement)

Write each `record` row the moment its item is done — inside the same loop that
does the work. The dashboard then shows contacts as they are sourced and a
crash mid-run loses at most the last partial batch instead of everything.

**Pattern — emit inside the work loop:**

```python
for item in todo:
    with run.step('contact', table='contacts', key=item.id):
        result = do_work(item)
        # ledger row written the moment this item is done
        run.count('contacts_found' if result else 'contacts_missed')
```

**Pattern — emit from a thread/process pool completion block** (when results
are merged from several provider phases before a row makes sense):

```python
for f in as_completed(futures):          # thread/process pool
    vat, people = f.result()
    results_by_vat[vat].extend(people)
    if n_done % 100 == 0:                # flush every batch, not only at the end
        _emit_live_contacts(todo, results_by_vat, fallback_vats)
```

**Anti-pattern — buffer everything, flush at the end.** This defeats the
dashboard and loses all results on a mid-run crash. The linter flags it:

```python
results = {}                       # buffered in memory
for item in todo:
    results[item.id] = do_work(item)   # all work happens here
# ... thousands of items later ...
for item in todo:                  # flush only at the very end
    ledger(scope, 'record', table='contacts', key=item.id, **results[item.id])
```

**Verify before the full run:** `python3 references/lint_emit.py <script.py>`
exits 0 when the common buffered-flush pattern is not detected, and exits 1
when record emits appear to happen only in a final flush block. This is a
heuristic guardrail, not a formal proof; still inspect the dashboard shape and
run a small dry-run sample.

## Dashboard proposal

Before wiring a new workflow, propose the dashboard shape instead of asking an
open-ended question. Include:

- `table=` groups, such as `companies`, `contacts`, `writes`;
- stable `key=` values;
- the source `progress_table` when `todo` measures one table but the run also
  emits derived tables such as contacts or writes;
- source/destination columns, such as `source`, `hubspot`, `google_sheet`;
- outcome columns, such as `condition`, `status`, `error`;
- 3-5 headline `summary_metrics`; pick the few counters that matter most.

Example:

> I will show one `companies` row per domain with `source`, `condition`,
> `email`, `hubspot`, and `google_sheet`. The top strip will show `processed`,
> `qualified`, `emails_enriched`, and `sheet_rows_appended`. Confirm or edit
> before I wire the ledger.

## Run-lane decision

Choose the run lane deliberately:

- Same source retry, fix, or dashboard-chat adaptation: keep the same lane
  (`--session <source-id>` or no session), same `table=`, and same `key=`.
  Rerun after patching so changed cells update in place.
- Clean redo, comparison, or new batch: use a new stable `--session <name>` or
  `--session auto` so the dashboard gets a separate run.

If ambiguous, ask: "Should I update the current run in place, or start a
separate run so you can compare old and new results?"

## Safety rules

- If a run is already active for the same source, wait for it to finish or
  deliberately stop the named PID before starting fresh. A duplicate run can
  create duplicate provider charges, CRM or sheet writes, and corrupted history.
- Default to one lock scope per external system or dataset identity.
- Parallel scopes are safe only when datasets are provably disjoint. If overlap
  is possible, use the same lock scope and run serially.
- Use `throttle(provider, rate)` before calls to shared provider accounts.
- Design resume by re-reading durable state so a re-run recomputes what is still missing.
- Put a hard spend/write ceiling in code.
- Re-read the logged outcome before writing a record again, so each entity is paid for only once.
- Use `EXPLAIN.md` for non-obvious or high-risk pipelines.

## Files to use

- `runguard.py`: library to vendor next to the target script.
- `run_dashboard.py`: standalone viewer; run one instance pointed at a ledger dir.
- `watch_chat.py`: run-scoped watcher for dashboard notes.
- `hooks/session-start-observer.sh`: hook script for agent session auto-wiring (see Agent wiring section).
- `observer_hook.py`: optional Claude Code hook for run-start reminders.
- `references/pattern.md`: load only for detailed event vocabulary, dashboard behavior,
  watcher/session semantics, parallelism, or adaptation guidance.
- `references/build-guide.md`: load only when rebuilding the stack or debugging
  acceptance-test details.
- `references/lint_emit.py`: **run on every agent-written batch script before the
  full run.** Flags the common case where `record` ledger events are buffered
  and flushed only at the end instead of emitted as work lands.

```bash
python3 references/lint_emit.py path/to/workflow.py   # exit 0 = OK, 1 = buffered-flush violation
```

Run `observer-kit test` after changing the safety core, linter, or dashboard
reader.

## Agent wiring: wake on dashboard feedback

The dashboard operator can leave notes for the agent. Without wiring, the agent
sits idle waiting. Two pieces make it automatic:

### 1. SessionStart hook (one-time setup)

The hook tells the agent on every session boot that a dashboard watcher is
active, so it knows to poll for notes without being told.

Place `hooks/session-start-observer.sh` at `.claude/hooks/session-start-observer.sh` and wire
it in `.claude/settings.local.json` or `.commandcode/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "./.claude/hooks/session-start-observer.sh"
          }
        ]
      }
    ]
  }
}
```

On boot the agent sees: "Active Observer Kit dashboard watchers: run
runguard:reactivation-de-… | To check for feedback: monitor_events({ taskId:
'<id>' })"

### 2. monitor_command instead of shell_command background

When starting a run scoped watcher, use `monitor_command` with
`notify: "scheduled"` so the runtime wakes the agent when a dashboard note
arrives — rather than `shell_command --background` which stays invisible until
checked manually.

```python
# In agent code — start the watcher:
monitor_command({
  command: "python3 watch_chat.py <run_id> --follow",
  notify: "scheduled",
  checkAfterMs: 45000
})

# Each turn after a monitor ping:
monitor_events({ taskId })      # read new notes
# Extract user feedback from notes
# Post a reply:
observer-kit reply .runguard --run <run_id> --anchor <anchor> --text "<reply>"
# Restart the monitor to keep listening:
monitor_command({ command: "python3 watch_chat.py <run_id> --follow", notify: "scheduled" })
```

Without both pieces, the operator has to manually say "check the dashboard" on
every turn — with them, feedback arrives automatically.
