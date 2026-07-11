# Live Observer Flow Demo

This example runs a compact six-node coordinator over 30 synthetic account rows.
It uses SQLite for durable node results and Observer Kit JSONL for the Data,
Flow, Attention, Timeline, and Run info views.

The graph inspects each synthetic profile, qualifies the account, sends each
row down one of three branches, finds a contact for qualified rows, and prepares
a simulated sheet row. One profile response and one contact lookup fail on
purpose so the row trace and Attention view have useful states to inspect.

From the repository root:

```bash
mkdir -p examples/observer-flow-demo/.runguard
cp examples/observer-flow-demo/EXPLAIN.md \
  examples/observer-flow-demo/.runguard/EXPLAIN.md
python3 -B -m observer_kit dashboard \
  examples/observer-flow-demo/.runguard --port 8532
```

In another terminal:

```bash
python3 -B examples/observer-flow-demo/flow_coordinator.py \
  --state-dir examples/observer-flow-demo/.runguard \
  --session live-flow-demo --limit 30 --delay 0.8 --dry-run
```

Open `http://localhost:8532/`, select the run, and choose **Flow**. The example
uses synthetic data, synthetic spend units, and a simulated destination.

After reviewing that sample, run the explicitly enabled simulation against the
same lane and state:

```bash
python3 -B examples/observer-flow-demo/flow_coordinator.py \
  --state-dir examples/observer-flow-demo/.runguard \
  --session live-flow-demo --limit 30 --delay 0.8 --full-run
```

Matching transform inputs reuse their durable node results. The sink includes
the execution mode in its input hash, so its row moves from `planned` in the
dry run to `simulated append` in the full-run simulation.

The demo scopes one graph and one source table to each SQLite file. It proves
versioned input hashes, attempts, cache reuse, row evolution, and aggregate
node outcomes. A production coordinator also implements the leases, queue,
transactional outbox, children, write intents, and write receipts defined in
[`flow-contract.md`](../../skills/observer-flow/references/flow-contract.md).
The mixed coordinator schedules `map`, `batch`, and `sink` nodes from declared
dependencies, so manifest array order is presentation rather than execution
order. Structured `when` conditions apply to individual and batch nodes.

## Batch API Variant

The second manifest combines individual homepage requests with discounted
labeling batches. It keeps the same row keys while exposing four bounded batch
calls, their members, partial outcomes, cost, and simulated savings.

Keep the same dashboard running and launch a separate lane:

```bash
cp examples/observer-flow-demo/BATCH_EXPLAIN.md \
  examples/observer-flow-demo/.runguard/EXPLAIN.md
python3 -B examples/observer-flow-demo/batch_flow_coordinator.py \
  --state-dir examples/observer-flow-demo/.runguard \
  --session batch-flow-demo --limit 24 --batch-size 6 --delay 0.8 --dry-run
```

The sidebar retains the earlier account-routing run and adds the homepage batch
run as a separate view. Select **Label homepages in batches** to inspect the
four calls while the Data table continues to show one evolving website row.
After review, repeat the command with `--full-run`; the scrape and batch results
are reused while the simulated export sink advances from `planned` to
`simulated append`.
