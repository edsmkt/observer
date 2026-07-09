---
name: observer-kit
description: Guardrails and a live localhost dashboard for scripts that spend API credits, scrape in bulk, send messages, or mutate shared state such as CRM, database, and spreadsheet records. Use when the user asks to "use observer-kit", "wire in observer-kit", "run observer kit", "make this script safe", "add locks/ledger/dashboard", "add dry-run sample gating", build a workflow or pipeline, push data, pull data, sync records, backfill, import/export data, enrich leads/contacts/accounts, contact source, scrape, run a CRM push, or before writing/running any batch job where duplicate runs, hidden failures, or full-run execution without review could cost money or corrupt data.
---

# observer-kit

Use Observer Kit to make risky batch scripts guarded, observable, and reviewable.
Default to the smallest safe integration: a run lock, append-only ledger, dry-run
sample, dashboard review, and explicit confirmation before the full run.

## Non-negotiable run gate

For any workflow that spends credits, scrapes in bulk, sends messages, or writes
to a shared system:

1. Add `--dry-run` plus `--limit` or `--sample-size`.
2. Run a representative sample first, usually 5-25 records.
3. Review the dashboard and summarize writes, skips, failures, schema issues, and
   estimated spend.
4. Wait for explicit confirmation before the full dataset.
5. Make the full run intentional, e.g. require `--full-run`.

Treat silence as no approval. If the user asks to skip the gate, call out the
risk and still keep dry-run and hard limits available.

Recommended CLI shape:

```bash
python3 workflow.py --dry-run --limit 10
python3 workflow.py --limit 10
python3 workflow.py --full-run
```

## Files to use

- `runguard.py`: library to vendor next to the target script.
- `run_dashboard.py`: standalone viewer; run one instance pointed at a ledger dir.
- `watch_chat.py`: run-scoped watcher for dashboard notes.
- `observer_hook.py`: optional Claude Code hook for run-start reminders.
- `references/pattern.md`: load only for detailed event vocabulary, dashboard behavior,
  parallelism, or adaptation guidance.
- `references/build-guide.md`: load only when rebuilding the stack or debugging
  acceptance-test details.

Run `python3 test_runguard.py` after changing the safety core.

## Preferred wrapper

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
)

try:
    for item in items:
        with run.step('step_name', table='companies', key=item.id,
                      company=item.domain):
            result = do_work(item)

            if not run.dry_run:
                write_result(item, result)

            run.count('items_processed')
            run.checkpoint('last_item', item.id)

    run.success(processed=len(items))
except Exception as exc:
    run.fail(exc)
    raise
```

The wrapper gives the run a lock, run id, ledger, dry-run state, visible record
rows, counters, checkpoints, and success/fail lifecycle closure.

## Wiring steps

1. Vendor `runguard.py` into the project next to the risky script.
2. Acquire the run through `start_observed_run()` before the first spend/write.
3. Put every external write or paid call inside a visible `run.step(...)`.
4. Check `run.dry_run` before mutating any external system.
5. Log stable row identity with `table=` and `key=`.
6. Add counters and checkpoints that make resume/audit obvious.
7. Run the dashboard against the state dir:

```bash
python3 /path/to/observer-kit/run_dashboard.py <project>/.runguard
```

Use `--port 8485` for a second dashboard.

## Dashboard schema

Before wiring a new workflow, propose the dashboard rows and columns to the
operator instead of asking an open-ended question. Cover:

- entities or steps as `table=` values, such as `companies`, `contacts`, `writes`;
- sources and destinations as columns, such as `source`, `supabase`, `hubspot`;
- status/outcome fields, such as `status`, `condition`, `error`;
- stable row identity via `key=`.

Example proposal:

> I will surface one `companies` row per domain with `source`, `condition`,
> `supabase`, `hubspot`, `status`, plus a `contacts` table with `name`, `title`,
> `tier`, `email`. Confirm or edit before I wire the ledger.

## Safety rules

- A lock refusal is the guard working. Do not bypass it or start a parallel run.
- Design for resume by re-reading durable state; avoid cleanup-only recovery.
- Put a hard spend/write ceiling in code.
- Never re-buy or rewrite a record whose outcome is already logged.
- Use `throttle(provider, rate)` before calls to shared provider accounts.
- Use `EXPLAIN.md` for non-obvious or high-risk pipelines.

## Optional review loop

For sample-first review inside the dashboard, call `wait_for_feedback(run_id)`
after the sample. Reply with `post_chat(run_id, anchor, text, resolved=True)`.
For long runs, poll `read_chat(run_id)` between rounds for STOP or adjustment
requests.
