# Observer Flow Contract

This reference defines the production contract for a dependency-driven data
workflow. Read it with the Observer Kit production pattern. Observer Flow owns
graph design and scheduling; Observer Kit owns supervised execution.

## Contents

1. Architecture boundary
2. Graph vocabulary
3. Manifest contract
4. Node modes
5. Node execution envelope
6. Authoritative state
7. Scheduler algorithm
8. Hashes, caching, and invalidation
9. Dashboard projection
10. Conditions, expansion, joins, and batches
11. External destinations
12. Concurrency and shared limits
13. Recovery and reconciliation
14. Approval scope
15. Flow evolution
16. User cookbook
17. Production checklist

## Architecture Boundary

Observer Flow is an orchestration playbook. It teaches the active agent to
create or adapt a graph coordinator from the real workflow. The active agent
session retains judgment and responds to operator chat. The coordinator performs
deterministic scheduling. Observer Kit supplies locks, ledgers, dashboard rows,
controls, durable run evidence, and approval.

For a new local flow, use these layers:

```text
active agent session
        |
        | designs, reviews, adapts, responds
        v
flow manifest + coordinator
        |
        | schedules declared units
        v
node callables or node scripts
        |
        | return fields, child rows, evidence, outcomes
        v
SQLite authoritative state + transactional outbox
        |
        | projects committed changes
        v
Observer Kit JSONL + dashboard
```

Use one coordinator for one graph run. Workers execute assigned units and return
results to that coordinator. The coordinator owns readiness, retries, commits,
controls, and terminal status.

An existing transactional queue or workflow database may replace SQLite when it
provides durable results, atomic readiness transitions, leases, and an event
outbox. Keep the JSONL ledger as the dashboard and audit projection.

## Graph Vocabulary

- **Entity**: a source or derived object with a stable key.
- **Row**: the dashboard representation of one entity.
- **Field**: a named value on a row.
- **Node**: one versioned executable transformation.
- **Unit**: the smallest node input committed atomically, such as one row, one
  child row, one bounded batch, or one aggregate partition.
- **Edge**: a dependency declared through `needs` and resolved inputs.
- **Condition**: a structured predicate evaluated from committed fields.
- **Result**: the durable output for one node and unit input hash.
- **Plan**: a graph manifest plus scripts, configs, source snapshot,
  destinations, and ceilings.
- **Lane**: one Observer Kit dashboard history for a plan evolution or
  comparison.

Choose one node for one coherent operation. A website fetch can produce title,
description, and social links together. A classification call can produce a
boolean and reasoning together. This preserves provider efficiency and gives
retry boundaries real meaning.

Choose a new node when one of these changes independently:

- prerequisites or condition;
- provider, model, script, or owner;
- cache lifetime or retry policy;
- durable unit or concurrency ceiling;
- operator review value;
- external side effect.

## Manifest Contract

Store the graph as JSON so every agent and runtime can parse the same structure
with standard tooling. A manifest contains:

```json
{
  "format_version": 1,
  "graph": {
    "id": "website-qualification",
    "version": "2026-07-10"
  },
  "source": {
    "identity": "websites.csv",
    "snapshot": "sha256:...",
    "table": "websites",
    "key": "website",
    "fields": ["website", "company_name"]
  },
  "state": {
    "store": ".observer/website-qualification.flow.sqlite3"
  },
  "nodes": [],
  "limits": {
    "sample_rows": 10,
    "max_in_flight": 4,
    "max_provider_calls": 30,
    "max_writes": 1000
  }
}
```

### Graph

- `id` remains stable across intentional versions of the same logical graph.
- `version` changes when the operator-facing graph design changes.

### Source

- `identity` is the immutable source scope used by Observer Kit locking.
- `snapshot` identifies the exact reviewed input state: file hash, export ID,
  table query plus watermark, sheet revision, or equivalent.
- `table` names the primary dashboard business table.
- `key` names the stable field used for same-row updates.
- `fields` lists source fields available before any node runs and includes the
  stable key.

### State

- `store` points to the authoritative graph state for resume and cache lookup.
  Place it under the private Observer Kit state directory for a local flow.

### Nodes

Each node declares:

```json
{
  "id": "qualify_saas",
  "version": "1",
  "mode": "map",
  "script": "nodes/qualify_saas.py",
  "needs": ["inspect_website"],
  "inputs": ["website", "website_title", "description"],
  "outputs": ["is_saas", "saas_reasoning"],
  "updates": [],
  "when": {
    "all": [
      {"field": "description", "op": "present"}
    ]
  },
  "durable_unit": "row",
  "concurrency": 4,
  "max_attempts": 3
}
```

- `id` is stable and unique inside the graph.
- `version` changes when code, prompt, model, parsing, or semantics change.
- `mode` selects the execution shape.
- `script` or a project-specific callable identity resolves the implementation.
  The bundled JSON validator uses normalized relative Python paths under
  `nodes/`; the executor resolves and contains that path again before reading or
  importing it.
- `needs` lists prerequisite node IDs.
- `inputs` lists committed source or ancestor fields.
- `outputs` lists fields owned by this node version.
- `updates` lists output fields that intentionally replace declared source
  fields while preserving the source value in lineage.
- `when` is an optional structured condition.
- `durable_unit` names the atomic result boundary.
- `concurrency` and `max_attempts` state local execution ceilings.
- `spend` may declare provider, units per call, and a hard unit ceiling.
- `side_effect` declares a destination and confirmation contract for a sink.
- `recipe`, when present, records the cookbook recipe `id`, `version`, and
  lifecycle `status`; all three participate in node and plan identity.

Give each output field one owner. A node may update a declared source field by
listing it in both `outputs` and `updates`. Preserve the original source value
in row lineage. Use an explicit merge node when several producers contribute to
one final field. This makes lineage, invalidation, and operator explanations
deterministic.

### Limits

Declare limits that the coordinator enforces in code. Include the bounded sample
size, maximum in-flight units, provider spend or request ceilings, destination
write ceiling, and quality thresholds relevant to the graph.

## Node Modes

### Map

Consumes one row and returns fields for that row. This covers normalization,
lookup, classification, enrichment, and validation.

### Expand

Consumes one parent row and returns zero or more child entities. Each child key
combines the stable parent key with a stable source child identity. Emit child
rows into a declared child table.

### Join

Consumes one row plus committed related entities and returns merged fields. The
join definition declares relationship keys and multiplicity.

### Reduce

Consumes one bounded partition and returns an aggregate row or aggregate fields.
The partition identity becomes the durable unit and cache key component.

### Batch

Consumes a bounded set for provider efficiency and returns one result envelope
per member. Commit each returned member atomically or commit the entire batch
when the provider response forms one indivisible authoritative unit.

### Sink

Consumes committed fields and delivers to an external CRM, database,
spreadsheet, file, webhook, queue, or API. It returns destination identity,
receipt, confirmation state, and any destination-derived fields.

## Node Execution Envelope

The coordinator supplies one structured unit:

```json
{
  "run_id": "runguard:website-qualification",
  "plan_id": "sha256:...",
  "table": "websites",
  "row_key": "acme.example",
  "node_id": "qualify_saas",
  "node_version": "1",
  "input_hash": "sha256:...",
  "inputs": {
    "website": "acme.example",
    "website_title": "Acme",
    "description": "Subscription planning software"
  },
  "dry_run": true,
  "attempt": 1
}
```

The node returns one structured result:

```json
{
  "status": "succeeded",
  "fields": {
    "is_saas": true,
    "saas_reasoning": "The site describes subscription software."
  },
  "children": [],
  "evidence": {
    "provider": "example-provider",
    "response_ref": "responses/acme.example/qualify_saas.json"
  },
  "error": ""
}
```

Supported terminal result states are `succeeded`, `skipped`, `held`, and
`failed`. `skipped` and `held` include a concise reason. `failed` includes an
actionable `error`. A successful result may include fields, child rows,
evidence, spend units, rate metadata, and a retained-response reference.

Node implementations keep external mutation inside sink nodes. Computational
nodes return data to the coordinator for commit and projection.

## Authoritative State

For SQLite, use WAL mode, a busy timeout, foreign keys, and explicit
transactions. A practical schema contains:

- `flow_runs`: run ID, plan ID, source snapshot, mode, status, approval ref;
- `rows`: table, row key, source JSON, current business JSON, source hash;
- `node_results`: row key, node ID, version, input hash, status, result JSON,
  attempt, timestamps;
- `queue`: ready or leased units, priority, lease owner, lease expiry;
- `children`: parent and child table/key lineage;
- `write_intents` and `write_receipts`: external delivery evidence;
- `outbox`: committed Observer Kit events waiting for JSONL projection.

Use a uniqueness constraint on `(run_id, table, row_key, node_id, input_hash)`.
The commit transaction:

1. verifies the active lease and input hash;
2. writes the node result;
3. merges owned fields into the business row;
4. inserts child rows and lineage when present;
5. marks the queue unit terminal;
6. creates newly ready queue units;
7. appends dashboard and timeline events to the outbox;
8. commits once.

After commit, flush outbox events to the Observer Kit JSONL and mark them
delivered. Startup flushes pending outbox rows first, which lets durable state
restore a dashboard after a process interruption.

## Scheduler Algorithm

Use this scheduling loop:

1. Load or resume the approved plan and source snapshot.
2. Flush committed outbox events.
3. Consume Observer Kit controls at a durable boundary.
4. Reclaim expired leases.
5. Select units whose dependencies are terminal.
6. Evaluate structured conditions from committed fields.
7. Mark condition misses as `skipped` with a visible reason.
8. For a taken path, run after required dependencies succeed or enter `held`
   with the upstream reason when a dependency is held or failed.
9. Compute each selected unit's input hash.
10. Reuse a successful matching result or lease the unit to a worker.
11. Enforce graph, provider, node, and destination ceilings.
12. Receive the worker result and commit it transactionally.
13. Flush outbox events and schedule descendants.
14. Repeat until every source and derived row reaches a graph terminal state.
15. Reconcile intents and receipts, then emit the terminal graph summary.

Keep worker leases bounded. A worker heartbeat extends a live lease. An expired
lease returns the unit to ready state while committed results remain reusable.

## Hashes, Caching, And Invalidation

### Node implementation hash

Hash the node script or callable module, referenced prompts, model identity,
parser configuration, and semantic options. Store this value with the node
version.

### Input hash

Compute SHA-256 over canonical JSON containing:

- node ID, version, and implementation hash;
- resolved input field names and values;
- upstream result identities used by a join or aggregate;
- node configuration and explicit refresh token;
- destination identity for a sink.

Use stable key ordering, UTF-8, explicit nulls, and deterministic scalar
normalization.

### Cache reuse

A successful result with the same node ID, implementation version, and input
hash is reusable. Record cache reuse as a node outcome and project the retained
fields onto the current row.

### Selective invalidation

When source data or one node changes, recompute that node where its input hash
changes. Descendants naturally receive new hashes from changed inputs. Matching
ancestors retain their cached results. An explicit refresh token supports
operator-requested re-enrichment while preserving lineage to the earlier result.

### Plan ID

Build the plan ID from:

- canonical manifest;
- every node implementation and config hash;
- source identity and snapshot;
- destination identities;
- spend, rate, write, policy, and quality ceilings.

The plan ID binds human approval. Cache keys remain node-specific, so a new plan
can reuse unaffected prior results after sample proof.

## Dashboard Projection

The dashboard represents business evolution rather than one row per node.

1. Emit the source business row as soon as its key is known.
2. Emit each committed node result with the same table and row key.
3. Merge newly owned fields while retaining earlier fields.
4. Keep expected skips and holds visible with an empty `error` and a reason.
5. Fill `error` only for an actionable row failure.
6. Update destination status on the same row from `pending` to `writing`,
   `verifying`, and the confirmed destination outcome.

Useful compact flow fields are:

- `flow_status`: waiting, running, complete, held, or failed;
- `current_node`: the active or latest node label;
- `flow_json`: clickable per-node states, versions, cache decisions, and timing;
- business outputs selected with the operator;
- destination status and concise actionable `error`.

Keep phase progress for source discovery before stable business keys exist.
Once a key exists, advance the business row during each slow stage. Choose three
to five headline counters with the operator and reconcile them against row
states at terminal events.

### Live Flow Events

The Flow tab is a visual projection of committed coordinator state. Scheduling
and execution remain in the coordinator. Emit these JSONL events:

- `flow_graph` once near the start of each attempt, with `plan_id`,
  `rows_total`, and a `graph` object containing the graph label, description,
  primary table, nodes, and edges;
- `flow_node` when a node starts and after each terminal unit, with `node_id`,
  label, aggregate status, completed and total units, outcome counts, and spend;
- `flow_unit` when a row enters a node and after its durable terminal commit,
  with `node_id`, table, stable row key, status, reason, timing, spend, and
  actionable `error`;
- `flow_batch` when a bounded batch request starts and after its raw response
  plus member outcomes are durable, with `batch_id`, position, total batches,
  member count, request identity, outcome counts, spend, and equivalent
  single-item cost.

A compact graph event looks like:

```json
{
  "event": "flow_graph",
  "plan_id": "sha256:...",
  "rows_total": 30,
  "graph": {
    "id": "account-routing",
    "label": "Account routing",
    "table": "accounts",
    "nodes": [
      {"id": "inspect", "label": "Inspect profile", "kind": "extract"},
      {"id": "qualify", "label": "Qualify account", "kind": "decision"}
    ],
    "edges": [{"from": "inspect", "to": "qualify", "label": "then"}]
  }
}
```

Emit the terminal `flow_unit` through the same committed outbox as its durable
node result. Follow it with the same-key `record` projection containing the new
business fields and refreshed `flow_json`. Emit `flow_node` after that boundary
so the graph card, row trace, Data table, and counters describe the same state.

Include node labels, kinds, scripts, input and output ports, and structured
conditions in `flow_graph`. The dashboard uses those declared fields for the
graph and inspector. Keep credentials, request headers, and private provider
payloads in the project state store and expose selected business evidence
through clickable JSON fields.

Use `pending`, `running`, `complete`, `held`, and `failed` for aggregate node
state. Use `running`, `succeeded`, `skipped`, `held`, `failed`, and `cached` for
row units. Expected branch misses use `skipped` with a concise reason and an
empty `error`. The selected row trace then shows every taken, skipped, held,
failed, or reused path through the graph.

## Conditions, Expansion, Joins, And Batches

### Conditions

Use structured predicates such as `all`, `any`, `field`, `op`, and `value`.
Supported operators may include `eq`, `ne`, `present`, `empty`, `contains`,
`gt`, `gte`, `lt`, `lte`, and `in`. Record the evaluated values and skip reason
for review. `in` compares the row value with an explicit list of accepted
values. The validator and coordinator expose the same operator set.

### Expansion

Persist parent fields first, then child entities with deterministic keys. A
website-to-contacts expansion might use `website + provider_contact_id`.
Project parent summary fields such as child count while retaining the child
table for inspection.

### Joins

Declare relationship keys, cardinality, and the field ownership of the join
node. Persist join inputs before evaluation. For late-arriving children, enqueue
the affected join unit when its related-set hash changes.

### Batches

Keep batches bounded by provider and restart semantics. Return a result envelope
per member when the provider response supports independent outcomes. Persist one
indivisible batch result when the authoritative response itself is atomic.

### Aggregates

Give each aggregate partition a stable identity and input-set hash. Recompute an
aggregate when that set hash changes and project the result to its declared row.

## External Destinations

Treat each sink as a side-effect node with:

- destination type and immutable identity;
- idempotency key derived from plan, node, row, and intended payload hash;
- validation and policy gate;
- write intent committed before dispatch;
- confirmed sink call;
- receipt containing destination record identity and observed outcome;
- reconciliation path for an interrupted intent;
- replay behavior for held or failed delivery.

Dry-run sink nodes emit planned payloads and destination fields while performing
zero mutation. A full-run canary chooses one reviewed unit, shows its row moving
through delivery states, verifies the real destination, and records the receipt.

For a spreadsheet append, a confirmed row might update `sheet_status` from
`pending` to `appended`. For an upsert, use the destination's observed status,
record ID, and version. The same pattern applies to CRM, database, file,
webhook, queue, and API sinks.

## Concurrency And Shared Limits

The coordinator enforces:

- graph-wide in-flight units;
- per-node worker count;
- per-provider shared throttle and credit ceiling;
- per-destination write ceiling;
- source page or query ceiling for samples;
- retry and elapsed-time ceilings;
- quality or policy pause thresholds.

Use Observer Kit source locks for overlapping source scopes and shared provider
throttles across independent flows. Different sources may run concurrently when
their row scopes are provably disjoint. A shared destination uses idempotency,
receipts, and destination-specific pacing.

## Recovery And Reconciliation

On startup:

1. load the run and approved plan;
2. verify source snapshot and destination identity;
3. flush the transactional outbox;
4. restore committed business rows and node results;
5. reconcile pending external intents;
6. reclaim expired worker leases;
7. compute ready units from committed state;
8. continue the same Observer Kit lane.

A forced crash sample should prove:

- completed provider units reuse their durable results;
- committed fields return to the dashboard through the outbox;
- active units retry within the declared attempt ceiling;
- descendants wait for terminal prerequisites;
- confirmed external writes reuse receipts or reconcile destination state;
- spend and write counters retain their prior totals.

## Approval Scope

Approval is evidence bound to one plan. Record an `approval` event containing:

- plan ID;
- graph ID and version;
- source identity, snapshot, and full row scope;
- destination identities;
- provider, spend, write, policy, and quality ceilings;
- sample run ID and reviewed timestamp;
- operator action or quoted approval message;
- approved mode: canary or full run.

Begin full dispatch after an explicit operator action approves the full plan and
says to start. Requests for inspection, lane creation, ledger cleanup, sample,
canary, pause, stop, or resume grant exactly the named operation.

A material change creates a new plan ID. Run a bounded sample for changed paths,
present the delta, and collect approval for that plan. Operational resume under
the same plan continues from committed work and existing approval.

## Flow Evolution

### Add an enrichment

Add the node and its owned fields, connect declared dependencies, bump the graph
version, and create a new plan ID. Sample affected paths. After approval, update
the same table and stable keys. Reuse matching ancestors and compute the new node
plus descendants.

### Change a provider or model

Bump the owning node version and implementation hash. Retain earlier results for
lineage. Sample old-versus-new outcomes and choose current-lane update or a
comparison lane with the operator.

### Change visible columns

Project retained fields from authoritative node results onto the same row. A
bounded re-read supplies fields absent from retained evidence. Record the new
projection version and preserve source response lineage.

### Redo a run

Use a comparison lane for side-by-side evaluation. Use the current lane for an
intentional continuation or correction of the same view. Keep stable keys and
plan IDs visible in both cases.

### Split or combine nodes

Choose boundaries around retry and cache semantics. Assign new node versions,
map old fields to new owners, sample branch behavior, and let input hashes drive
selective recomputation.

## User Cookbook

When a project contains reusable node code, repeated graph clusters, or shared
integration adapters, create and maintain the user's `flow-cookbook/`. Read
[`cookbook-contract.md`](cookbook-contract.md) for its folder shape, recipe
contract, candidate-to-proven lifecycle, code-node promotion criteria, hashing,
and sample proof.

Search the project cookbook before creating a node. A chosen recipe version
becomes a project execution dependency and contributes its implementation,
configuration, and bindings to the graph plan ID. Present cookbook additions,
reuse, and upgrades in the operator proposal.

## Production Checklist

Before sample:

- Observer Kit skill and production pattern loaded;
- operator decision and visible fields proposed;
- source identity, snapshot, table, and stable keys declared;
- manifest validates and graph order is acyclic;
- node scripts, versions, inputs, outputs, conditions, and limits mapped;
- SQLite or existing transactional state plus outbox ready;
- plan ID recorded in `EXPLAIN.md` and run manifest.

During sample:

- earliest source work bounded;
- source and node fields stream into same-key rows;
- taken and skipped paths visible;
- failures use actionable `error`;
- durable state and outbox advance together;
- crash, resume, cache reuse, and selective invalidation proven;
- external canary confirmed with intent and receipt;
- Observer Kit universal and active-branch checks recorded.

Before full run:

- sample reviewed in dashboard;
- counters and row outcomes reconcile;
- exact plan ID, scope, destinations, and ceilings presented;
- explicit full-run approval event recorded;
- one coordinator and one covering watcher active.

At completion:

- every unit terminal or intentionally held;
- pending intents reconciled;
- durable state, JSONL rows, counters, and destinations agree;
- final summary reports rows, node outcomes, cache reuse, spend, writes, holds,
  failures, and restart evidence.
