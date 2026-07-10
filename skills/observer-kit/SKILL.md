---
name: observer-kit
description: Harness for visible, reviewable, resumable agent-run data movement. Use when writing, adapting, or running a pipeline that transforms, enriches, imports, exports, syncs, backfills, sends, or writes records; when the job spends API credits or mutates a CRM, database, spreadsheet, file, webhook, or API; when adding dry-run approval, source locks, durable resume, JSONL ledgers, dashboard rows, run controls, or dashboard chat; or when maintaining Observer Kit itself.
---

# Observer Kit

Use Observer Kit as a local workflow **harness**. The current Codex, Claude,
Pi, Command Code, Goose, or other agent session remains the brain. The skill
supplies judgment, the CLI supplies repeatable plumbing, the script performs the
work, and the watcher carries operator messages back to the active session.

Build every run around two separate guarantees:

- **Liveness**: JSONL events advance while work happens, so the dashboard stays
  current.
- **Durability**: completed results reach a re-readable sink at a durable
  boundary, so a restart continues from saved work.

## 1. Load The Right Context

Resolve relative paths from the directory containing this `SKILL.md`.

Read the Observer Kit README from the first available location:

- repository checkout: [`../../README.md`](../../README.md)
- standalone installation:
  [`github.com/edsmkt/observer-kit/README.md`](https://github.com/edsmkt/observer-kit/blob/main/README.md)

Use it to learn the product promise, skill/CLI split, operator journey, and
dashboard expectations.

Choose the active branch and load its reference:

- **Write or adapt a production workflow**: read
  [`references/pattern.md`](references/pattern.md) in full. It is the single
  source of truth for source identity, rows, durable boundaries, external
  writes, run lanes, controls, watchers, concurrency, and dashboard events.
- **Respond to a running workflow**: read the run-lane, controls, watcher, and
  recovery sections of `references/pattern.md`, then inspect the current JSONL,
  durable destination, process state, and script.
- **Change Observer Kit itself**: read
  [`references/build-guide.md`](references/build-guide.md), then run the full
  acceptance suite after the change.

Read the target script and its configuration before designing the harness.

**Complete when:** you can state which branch is active, what the user expects
to supervise, and which files define the implementation.

## 2. Map The Real Workflow

Trace the script from input to destination and record:

- the immutable source identity: resolved path, sheet ID, table plus query,
  export ID, or equivalent;
- the stable key for each source entity and each derived entity;
- every slow loop, pool, page, retry, provider call, and cache fill;
- every destination mutation and its confirmation signal;
- the durable store that resume reads;
- the spend, write, rate, policy, and quality ceilings;
- the requested run lane: update the current view or create a separate view.

Preserve the script's working business logic and CLI while placing the harness
on these real execution paths.

**Complete when:** every spend and mutation belongs to a mapped loop, source,
stable key, durable store, destination, and ceiling.

## 3. Propose The Operator View

Propose a compact dashboard shape before wiring records:

- tables and stable keys;
- source, transformation, reasoning, outcome, destination, and `error` fields;
- the source table used for progress;
- three to five headline metrics chosen for this workflow;
- a representative dry-run sample size, usually 5 to 25 rows;
- whether later enrichment updates these rows or opens a comparison lane.

Invite the user to confirm or edit the proposal. Set `error` to a concise message
for a row that needs human attention; a successful retry emits `error=''` with
the updated row so the Attention view reflects current row state.

**Complete when:** the user can picture the tables, columns, counters, sample,
and lane before execution begins.

## 4. Wire The Harness

Use `start_observed_run()` around the real job and pass the actual `source=`,
`dry_run`, `description`, `todo`, `progress_table`, and concise
`summary_metrics`.

Apply the production contracts from `references/pattern.md`:

1. Acquire the source-derived lock before the first spend or mutation.
2. Record the input snapshot, script/config identity, destination, and transform
   version in the manifest.
3. Require `--dry-run` plus a sample limit and make `--full-run` intentional.
4. Emit each entity with stable `table=` and `key=` values from the loop or
   completion callback where its work lands.
5. Use the durable boundary order: perform work, persist the real result, emit
   the row, then checkpoint the completed key or chunk.
6. Wrap each external delivery with validation, policy checks, write intent,
   confirmed sink call, and write receipt. Update the same business row's
   destination field from `pending` to its confirmed outcome.
7. Check dashboard controls at loop boundaries and after completed writes.
8. Pace shared provider accounts with `throttle()` and enforce hard spend/write
   ceilings in code.

For a phase-batched pipeline, persist each finalized item or bounded chunk as
soon as that phase produces authoritative output. Resume reads that same store
and selects the remaining work.

When one bounded unit uses internal pagination, keep the accumulator scoped to
that unit and persist it immediately after its final page, before the next unit
begins. Startup replay may rebuild working maps from this durable store; that
read restores completed work and preserves the existing checkpoint.

**Complete when:** stopping the process one line before its final statement
loses at most the active item or bounded chunk, and resume selects remaining
work while preserving confirmed spend and writes.

## 5. Prove The Sample

Start the long-lived dashboard before the sample so the user sees rows arrive:

```bash
observer-kit init .
observer-kit dashboard .runguard
observer-kit run --state-dir .runguard -- python3 workflow.py --dry-run --limit 10
```

Run the static emission/durability check from the skill directory:

```bash
python3 references/lint_emit.py /absolute/path/to/workflow.py
```

Exercise the real sample and verify all of these surfaces:

- JSONL events and dashboard rows advance during every slow phase;
- the durable result store advances with completed work;
- stable keys update existing rows and retain earlier fields;
- destination receipts match the real destination state;
- a forced mid-sample failure resumes in the same lane from saved work;
- a simultaneous start on the same source receives the duplicate-run warning;
- pause or stop reaches a script checkpoint, records acknowledgement, and opens
  a channel for operator context;
- the dashboard remains usable while records arrive, including scroll position,
  filters, timeline, counters, and Attention rows.

Summarize planned writes, skips, errors, schema findings, estimated spend, and
the observed restart boundary.

**Complete when:** the linter exits zero, every verification above has direct
evidence, and the user has reviewed the sample dashboard.

## 6. Run After Explicit Approval

Ask for explicit confirmation after presenting the sample summary. Begin the
full dataset through the intentional full-run flag after approval.

Keep one dashboard server attached to the state directory. Let
`observer-kit run` attach to it and start the run-scoped watcher, or keep one
all-run watcher for a long-lived project:

```bash
observer-kit watch .runguard --all --follow
observer-kit run --state-dir .runguard -- python3 workflow.py --full-run
```

Treat watcher output as transport into the current agent session. Inspect the
script, JSONL, durable sink, and destination before replying or changing the
run.

**Complete when:** the full run has an explicit operator approval, live
monitoring, a terminal ledger event, reconciled receipts, and a concise outcome
summary.

## 7. Adapt Or Recover Deliberately

Use the same source, lane, table, and key to retry a failure, fix the script, or
add enrichment to rows already shown. Append updated fields so the dashboard
retains history and presents the latest row state.

Use a new stable session name or `--session auto` for a clean redo, comparison,
or genuinely separate batch. Run parallel sources when their records are
provably disjoint; use the shared provider throttle across those runs.

When a pending write exists, reconcile the destination and append the matching
receipt before continuing. When an active source lock exists, wait for that
process or deliberately stop the named PID before starting fresh.

**Complete when:** retries reuse authoritative durable state, current-row
changes appear in place, and intentionally separate work appears in its own
dashboard view.

## Reference Map

- [`references/pattern.md`](references/pattern.md): production integration and
  operation contract; read in full for workflow design and adaptation.
- [`references/lint_emit.py`](references/lint_emit.py): static check for final
  flushes and progress paired with memory-buffered results; run before every
  full dataset.
- [`references/build-guide.md`](references/build-guide.md): Observer Kit core,
  dashboard, packaging, and acceptance-test contract.
- `runguard.py`: vendored runtime used by the workflow script.
- `run_dashboard.py`: localhost dashboard server for one state directory.
- `watch_chat.py`: dashboard-message transport for the active agent harness.
- `EXPLAIN.md`: project-specific statement of intent shown to the operator.

Run `observer-kit doctor .` after project setup. Run `observer-kit test` after
changing Observer Kit's runtime, CLI, linter, watcher, or dashboard.
