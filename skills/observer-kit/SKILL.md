---
name: observer-kit
description: Harness for visible, reviewable, resumable agent-run data movement. Use when writing, adapting, or running a pipeline that transforms, enriches, imports, exports, syncs, backfills, sends, or writes records; when the job spends API credits or mutates a CRM, database, spreadsheet, file, webhook, or API; when adding dry-run approval, source locks, durable resume, JSONL ledgers, dashboard rows, run controls, or dashboard chat; or when maintaining Observer Kit itself.
---

# Observer Kit

Use Observer Kit as a local workflow **harness**. The current Codex, Claude,
Pi, Command Code, Goose, or other agent session remains the brain. The skill
supplies judgment, the CLI supplies repeatable plumbing, the script performs the
work, and the watcher carries operator messages back to the active session.

Build every run around two guarantees:

- **Liveness**: JSONL events advance while work happens so the dashboard stays current.
- **Durability**: completed results reach a re-readable sink at a durable boundary so
  a restart continues from saved work. Ledger rows, write receipts, controls, and
  throttle claims are fsync'd after append; resume trusts that disk boundary over
  process memory.

Route every external mutation through `write_intent` / `write_receipt` (and optional
`validate` / `allow_write`) for dry-run honesty. Use `RUNGUARD_SESSION` for a separate
ledger **and** lock so comparison or redo lanes stay independent on the same source.

## 1. Load The Right Context

Resolve relative paths from the directory containing this `SKILL.md`.

Read the Observer Kit README from the repository checkout
[`../../README.md`](../../README.md) or the
[public repository](https://github.com/edsmkt/observer-kit/blob/main/README.md)
for the product promise, skill/CLI split, operator journey, and dashboard expectations.

Establish a verified CLI command prefix before project setup. Probe
`observer-kit --help`, then `python3 -m observer_kit --help`. When both probes
fail, install the CLI from the public repository into a writable Python
environment using the README command, then repeat the probes. Use the
bundled-script path in `references/pattern.md` when installation remains
unavailable or the user selects a skill-only setup.

Choose the active branch and load its reference:

- **Write or adapt a production workflow**: read
  [`references/pattern.md`](references/pattern.md) in full. It is the single
  source of truth for source identity, rows, durable boundaries, external
  writes, run lanes, controls, watchers, concurrency, and dashboard events.
- **Respond to a running workflow**: read the run-lane, controls, watcher, and
  recovery sections of `references/pattern.md`, then inspect the current JSONL,
  durable destination, process state, and script.
- **Change Observer Kit itself**: read `references/pattern.md` in full, inspect
  the affected runtime and matching tests, then run the full acceptance suite.

Read the target script/config when present; for new work, inspect the source and destination contracts first.

**Complete when:** you can state which branch is active, what the user expects
to supervise, and which files define the implementation.

## 2. Map The Real Workflow

Trace the script from input to destination and record:

- the immutable source identity: resolved path, sheet ID, table plus query,
  export ID, or equivalent;
- the stable key for each source entity and each derived entity;
- every slow loop, pool, page, retry, provider call, and cache fill;
- the declared API/schema contract and observed response shape from bounded read calls;
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
- an observed field catalog with paths/types/examples, projected columns, and clickable `response_json`;
- three to five scalar headline metrics covering the material outcomes;
- a stratified dry-run sample across planned, write, skip, hold, missing, and failure outcomes;
- whether later enrichment updates these rows or opens a comparison lane;
- the selected verification branch IDs and their trigger reasons.

A cold-start agent owns the initial proposal, then asks concise questions about decisions, fields, response retention, metrics, attention rules, limits, and lane; the user refines it from the sample.
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
4. Emit stable business rows as source items and classifications become known; reserve phase rows for work that has no business key yet, then update the same keys.
5. Use the durable boundary order: perform work, persist the real result, emit
   the row, then checkpoint the completed key or chunk.
6. Wrap each external delivery with validation, policy checks, write intent,
   confirmed sink call, and write receipt. Update the same business row's
   destination field from `pending` to its confirmed outcome.
7. Consume structured dashboard controls at loop boundaries and after completed writes; dashboard chat remains input for the active agent session.
8. Pace shared provider accounts with `throttle()` and enforce hard spend/write
   ceilings in code.

For a phase-batched pipeline, persist each finalized item or bounded chunk when
that phase produces authoritative output; resume selects remaining work from it.
When one bounded unit uses internal pagination, keep the accumulator scoped to
that unit and persist it immediately after its final page, before the next unit
begins. Startup replay may rebuild working maps from this durable store.

**Complete when:** dry-run work stops at its sample boundary; stopping one line
before the final statement loses at most the active item or bounded chunk, and
resume preserves confirmed spend and writes while selecting remaining work.

## 5. Prove The Sample

With the CLI helper, start the dashboard before the sample so rows arrive live:

```bash
observer-kit init .
observer-kit dashboard .observer
observer-kit run --state-dir .observer -- python3 workflow.py --dry-run --limit 10
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
- a canary row visibly moves through selected, writing, verifying, and verified or failed;
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

Keep one dashboard server attached to the state directory. By default,
`observer-kit run` creates or reuses one run-scoped watcher; different run IDs
stay independent. Choose one all-run watcher for a single long-lived project session:

```bash
observer-kit watch .observer --all --follow
observer-kit run --state-dir .observer -- python3 workflow.py --full-run
```

For interactive dashboard chat, use the AXI-style poll loop so the UI shows
**listening** while you wait (`poll` → note → `reply` → `poll` again). Watcher
ownership refuses overlapping bridges; parent-owned watchers exit with their CLI
process. Use `observer-kit watch .observer --status` for inspection.

Treat watcher/poll output as transport into the current agent session. Inspect
the script, JSONL, durable sink, and destination before replying or changing the run.

**Complete when:** the full run has an explicit operator approval, live
monitoring, a terminal ledger event, reconciled receipts, and a concise outcome
summary.

## 7. Adapt Or Recover Deliberately

Use the same source, lane, table, and key for fixes or added columns. Project
retained per-key responses into same-key updates; use a bounded re-read for
fields absent from retained state so the dashboard preserves row history.

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
  flushes and progress paired with memory-buffered results; run before every full dataset.
- `runguard.py`, `run_dashboard.py`, `watch_chat.py`: vendored runtime, localhost
  dashboard, and chat transport for the active agent harness.
- `EXPLAIN.md`: project-specific statement of intent shown to the operator.

With the CLI helper, run `observer-kit doctor .` after setup and `observer-kit test` after core changes. The bundled-script path runs the matching `test_*.py` files.
