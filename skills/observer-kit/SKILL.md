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

Run `observer-kit --help`. Choose the CLI helper path when it succeeds; choose
the bundled-script path in `references/pattern.md` for a skill-only install.

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

Read the target script/config when present; for new work, inspect the source and destination contracts first.

**Complete when:** you can state which branch is active, what the user expects
to supervise, and which files define the implementation.

## 2. Map The Real Workflow

Trace the script from input to destination and record:

- the immutable source identity: resolved path, sheet ID, table plus query,
  export ID, or equivalent;
- the stable key for each source entity and each derived entity;
- every slow loop, pool, page, retry, provider call, and cache fill;
- the observed response shape from a bounded read call and its credential fields;
- every destination mutation and its confirmation signal;
- the durable store that resume reads;
- the spend, write, rate, policy, and quality ceilings;
- the requested run lane: update the current view or create a separate view.

Select each verification branch whose trigger is present:

- `paid_provider`: a metered, credit, quota, or account-rate-limited call;
- `external_destination`: delivery beyond the authoritative durable result
  store to a CRM, database, spreadsheet, shared file, webhook, or API;
- `long_running`: a loop, pool, or page set whose duration supports operator
  pause or stop;
- `schema_policy_quality`: an explicit schema, policy, or quality threshold;
- `iterative_comparison`: requested enrichment updates, retries, redos, or
  comparison lanes.

Record the selected branch IDs and trigger reasons in `EXPLAIN.md`, then carry
that same list into the operator proposal and sample verification.

Create new logic and CLI or preserve existing ones while wiring these paths for optimum operator visibility.

**Complete when:** every spend and mutation has a mapped path and every selected
verification branch has a recorded trigger reason.

## 3. Propose The Operator View

Derive a compact initial dashboard shape from the mapped workflow and observed schema:

- tables and stable keys;
- source, transformation, reasoning, outcome, destination, and `error` fields,
  plus the source table used for progress;
- observed schema, projected columns, and a clickable `response_json` sample field;
- three to five scalar headline metrics covering the material outcomes;
- a stratified dry-run sample across planned, write, skip, hold, missing, and failure outcomes;
- whether later enrichment updates these rows or opens a comparison lane;
- the selected verification branch IDs and their trigger reasons.

A cold-start agent owns the initial proposal, then asks concise questions about decisions, fields, metrics, attention rules, limits, and lane; the user refines it from the sample.
Set concise attention errors; healthy and expected outcomes emit `error=''`.

**Complete when:** the user can picture the view and every unresolved operator choice has an answer.

## 4. Wire The Harness

Use `start_observed_run()` around the real job and pass the actual `source=`,
`dry_run`, `description`, `todo`, `progress_table`, and concise
`summary_metrics` whose keys advance through `run.count()` and become scalar terminal counters.

Apply the production contracts from `references/pattern.md`:

1. Acquire the source-derived lock before the first spend or mutation.
2. Record the input snapshot, script/config identity, destination, and transform
   version in the manifest.
3. Use the first bounded read to call `run.schema_sample()` with the decoded real
   response body; let `--dry-run --limit` stop the earliest query/page/batch.
4. Emit every material outcome as a stable entity or phase row from every slow
   discovery/read/transform/write loop; update the same keys and pair progress with rows.
5. Use the durable boundary order: perform work, persist the real result, emit
   the row, then checkpoint the completed key or chunk.
6. Wrap each external delivery with validation, policy checks, write intent,
   confirmed sink call, and write receipt. Update the same business row's
   destination field from `pending` to its confirmed outcome.
7. Check dashboard controls at loop boundaries and after completed writes.
8. Pace shared provider accounts with `throttle()` and enforce hard spend/write
   ceilings in code.

For a phase-batched pipeline, persist each finalized item or bounded chunk when
that phase produces authoritative output; resume selects remaining work from it.

When one bounded unit uses internal pagination, keep the accumulator scoped to
that unit and persist it immediately after its final page, before the next unit
begins. Startup replay may rebuild working maps from this durable store; that
read restores completed work and preserves the existing checkpoint.

**Complete when:** dry-run work stops at its sample boundary; stopping one line
before the final statement loses at most the active item or bounded chunk, and
resume preserves confirmed spend and writes while selecting remaining work.

## 5. Prove The Sample

With the CLI helper, start the dashboard before the sample so rows arrive live:

```bash
observer-kit init .
observer-kit dashboard .runguard
observer-kit run --state-dir .runguard -- python3 workflow.py --dry-run --limit 10
```

Run the static emission/durability check from the skill directory:

```bash
python3 references/lint_emit.py /absolute/path/to/workflow.py
```

Exercise the real sample and verify this universal minimum:

- each slow phase emits a record before its terminal event while rows and the durable store advance;
- the bounded schema sample opens as full JSON and its projected columns match user review;
- scalar headline counts reconcile with stratified write, skip, hold, missing, and failure rows;
- the sample limit bounds the earliest query, page, batch, or provider loop;
- stable keys update existing rows and retain earlier fields;
- a forced mid-sample failure resumes in the same lane from saved work;
- a simultaneous start on the same source receives the duplicate-run warning;
- the dashboard remains usable while records arrive, including scroll position,
  filters, timeline, counters, and Attention rows.

Use the branch list recorded in Step 2 and `EXPLAIN.md`. Verify every selected
branch:

- **Paid provider or metered API (`paid_provider`):** hard spend and rate
  ceilings hold, shared throttles pace calls, and resume reuses persisted
  provider units;
- **External destination mutation (`external_destination`):** for delivery
  beyond the authoritative durable result store, intents and receipts reconcile
  with the real CRM, database, spreadsheet, shared file, webhook, or API state;
- **Long-running supervised job (`long_running`):** pause or stop reaches a
  script checkpoint, records acknowledgement, and opens a channel for operator
  context;
- **Schema, policy, or quality contract (`schema_policy_quality`):** measured
  gates produce the expected pass, pause, or refusal evidence before delivery.
- **Iterative enrichment or comparison (`iterative_comparison`):** current-lane
  keys update existing rows, while a comparison lane opens a separate dashboard
  view.

Summarize the universal evidence and each active branch, including records,
skips, errors, planned writes, schema findings, spend, ceilings, and the
observed restart boundary.

**Complete when:** the linter exits zero, every universal check and active
branch has direct evidence, and the user has reviewed the sample dashboard.

## 6. Run After Explicit Approval

Ask for explicit confirmation after presenting the sample summary. Begin the
full dataset through the intentional full-run flag after approval.

Keep one dashboard server attached to the state directory. With the CLI, let
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

With the CLI helper, run `observer-kit doctor .` after setup and
`observer-kit test` after core changes. The bundled-script path runs the
matching `test_*.py` files.
