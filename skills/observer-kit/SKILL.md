---
name: observer-kit
description: Guardrails and a live localhost dashboard for scripts that spend API credits, scrape in bulk, send messages, or mutate shared state such as CRM, database, and spreadsheet records. Use when the user asks to "use observer-kit", "wire in observer-kit", "run observer kit", "make this script safe", "add locks/ledger/dashboard", "add dry-run sample gating", build a workflow or pipeline, push data, pull data, sync records, backfill, import/export data, enrich leads/contacts/accounts, contact source, scrape, run a CRM push, or before writing/running any batch job where duplicate runs, hidden failures, or full-run execution without review could cost money or corrupt data.
---

# observer-kit

Use Observer Kit to make risky batch scripts guarded, observable, and reviewable.
Default to the smallest safe integration: a lock, append-only ledger, dry-run
sample, dashboard review, and explicit confirmation before the full run.

## Non-negotiable gate

For any workflow that spends credits, scrapes in bulk, sends messages, or writes
to a shared system:

1. Add `--dry-run` plus `--limit` or `--sample-size`.
2. Run a representative sample first, usually 5-25 records.
3. Review the dashboard and summarize writes, skips, failures, schema issues, and
   estimated spend.
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
    lock_key='dataset-or-system-identity',
    dry_run=args.dry_run,
    description='What this run does',
    todo=len(items),
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

## Live observability contract

The dashboard is only live if the script writes ledger events while work is
happening. Do not batch all provider work in memory and emit rows only at the
final write pass.

For every slow loop, provider batch, thread pool, scraper page, cache fill, or
external write phase:

- emit a visible `run.step(...)` row when an item starts and finishes;
- call `run.count(...)` and `run.checkpoint(...)` inside the loop, not only at
  the end;
- keep output/logs unbuffered for long runs, e.g. `python3 -u` or `flush=True`;
- if using low-level `ledger(...)`, emit progress events with stable `table=`
  and `key=` values from the same loop that spends, scrapes, or mutates.

If a dashboard looks stale while logs/cache files change, patch the script to
emit incremental ledger events before continuing the full run.

## Dashboard proposal

Before wiring a new workflow, propose the dashboard shape instead of asking an
open-ended question. Include:

- `table=` groups, such as `companies`, `contacts`, `writes`;
- stable `key=` values;
- source/destination columns, such as `source`, `hubspot`, `google_sheet`;
- outcome columns, such as `condition`, `status`, `error`;
- 3-5 headline `summary_metrics`; do not dump every counter.

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

- A lock refusal is the guard working. Do not bypass it.
- Default to one lock scope per external system or dataset identity.
- Parallel scopes are safe only when datasets are provably disjoint. If overlap
  is possible, use the same lock scope and run serially.
- Use `throttle(provider, rate)` before calls to shared provider accounts.
- Design resume by re-reading durable state; avoid cleanup-only recovery.
- Put a hard spend/write ceiling in code.
- Never re-buy or rewrite a record whose outcome is already logged.
- Use `EXPLAIN.md` for non-obvious or high-risk pipelines.

## Files to use

- `runguard.py`: library to vendor next to the target script.
- `run_dashboard.py`: standalone viewer; run one instance pointed at a ledger dir.
- `watch_chat.py`: run-scoped watcher for dashboard notes.
- `observer_hook.py`: optional Claude Code hook for run-start reminders.
- `references/pattern.md`: load only for detailed event vocabulary, dashboard behavior,
  watcher/session semantics, parallelism, or adaptation guidance.
- `references/build-guide.md`: load only when rebuilding the stack or debugging
  acceptance-test details.

Run `python3 test_runguard.py` after changing the safety core.
