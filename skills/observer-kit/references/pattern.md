# Observer Kit Production Pattern

This file is the detailed implementation reference for agent-run data movement.
Use `SKILL.md` for the ordered procedure and completion gates. Use this file for
the runtime contracts, APIs, and examples that implement those steps.

## Contents

- System boundaries
- Helper availability and launch paths
- Source identity and run lanes
- Minimal wrapper
- Operator view and record identity
- Durable boundaries and resume
- External delivery
- Live loops, batches, and pools
- Controls, chat, and watchers
- Failure and recovery
- Parallel sources, throttling, and ceilings
- Dashboard event contract
- Production verification
- Runtime files and APIs

## System Boundaries

Observer Kit is a local workflow harness with five pieces:

1. **The agent skill** carries the operational judgment: map the real workflow,
   design the operator view, run a sample, inspect evidence, and request approval.
2. **The CLI** repeats setup and transport: `init`, `dashboard`, `run`, `watch`,
   `reply`, `doctor`, and `test`.
3. **`runguard.py`** gives scripts source locks, append-only events, counters,
   checkpoints, validation, policy gates, write intents and receipts, controls,
   dead letters, simulation, and shared throttling.
4. **`run_dashboard.py`** tails a state directory and renders every JSONL lane.
   It also records operator chat and control requests.
5. **`watch_chat.py`** carries dashboard messages into the active agent harness.

The agent session remains the brain. The watcher supplies input, and the script
acknowledges controls at its own durable boundaries.

## Helper Availability And Launch Paths

The skill installation and Python CLI are separate capabilities. Probe both CLI
forms and use the first command prefix that succeeds:

```bash
observer-kit --help
python3 -m observer_kit --help
```

The CLI helper path uses that prefix for `init`, `dashboard`, `run`, `watch`,
`reply`, `doctor`, and `test`.

The bundled-script path works directly from the directory containing
`SKILL.md`. Copy `runguard.py` and `watch_chat.py` beside the workflow, create
`.runguard`, and copy `EXPLAIN.md` into it. Start the dashboard as a long-lived
process, then launch the sample through the active harness session:

```bash
python3 /absolute/skill/path/run_dashboard.py .runguard --port 8484
python3 workflow.py --dry-run --limit 10
```

As soon as the worker prints `OBSERVER_RUN_STARTED <run-id>`, launch the
run-scoped watcher in an independent monitor:

```bash
python3 watch_chat.py <run-id> --state-dir .runguard --follow
```

Keep the same dashboard and watcher alive for the approved full run. Both paths
produce the same source locks, JSONL ledgers, controls, chat, and dashboard.

## Source Identity And Run Lanes

Pass the real input identity as `source=`. Good identities remain stable across
retries and distinguish genuinely separate datasets:

- a resolved CSV, JSONL, workbook, or export path;
- a Google Sheet ID plus worksheet identity;
- a database table plus stable query or snapshot ID;
- an API export ID, object collection, or remote dataset version.

`start_observed_run(name, source=...)` derives the lock scope from the workflow
name and source identity. Two processes using the same identity contend on one
lock. Disjoint source identities can run concurrently.

Capture reviewed input state with `input_snapshot()`:

```python
from runguard import input_snapshot, start_observed_run

snapshot = input_snapshot(args.input)
run = start_observed_run(
    'transform-records',
    source=args.input,
    input_snapshot=snapshot,
    destination='warehouse',
    transform_version='v3',
    script=__file__,
    dry_run=args.dry_run,
)
```

File sources receive a content hash automatically. Remote sources gain a useful
fingerprint when `records=` or `version=` accompanies their identity. A changed
fingerprint in the same lane emits `input_changed` for operator review.

Choose lanes by intent:

- **Retry, script fix, or additional enrichment of existing rows**: reuse the
  source, session, table, and stable keys. New record events update those rows.
- **Clean comparison, redo, or separate batch**: set a new stable
  `RUNGUARD_SESSION`, pass `--session <name>`, or use `--session auto`.

## Minimal Wrapper

Use `start_observed_run()` for new scripts and unfamiliar existing scripts after
tracing their actual CLI and work paths into the standard lifecycle:

```python
from runguard import RunPaused, start_observed_run

run = start_observed_run(
    'normalize-catalog',
    source=args.input,
    dry_run=args.dry_run,
    description='Normalize catalog records and append accepted rows',
    todo=len(records),
    progress_table='records',
    summary_metrics=[
        {'key': 'processed', 'label': 'processed'},
        {'key': 'accepted', 'label': 'accepted'},
        {'key': 'written', 'label': 'written'},
    ],
)

try:
    for record in records:
        run.check_controls()
        with run.step('normalize', table='records', key=record['id'],
                      source_value=record['value']):
            output = normalize(record)
            persist_checkpoint(record['id'], output)
            run.count('processed')
            run.checkpoint('last_record', record['id'])
        run.check_controls(after_record=True)
    run.success()
except RunPaused:
    raise
except Exception as exc:
    run.fail(exc)
    raise
```

`run.step()` writes the same row as `running`, then `done` or `failed`. A failed
step includes `error` and creates a replayable dead letter. `run.success()` and
`run.fail()` write terminal events and release the source lock. Process exit
before either closure writes `run_abandoned`.

Use the lower-level `acquire_lock()` and `ledger()` functions when the script
needs a custom lifecycle while preserving the same source, row, durability, and
approval contracts.

## Operator View And Record Identity

The dashboard is workflow-specific. Treat Observer Kit examples as illustrations
and derive tables from the user's actual entities, effects, and review needs.

For an API, database, CRM, or other remote source, begin the dry run with one
bounded read that reaches the real endpoint and returns representative entities.
Capture each selected entity with the wrapper:

```python
observed_response = run.schema_sample(
    'companies', company_id, response,
    sensitive_fields={'provider_specific_secret'},
    name=response.get('name'), outcome='schema_sample', error='',
)
```

Pass the decoded API response body to `run.schema_sample()`. Request bodies,
headers, and authorization stay in the API client. The helper emits a cumulative
`schema_observed` path/type profile and a normal record row whose `response_json`
object opens in the dashboard JSON viewer. Credential-like response fields
become `[REDACTED]`; `sensitive_fields` adds provider-specific names. Use one
entity per sample row. Governed or very large responses stay in their governed
store and use a small representative object plus `payload_ref` in the ledger.

A cold-start agent produces the complete initial projection from mapped evidence
and the user's objective. Present it beside raw samples; the user refines it
through the JSON cell, a column header, or run chat before full execution.

Ask two to five concise questions after presenting that recommendation. Choose
questions whose answers change the workflow or operator view:

- What decision should the sample help the user make before full execution?
- Which entities, response fields, reasoning, and headline metrics must stay visible?
- Which outcomes belong in Attention or should pause further work?
- What spend, write, quality, and rate limits apply, and what proves destination success?
- Should later enrichment update this lane or open a comparison lane?

Resolve source identities, stable keys, API shapes, and script mechanics through
inspection. Record the user's answers in the Dashboard view, boundaries, and
lane sections of `EXPLAIN.md`, then wire and prove that agreed proposal.

Every business row uses:

```python
ledger(scope, 'record', table='orders', key=order_id,
       source='erp', amount=amount, qualification=reasoning,
       warehouse='pending', error='')
```

- `table` groups comparable entities.
- `key` identifies one entity across retries and later enrichment.
- arbitrary fields become columns.
- the first meaningful identity field becomes the frozen inspection column.
- a non-empty `error` field places the current row in Attention.
- a later event with the same `table` and `key` updates the row in place and
  retains fields supplied by earlier events.

Emit a record for every material outcome: planned write, confirmed write,
already-correct skip, held conflict, missing source entity, and failure. Set a
concise `error` for outcomes that need operator attention and `error=''` for
healthy or expected outcomes. Build the dry-run preview as a stratified sample
so uncommon holds, missing entities, and failures remain inspectable.

`run.step(name, **fields)` reserves `name` for the step label. Use `label` or
`entity_name` for a business name during the step, then emit the operator-facing
`name` field in the completed record or receipt update.

Represent source processing and destination delivery as separate fields. For
example, `status='transformed'` describes local work and
`warehouse='appended'` describes the confirmed sink outcome. Emit the confirmed
outcome on the same business row so `pending` changes to `appended`, `updated`,
`inserted`, `skipped`, or `failed` in one column.

Choose three to five `summary_metrics` that answer the operator's main questions.
Advance each selected key with `run.count()` during work; it maps to a scalar
numeric field on the terminal event. Emit additional counters for audit value while
keeping the headline strip compact. Reconcile each material outcome counter with
the folded record rows during the sample.

## Durable Boundaries And Resume

A **durable boundary** is the point where completed work can be read after the
process exits. Suitable boundaries include:

- an appended JSONL checkpoint row;
- a committed database transaction;
- a confirmed Sheet or CRM update;
- a cache file written atomically;
- a provider result stored with its stable source key.

Use this completion order for each item or bounded chunk:

1. Perform the provider, transformation, or source work.
2. Persist its authoritative result.
3. Emit the dashboard record for the same stable key.
4. Emit the checkpoint that resume will read.
5. Begin the next spend or mutation.

Resume starts by reading the durable store, derives the remaining work, and
reuses the same lane and keys:

```python
saved = read_checkpoint_rows(checkpoint_path)
remaining = [row for row in source_rows if row['id'] not in saved]

for row in remaining:
    result = paid_provider(row)
    append_checkpoint(row['id'], result)
    ledger(scope, 'record', table='records', key=row['id'], **result)
    run.checkpoint('last_record', row['id'])
```

For a phase-batched pipeline, persist every finalized item as a phase completes.
When an API returns authoritative batches, persist each bounded response before
submitting the next batch. Progress events provide liveness; the saved result
provides durability.

When one bounded unit becomes authoritative only after internal pagination,
keep only that active unit in memory and persist it immediately after its final
page, before starting the next unit. On resume, replay persisted units into the
working maps, then select and execute only the remaining units. Replay is a read
from the authoritative durable store and retains the existing checkpoint.

Run `references/lint_emit.py` against every agent-written batch script. It checks
for final record flushes and for result containers that advance while the script
offers progress events in place of a durable sink. It also checks that repeated
progress loops have a stable record-row path. Treat its zero exit as one piece
of evidence and confirm the real sink during the sample.

## External Delivery

Wrap every CRM, database, spreadsheet, file, webhook, or API mutation in an
observed delivery boundary:

```python
CONTRACT = {
    'required': ['id', 'name'],
    'types': {'id': 'string', 'name': 'string'},
    'unique': ['id'],
}
POLICY = {
    'allowed_destinations': ['warehouse'],
    'protected_fields': ['created_at'],
}

for row in rows:
    output = transform(row)
    run.validate(output, row['id'], CONTRACT, table='records')
    allowed = run.allow_write(
        output, row['id'], POLICY, current=row, destination='warehouse')
    if allowed:
        ticket = run.write_intent(
            row['id'], 'warehouse', payload=output,
            record_table='records')
        if run.dry_run:
            continue
        if ticket:
            result = upsert_warehouse(
                output, idempotency_key=ticket['operation_key'])
            run.write_receipt(
                ticket,
                destination_id=result['id'],
                verified=True,
                record_table='records',
                outcome_field='warehouse',
                outcome='updated',
                lineage={'source': 'input_export'},
            )
    run.check_controls(after_record=True)
```

`run.write_intent()` creates a stable operation key from the record key,
destination, and transform version. Pass that key to destination idempotency
support. `run.write_receipt()` records confirmed delivery and can update the
business row's destination field. `run.reconcile()` summarizes intended,
written, verified, pending, skipped, blocked, and dead-lettered operations.

A prior intent with a confirmed receipt returns an idempotent skip. A prior
intent awaiting a receipt raises `PendingWrite`, giving the agent a clear
reconciliation task: inspect the destination, then record the matching receipt
or retry according to destination evidence.

Use `run.lineage()` or receipt `lineage=` for source URL, provider, reasoning,
model, or transformation provenance. Keep sensitive payloads in their governed
store and place hashes or `payload_ref` values in the ledger.

## Live Loops, Batches, And Pools

Emit from the place where work becomes authoritative.

For a dry-run sample, thread the sample limit into the earliest source query,
page, batch, or provider loop. Stop discovery when the representative table rows
reach that boundary, so sample time and source work stay proportionate.

During every slow discovery, read, transform, or write phase, emit stable entity
records as each entity becomes known. A phase that produces authoritative data
only at completion updates one stable phase row while it runs. Progress events
accompany these rows and provide counts or percentages.

For a sequential loop, persist and emit per item:

```python
for row in rows:
    with run.step('transform', table='records', key=row['id']):
        output = transform(row)
        append_checkpoint(row['id'], output)
        run.count('processed')
        run.checkpoint('last_record', row['id'])
```

For a thread or process pool, persist and emit as futures complete:

```python
with ThreadPoolExecutor(max_workers=workers) as pool:
    futures = {pool.submit(enrich_one, row): row for row in rows}
    for future in as_completed(futures):
        row = futures[future]
        result = future.result()
        append_checkpoint(row['id'], result)
        ledger(scope, 'record', table='records', key=row['id'], **result)
        run.count('processed')
        run.checkpoint('last_record', row['id'])
```

For provider batches, emit a stable `batches` row before submission, persist the
response after completion, then update that same row. Emit progress alongside
the row for counts or percentages. Keep stdout and stderr unbuffered with
`python3 -u`, `PYTHONUNBUFFERED=1`, or `flush=True` so timing agrees.

## Controls, Chat, And Watchers

The dashboard writes controls to `controls.jsonl` and operator messages to
`chat.jsonl`. Both channels retain the run ID and relevant anchor.

Call `run.check_controls()` before starting the next item and
`run.check_controls(after_record=True)` after a durable item boundary:

- `pause` closes the attempt at the next check and emits `run_paused`.
- `stop_after_record` waits for the next `after_record=True` check.
- `approve_full_run` is returned to the script or harness as an operator signal.

Each applied control emits `control_acknowledged`, so recovered processes retain
one-shot control state.

Use one long-lived dashboard and one all-run watcher for a project:

```bash
observer-kit dashboard .runguard
observer-kit watch .runguard --all --follow
```

`observer-kit run` also detects `OBSERVER_RUN_STARTED` and starts a run-scoped
watcher. The watcher emits `OBSERVER_CHAT_EVENT` lines to the active harness.
The harness inspects evidence, edits scripts, resumes work, and replies:

```bash
observer-kit reply .runguard \
  --run runguard:my-run.jsonl \
  --anchor run \
  --resolved \
  --text "Updated the transform and resumed from the saved checkpoint."
```

## Failure And Recovery

Close expected failures through `run.fail(exc)` and preserve the original
exception. `run.step()` creates an error-bearing row and dead letter for item
exceptions. Use `run.dead_letter()` directly for recoverable failures discovered
outside a step.

`run.replay_candidates()` returns unresolved record failures after a fix. Emit
the repaired record with the same table and key; set `error=''` with the current
healthy fields so Attention reflects the latest row.

Recovery follows evidence:

- source lock present: wait for the named process or deliberately stop it;
- process exit: rerun with the same source and lane;
- pending external intent: inspect the destination and complete reconciliation;
- input fingerprint change: review the new input and choose update-in-place or a
  separate lane;
- schema or policy event: fix the source, transform, contract, or policy and
  replay the affected keys;
- quality gate pause: review the measured batch and resume after the threshold
  or workflow rule is intentionally updated.

## Parallel Sources, Throttling, And Ceilings

Use one source-derived lock for each dataset identity. Parallel runs earn their
separate scopes when the datasets are provably disjoint. Shared destinations
gain idempotent operation keys, and shared provider accounts gain one throttle
resource name:

```python
throttle('provider-account:production', 5)
response = provider_call(payload)
```

`throttle()` coordinates local processes through an advisory file lock. Every
process using the same resource string shares the configured request rate.

Encode a hard maximum for spend, successful paid results, messages, writes, and
deletes. For providers that charge per result, submit work bounded by each
entity's remaining need and check already-saved outcomes before submission.

## Dashboard Event Contract

The dashboard renders arbitrary JSON fields. These events receive first-class
behavior:

| Event | Important fields | Purpose |
|---|---|---|
| `run_started` | `description`, `todo`, `progress_table`, `summary_metrics` | open an attempt |
| `run_manifest` | source snapshot, destination, versions, hashes | establish provenance |
| `schema_observed` | `table`, `sample_count`, path/type profile | preserve the real source shape |
| `record` | `table`, `key`, arbitrary columns, optional `error` | create or update a row |
| `metric` | `metric`, `value`, `increment` | update counters |
| `checkpoint` | `checkpoint`, `value` | show durable progress |
| `credits` | `provider`, `used`, `left` | show provider spend separately |
| `write_intent` | operation key, record key, destination | reserve delivery |
| `write_receipt` | operation key, destination ID, status | confirm delivery |
| `dead_letter` | record key, error, retry, payload reference | target recovery |
| `control_acknowledged` | control ID, kind, note | confirm worker action |
| `run_paused` | reason | close a paused attempt |
| `run_finished` | status, counters, checkpoints | close success |
| `run_failed` / `run_abandoned` | `error`, counters, checkpoints | close failure |

The JSONL ledger remains append-only. The dashboard folds `record` events by
attempt, dry/live mode, table, and key to present the latest row while retaining
the event history for timeline inspection. Fresh clients fetch large ledgers in
chunks and immediately continue until caught up.

## Production Verification

Prove the harness against real workflow behavior before the full dataset.

During workflow mapping, record the selected verification branch IDs and their
trigger reasons in `EXPLAIN.md`: `paid_provider`, `external_destination`,
`long_running`, `schema_policy_quality`, and `iterative_comparison`. Carry that
same selected set into the operator proposal and the active-branch evidence.

Universal evidence for every workflow:

1. Run CLI `doctor` or confirm the bundled files, then run the emit/durability
   linter.
2. Start the dashboard before a representative dry-run sample.
3. Open the bounded `response_json` samples, compare their observed schema and
   projection with the user-approved columns, and confirm credential redaction.
4. Compare dashboard rows and scalar counters with the raw JSONL; confirm each
   material outcome is represented and each slow phase spans its work with rows.
5. Compare each completed sample row with its durable result store.
6. Force a failure after several saved items and resume the same lane.
7. Start a second process on the same source and confirm lock refusal.
8. Present the sample dashboard and restart evidence for explicit approval.

Active-branch evidence for the workflow's real effects:

- **Paid provider or metered API (`paid_provider`):** verify spend and rate
  ceilings, shared throttling, persisted provider units, and resume reuse.
- **External destination mutation (`external_destination`):** for delivery
  beyond the authoritative durable result store, reconcile intents and receipts
  with destination state and exercise append-before-receipt recovery.
- **Long-running supervised job (`long_running`):** exercise pause or stop and
  confirm worker acknowledgement plus operator chat at a durable boundary.
- **Schema, policy, or quality contract (`schema_policy_quality`):** exercise
  measured gates and retain their pass, pause, or refusal evidence in the
  ledger.
- **Iterative enrichment or comparison (`iterative_comparison`):** verify
  current-lane keys update the same rows and a comparison session opens a
  separate dashboard view.

## Runtime Files And APIs

- `runguard.py`: runtime library vendored beside the workflow.
- `run_dashboard.py`: one localhost server for a state directory.
- `watch_chat.py`: watcher transport for dashboard messages and controls.
- `EXPLAIN.md`: operator-facing statement of intent copied into `.runguard`.
- `references/lint_emit.py`: static liveness/durability heuristic.
- `example_worker.py`: deterministic dry-run, full-run, and resume example.

Core helpers:

- identity and lifecycle: `input_snapshot`, `start_observed_run`,
  `acquire_lock`, `ledger`, `success`, `fail`;
- rows and progress: `step`, `count`, `checkpoint`, `lineage`;
- review and policy: `schema_sample`, `preview`, `validate`, `allow_write`,
  `gate`, `simulate`;
- delivery and recovery: `write_intent`, `write_receipt`, `reconcile`,
  `dead_letter`, `replay_candidates`;
- operator input: `check_controls`, `read_chat`, `post_chat`,
  `wait_for_feedback`;
- shared limits: `throttle`.
