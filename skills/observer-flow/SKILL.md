---
name: observer-flow
description: Orchestration playbook for multi-stage agent-run data transformations. Use when chaining scripts or API steps, making fields depend on earlier results, building a DAG or Clay-style workflow, conditionally enriching rows, joining or expanding entities, rerunning affected steps, caching node outputs, creating a user cookbook of reusable nodes, subflows, or adapters, or coordinating several Observer Kit operations as one reviewed flow.
---

# Observer Flow

Use Observer Flow to design and operate a visible dependency graph made from
small data transformations. The active Codex, Claude, Pi, Command Code, Goose,
or other agent session remains the brain. One coordinator owns scheduling, and
Observer Kit supplies the execution harness, durable review surface, controls,
and approval gate.

Use these terms consistently:

- **Row**: one entity with a stable key.
- **Field**: a visible value on that row.
- **Node**: one coherent transform that produces fields, child rows, or an
  external delivery outcome.
- **Edge**: a declared dependency between nodes.
- **Graph**: the versioned set of nodes, edges, source identity, destination,
  and limits.

A node may produce several related fields from one call. Choose node boundaries
where independent retry, caching, branching, ownership, or review adds value.

## 1. Load The Foundation

Resolve relative paths from the directory containing this `SKILL.md`.

Read the sibling [`../observer-kit/SKILL.md`](../observer-kit/SKILL.md) and its
[`../observer-kit/references/pattern.md`](../observer-kit/references/pattern.md)
in full before creating or adapting a production flow. They define the source
lock, live rows, durable boundaries, schema discovery, controls, watcher,
external receipts, sample proof, and explicit full-run approval that every flow
inherits.

Read [`references/flow-contract.md`](references/flow-contract.md) in full. It
defines graph identity, node modes, scheduler state, cache invalidation,
fan-out, joins, delivery, recovery, and approval scope. Read
[`examples/website-qualification.flow.json`](examples/website-qualification.flow.json)
when a concrete conditional-enrichment graph helps.

When the workflow contains reusable code or repeated node clusters, read
[`references/cookbook-contract.md`](references/cookbook-contract.md). It tells
the agent how to create and maintain the user's project cookbook from real,
proven nodes, subflows, and integration adapters.

Use the CLI surface verified by `observer-kit --help`. The baseline execution
path works today:

```bash
observer-kit run --state-dir .runguard -- \
  python3 flow_coordinator.py --flow pipeline.flow.json --dry-run --limit 10
```

**Complete when:** the Observer Kit contract, flow contract, target workflow,
source, and destination are understood.

## 2. Discover The Real Work

Inspect existing scripts, clients, queries, schemas, fixtures, cached responses,
and destination code. For new work, perform bounded read-only discovery through
the real client and query shape.

Map:

- the operator decision and fields needed to make it;
- source tables, stable entity keys, parent-child keys, and source snapshot;
- each transformation, API call, condition, join, expansion, and delivery;
- fields or child rows produced together by one coherent operation;
- cookbook recipes, prior reuse observations, and recurring mechanics;
- spend, rate, write, quality, and policy ceilings;
- durable results and destination confirmations already available;
- the selected Observer Kit verification branches.

A single cohesive transform stays a direct Observer Kit workflow. Observer Flow
fits two or more independently retryable steps, conditional paths, joins,
fan-out, or selective recomputation.

Present an evidence-backed initial proposal, then ask two to five concise
questions about the decision, visible fields, branch behavior, retained
responses, limits, destination, and update versus comparison lane.

**Complete when:** every node candidate has a clear purpose, input, output,
durable boundary, and operator value.

## 3. Define The Graph

Create a versioned `pipeline.flow.json` manifest following the flow contract.
For each node, declare:

- stable `id`, explicit `version`, and execution `mode`;
- `needs`, `inputs`, `outputs`, and optional `when` condition;
- script or callable identity and configuration identity;
- durable unit: row, child row, bounded batch, or aggregate;
- spend and concurrency ceilings;
- side-effect destination and confirmation contract when active.

Declare source fields and keys before node outputs. Give each output field one
owning node for that graph version. Declare an intentional source-field update
with the node's `updates` list. Represent intentional merges with an explicit
merge node. Give expanded child rows deterministic keys derived from their
parent and source child identity.

Validate structure with:

```bash
python3 /path/to/observer-flow/scripts/validate_flow.py pipeline.flow.json
```

Build a plan ID from the canonical manifest, node script and config hashes,
source snapshot, destination identities, and ceilings. Record the plan ID in
`EXPLAIN.md`, the Observer Kit manifest, and sample ledger.

**Complete when:** the graph is acyclic, every input has an upstream owner,
every output has one owner, and the plan ID captures every material behavior.

## 4. Build The Coordinator

Use one coordinator as the execution authority. For a new local flow, use
SQLite as the authoritative queue and result index; preserve an existing
transactional database or queue when it already provides the same guarantees.
Use Observer Kit JSONL as the append-only audit and dashboard projection. Emit
`flow_graph`, `flow_node`, `flow_unit`, and bounded `flow_batch` events.

The coordinator:

1. loads source rows under stable keys and emits their initial business rows;
2. selects ready nodes from declared dependencies and conditions;
3. computes an input hash from node version, config, and resolved inputs;
4. reuses a successful matching result or executes the node within its ceiling;
5. persists the result and state transition atomically;
6. updates the same dashboard row with newly produced fields;
7. schedules newly ready descendants and checkpoints the completed unit.

Use bounded worker pools owned by the coordinator. Node programs accept one
declared unit and return a structured result containing status, fields, child
rows, evidence, and concise actionable `error`. Expected skips and holds carry
an empty `error` plus a visible reason.

Keep business fields primary in the dashboard. Show a compact current node and
state, place detailed execution metadata in clickable `flow_json`, and update
destination fields on the same row from `pending` through the confirmed outcome.

Wrap external mutation nodes with Observer Kit intent, validation, sink call,
receipt, and reconciliation. Consume structured pause and stop controls between
durable units. Route dashboard chat to the active agent session.

Create or update the user's `flow-cookbook/` when mapped work contains reusable
nodes, subflows, or adapters. Search its catalog first and append each distinct
implementation occurrence to `observations.jsonl`, counting code or graph
locations rather than row executions. When a pattern recurs, show the evidence
and ask whether to keep it separate or promote it. Create the chosen candidate,
include its project code and config in the plan hash, and promote it after its
isolated tests and integrated sample proof pass.

**Complete when:** each completed unit survives restart, each node result appears
on its business row and Flow view, and one scheduler owns graph dispatch.

## 5. Prove A Bounded Sample

Start the dashboard first, then run a stratified sample through the coordinator:

```bash
observer-kit dashboard .runguard
observer-kit run --state-dir .runguard -- \
  python3 flow_coordinator.py --flow pipeline.flow.json --dry-run --limit 10
```

Apply the complete Observer Kit sample proof plus these graph checks:

- the earliest source query, page, or batch respects the sample limit;
- source rows appear before slow descendants finish;
- fields from successive nodes merge into the same stable row;
- each condition demonstrates a taken and skipped path where available;
- child rows and joins use stable, inspectable keys;
- a forced crash resumes from committed node results;
- matching input hashes reuse cached results and preserve prior spend;
- a changed input recomputes the owning node and affected descendants;
- a node failure preserves completed upstream fields and exposes one actionable
  row error;
- candidate cookbook recipes pass isolated tests and their integrated graph paths;
- an external destination canary visibly advances through confirmation;
- counters reconcile source rows, node outcomes, skips, failures, and deliveries.

Record direct evidence for each check and every active Observer Kit verification
branch.

**Complete when:** the sample proves live row evolution, durable node recovery,
selective recomputation, and destination confirmation.

## 6. Review And Approve The Plan

Present the user with:

- source scope and sampled row count;
- nodes, dependencies, conditions, and fields each node produces;
- sample rows, branch outcomes, errors, spend, and planned writes;
- graph plan ID, destination identities, ceilings, and restart boundary;
- update-current-lane or comparison-lane behavior.

Start the full dataset when the user's message or dashboard action explicitly
approves this plan ID for the stated full scope and says to begin. Record the
approval evidence in the ledger before dispatching full-run work.

A request to create a fresh lane, clear a view, inspect data, resume saved work,
or run a canary authorizes exactly that named operation. A graph, node version,
source snapshot, destination, or ceiling change creates a new plan ID and a new
bounded sample for review.

**Complete when:** full-run approval names the reviewed plan and exact scope.

## 7. Run, Adapt, And Compare

Keep one dashboard server attached to the state directory and let
`observer-kit run` create or reuse the run-scoped watcher. The coordinator
continues from durable ready work and emits a terminal graph summary after all
rows reach a terminal state.

For an added enrichment, add or revise the owning node, create a new plan ID,
sample the affected paths, and update the same table and stable keys after
approval. Reuse matching upstream results and recompute the changed node plus
its descendants.

Update the user's cookbook with newly proven recipe versions and proof runs.
Preserve earlier versions for plans that still reference them.

Use the current lane for an intentional evolution of the same dataset and view.
Use a comparison lane for a separate experiment, model, provider, source batch,
or side-by-side result set. Preserve graph history, node versions, input hashes,
receipts, and lineage across both choices.

**Complete when:** every row has an explainable graph state, retries reuse saved
work, current-lane fields evolve in place, and comparison work has its own view.

## Reference Map

- [`references/flow-contract.md`](references/flow-contract.md): production graph
  and scheduler contract; read in full for every build or adaptation.
- [`examples/website-qualification.flow.json`](examples/website-qualification.flow.json):
  generic map, condition, enrichment, and destination example.
- [`scripts/validate_flow.py`](scripts/validate_flow.py): structural graph
  validator; run after each manifest change.
- [`references/cookbook-contract.md`](references/cookbook-contract.md): agent
  contract for creating the user's reusable nodes, subflows, and adapters.
- [`../observer-kit/SKILL.md`](../observer-kit/SKILL.md): execution workflow and
  sample gate inherited by every flow.
- [`../observer-kit/references/pattern.md`](../observer-kit/references/pattern.md):
  detailed harness, ledger, durability, controls, and delivery contract.
