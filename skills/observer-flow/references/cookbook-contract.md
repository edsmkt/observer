# User Cookbook Contract

Observer Flow teaches the active agent to create and maintain a cookbook for
the user's real workflows. The cookbook belongs to the user or project. It grows
from integrations and node clusters that the agent has implemented, sampled,
and proven in that environment.

## Contents

1. Ownership and scope
2. Folder shape
3. Recipe kinds
4. Catalog contract
5. Recipe contract
6. Recipe lifecycle
7. Reuse observation log
8. Consultation and promotion
9. Reuse process
10. Promoting repeated work
11. Plan identity and approval
12. Sample proof

## Ownership And Scope

Create a project cookbook when the graph contains reusable code, a repeated
node cluster, or an integration adapter that will serve another field or flow.
Use the project's existing module and test conventions. A practical default is:

```text
flow-cookbook/
```

Ask the user whether proven recipes should also enter a shared user cookbook.
A shared cookbook may live at a user-selected path, such as:

```text
~/.observer-flow/cookbook/
```

The project cookbook remains the production source pinned by the graph plan.
Copy a selected shared recipe into the project cookbook before execution, then
hash that project copy into the reviewed plan.

Keep credentials in the project's established secret store. Cookbook adapters
declare secret names and configuration fields while recipe files contain code,
contracts, fixtures, and sanitized evidence.

## Folder Shape

Use this shape as a starting point and adapt file extensions to the project:

```text
flow-cookbook/
  catalog.json
  observations.jsonl
  nodes/
    normalize-and-validate-domain/
      recipe.json
      normalize.py
      test_normalize.py
  subflows/
    qualify-enrich-deliver/
      recipe.json
      fragment.flow.json
      test_subflow.py
  adapters/
    crm-client/
      recipe.json
      client.py
      test_client.py
```

Co-locate each recipe's implementation, contract, fixtures, and tests. Give
every recipe directory a stable ID and each released behavior an explicit
version.

## Recipe Kinds

### Node Recipe

One coherent reusable operation with declared inputs and outputs. Examples:

- ordered fallback and candidate selection;
- normalized API pagination;
- schema probe and response projection;
- classification with reasoning;
- deterministic deduplication;
- bounded batch lookup;
- confirmed destination sink.

### Subflow Recipe

Several connected nodes that form a repeated business pattern. Examples:

- inspect, classify, conditionally enrich, and deliver;
- expand parent rows, enrich children, and join a summary;
- read source pages, normalize records, validate, and upsert;
- compare two providers and route uncertain rows to review.

### Adapter Recipe

Reusable code that connects node logic to one real provider or destination.
Examples include a CRM batch reader, database upsert adapter, spreadsheet
receipt verifier, webhook client, or provider-specific pagination helper.

## Catalog Contract

The agent creates `catalog.json` as a concise index:

```json
{
  "format_version": 1,
  "recipes": [
    {
      "id": "normalize-and-validate-domain",
      "kind": "node",
      "version": "1",
      "status": "proven",
      "path": "nodes/normalize-and-validate-domain",
      "purpose": "Normalize a domain and return its validation evidence",
      "inputs": ["raw_domain"],
      "outputs": ["domain", "domain_valid", "normalization_json"],
      "proof": "runguard:domain-normalization-sample.jsonl"
    }
  ]
}
```

Keep recipe IDs unique within the cookbook. Keep earlier versions available for
lineage while the catalog points to the current recommended version.

## Recipe Contract

Each recipe directory contains `recipe.json`:

```json
{
  "format_version": 1,
  "recipe": {
    "id": "normalize-and-validate-domain",
    "kind": "node",
    "version": "1",
    "status": "candidate",
    "purpose": "Normalize a domain and return its validation evidence"
  },
  "contract": {
    "inputs": [
      {"name": "raw_domain", "type": "string", "required": true}
    ],
    "outputs": [
      {"name": "domain", "type": ["string", "null"]},
      {"name": "domain_valid", "type": "boolean"},
      {"name": "normalization_json", "type": "object"}
    ],
    "durable_unit": "row",
    "side_effect": false
  },
  "implementation": {
    "entrypoint": "normalize.py",
    "tests": ["test_normalize.py"]
  },
  "limits": {
    "max_operations_per_unit": 4,
    "max_attempts": 1
  },
  "proof": {
    "sample_run": "",
    "plan_id": "",
    "verified_at": ""
  }
}
```

Declare:

- stable recipe identity, kind, version, status, and purpose;
- typed input and output ports;
- durable unit, side effects, and retained evidence;
- entrypoint, fixtures, and tests;
- provider, concurrency, spend, retry, rate, and write ceilings;
- sample run, plan ID, and verification time after proof.

## Recipe Lifecycle

Use three visible states:

- `candidate`: implemented for the current flow with isolated tests ready;
- `proven`: passed its isolated tests and the integrated Observer Kit sample;
- `superseded`: retained for lineage after a newer reviewed version becomes
  current.

Create a candidate while building the first real use. Promote it to proven after
the bounded sample demonstrates its contract. Create a new version for changed
behavior and keep the earlier proven version available to existing plans.

A recipe earns reuse when its boundary is coherent and another node or flow can
bind the same contract. Small local helpers remain inside their owning recipe.

## Reuse Observation Log

The agent creates `flow-cookbook/observations.jsonl` as an append-only history
of recurring implementation structure. Count one occurrence for each distinct
code location, node definition, subflow, or workflow adaptation. Row count,
retry count, and ordinary execution volume remain runtime metrics rather than
reuse occurrences.

Use a stable pattern ID and append an observation such as:

```json
{"ts":"2026-07-10T15:00:00Z","event":"pattern_observed","pattern_id":"normalize-validate-domain","occurrence":2,"summary":"normalize, parse, validate, and retain evidence","locations":["flows/import.py:normalize_domain","flows/enrich.py:prepare_domain"],"shape":{"inputs":["raw_domain"],"outputs":["domain","domain_valid","normalization_json"],"durable_unit":"row","side_effect":"none"},"fingerprint":"sha256:..."}
```

Build the fingerprint from normalized mechanics rather than variable names:

- ordered operation or call sequence;
- typed input and output shape;
- provider or destination class using credential-free identity;
- condition and acceptance semantics;
- retry, cache, spend, rate, and concurrency policy;
- durable unit and side-effect boundary.

Record locations, similarities, and meaningful differences. A new location
appends a new observation with the current occurrence count. A changed shape
receives a new pattern ID or fingerprint so distinct contracts stay visible.

## Consultation And Promotion

Ask the user for their preferred proposal threshold when creating the cookbook.
A practical default is:

- first distinct occurrence: record the pattern;
- second distinct occurrence: mark it recurring and retain evidence;
- third distinct occurrence: recommend code-node, subflow, or adapter promotion;
- an expensive, fragile, or policy-sensitive repeat: present it earlier with
  the reason.

Before promotion, show:

- occurrence count and exact locations;
- common inputs, outputs, policies, and boundaries;
- meaningful differences that need parameters or separate recipes;
- proposed recipe kind and public contract;
- expected maintenance, consistency, spend, and observability benefit;
- migration and sample work involved.

Ask the user whether to promote it into a code node, subflow, or adapter, keep
the implementations separate, or revisit after another occurrence. Append the
choice as a decision event:

```json
{"ts":"2026-07-10T15:10:00Z","event":"promotion_decision","pattern_id":"normalize-validate-domain","occurrence":3,"decision":"promote_code_node","user_context":"Use one shared normalizer for imports and enrichment."}
```

Create a candidate recipe after the user selects promotion. A keep-separate or
revisit decision records its rationale and next review trigger, helping later
agents respect the user's earlier choice.

## Reuse Process

For every new graph or adaptation, the agent:

1. reads `flow-cookbook/catalog.json` when present;
2. reads `observations.jsonl` and respects prior promotion decisions;
3. compares mapped node candidates with existing recipe contracts;
4. appends distinct pattern observations and consults at the chosen threshold;
5. selects an exact proven version or creates an approved project candidate;
6. binds real fields, adapters, destination identities, and ceilings;
7. runs isolated recipe tests;
8. includes recipe files and configuration in node and plan hashes;
9. exercises the integrated paths in the bounded sample;
10. updates catalog status and proof after verification;
11. presents new, reused, and upgraded recipes in the operator proposal.

When the user has a shared cookbook, search it after the project catalog. Copy
the chosen version into the project, preserve attribution and source version,
and let the project copy become the execution dependency.

## Promoting Repeated Work

When the agent sees the same mechanics repeated across nodes, branches, or
workflows, it evaluates whether they form one coherent reusable code node.
Ordinary execution of one node across many rows remains normal graph work;
promotion responds to repeated implementation structure.

Combine repeated mechanics into one code node when they share:

- stable input and output ports;
- one retry, cache, and durable-result boundary;
- one spend, rate, concurrency, and side-effect policy;
- intermediate details that fit clickable evidence;
- a clear operator-facing purpose;
- isolated tests that cover the complete internal sequence.

Use a reusable subflow when intermediate steps provide independent value through:

- operator-visible business fields or review decisions;
- conditional routing, fan-out, joins, or parallel execution;
- separate provider spend or external side effects;
- independent caching, retries, durable commits, or selective recomputation.

Possible code-node candidates include ordered provider fallback, paginated fetch
plus normalization, repeated parsing and validation, bounded batch assembly,
candidate scoring and selection, or destination payload preparation. The agent
writes each promoted node against the user's actual contracts and keeps its
internal attempts, decisions, and spend available as evidence.

Every real API request inside a code node still acquires the shared provider
throttle. Every external mutation retains its Observer Kit intent, confirmation,
receipt, and reconciliation boundary. Combining code preserves these controls.

## Plan Identity And Approval

Include these values in each node implementation hash and graph plan ID:

- recipe ID, version, and status;
- implementation, fixture, prompt, and parser hashes;
- bound input and output fields;
- adapter and provider configuration identities;
- conditions, acceptance rules, and merge policy;
- durable unit and retained response policy;
- spend, rate, concurrency, retry, quality, and write ceilings.

An upgraded recipe creates a new plan ID. Sample the changed path, show the
recipe delta to the user, and collect approval for that plan.

## Sample Proof

Every candidate runs isolated tests plus the Observer Flow integrated sample.
Prove:

- declared inputs produce typed outputs;
- clean misses, skips, holds, and failures follow the recipe contract;
- shared throttles and hard ceilings apply to every provider call;
- durable state commits before the next unit begins;
- crash and resume reuse completed recipe results;
- same-key dashboard rows receive the expected fields;
- conditions, fan-out, joins, and sinks cover their representative branches;
- destination recipes reconcile intents and receipts;
- recipe counters and evidence reconcile with graph totals.

Record the sample run, plan ID, verification time, and concise proof summary in
the recipe and catalog. Present the cookbook additions and upgrades alongside
the full-run approval request.
