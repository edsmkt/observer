# Observer

**Run locks, JSONL ledgers, a localhost dashboard, and an agent CLI** so agent-run
data work stays reviewable — sample first, fix in place, approve the full run.

An agent can pull records, enrich them, and write results back while you wait in
chat. Without structure you cannot see what landed, catch a bad row early, or
tell the agent what to change while the job is still running. Observer gives you
that structure.

| Piece | What it is |
| --- | --- |
| **Run locks** | Source-based locks and durable checkpoints so retries resume the same lane |
| **JSONL ledger** | Append-only events for live rows, progress, and audit |
| **Dashboard** | Localhost table, flow graph, chat, pause / stop / approve |
| **AXI** | `observer-kit axi` — dense TOON status for agents (live runs, orphans, `install_skew`, next commands) |
| **Compliance gate** | PreToolUse nudge that blocks side-effect scripts not under Observer (not a security boundary — [details](#side-effect-compliance-gate)) |
| **Observer Flow** | Dependency graph for multi-step pipelines under the same harness |

**Observer Kit** supervises each run. **Observer Flow** coordinates dependent
steps. Agents orient with AXI; humans review on the dashboard. Stdlib-only
runtime (Python 3.9+, macOS / Linux, MIT).

<p align="center">
  <img alt="Observer Flow dependency graph" src="assets/dashboard-flow.png" width="960" />
</p>

Use it for imports, exports, enrichment, backfills, CRM updates, spreadsheet
pushes, and any job that changes or moves many records.

## How It Works

```text
Agent creates or adapts the real workflow
                 |
        +--------+---------+
        |                  |
   one script       dependent steps
        |                  |
        |           Observer Flow graph
        |                  |
        +--------+---------+
                 |
          Observer Kit harness
                 |
        +--------+---------+
        |                  |
  observer-kit axi     dashboard
  (agent status)     (human eyes)
        |                  |
        +--------+---------+
                 v
   Live sample, review, controls, and approval
```

The same run can continue after a fix. Existing rows update in place, so you
see what changed instead of losing the earlier attempt.

## Two Layers

This repository includes two complementary agent skills and one shared local
dashboard:

- **[Observer Kit](.claude/skills/observer-kit/SKILL.md)** supervises execution with
  live rows, source locks, durable resume, shared throttling, controls, review,
  and explicit full-run approval.
- **[Observer Flow](.claude/skills/observer-flow/SKILL.md)** designs multi-stage graphs
  where later transformations depend on earlier results. Nodes may map, batch,
  branch, expand, join, aggregate, or deliver records.

Observer Flow treats a row as the entity the user follows, a field as visible
data, and a node as one coherent operation that may produce several fields or
child rows. One coordinator schedules the nodes and updates the same row as
results land. Observer Kit remains the harness around the complete run.

As the agent proves reusable nodes, subflows, and integrations, Observer Flow
creates a project `flow-cookbook/`. That cookbook reflects the user's real APIs,
fields, limits, and tests. When repeated mechanics share one meaningful contract
and durable boundary, the agent logs each distinct occurrence, shows the user
the evidence, and asks whether to promote them into a reusable code node or
subflow.

An Observer Flow coordinator runs through the same CLI and dashboard:

```bash
observer-kit run --state-dir .observer -- \
  python3 flow_coordinator.py --flow pipeline.flow.json --dry-run --limit 10
```

The [synthetic Flow demos](examples/observer-flow-demo/README.md) show
conditional routing and a mixed workflow where individual homepage requests
feed a discounted batch-labeling API while results still return to their stable
business rows.

## Dashboard

The dashboard runs only on localhost. It reads the JSONL ledger the workflow
writes while it works.

It assumes a trusted local machine. Do not forward the dashboard port or expose
it on a network.

The **Data** tab shows one row per source item. The columns come from the
workflow itself: a company-sourcing run might show domains, qualification,
LinkedIn, email, and sheet status; another workflow might show entirely
different fields. Use **Filter columns** to combine text, category, and number
filters while you inspect a large run.

<p align="center">
  <img alt="Observer data table with stable business rows" src="assets/dashboard-data.png" width="960" />
</p>

<p align="center"><em>The Data view keeps source rows stable while fields and outcomes change.</em></p>

For an Observer Flow run, the **Flow** tab shows the live dependency graph,
which node is running, row outcomes at each node, and the selected row's path.
Click a node to inspect its script, inputs, outputs, condition, and rows. The
Data table remains the business view and updates the same stable rows as fields
land from successive nodes.

<p align="center">
  <img alt="Observer Flow batch node inspector" src="assets/dashboard-batch-details.png" width="960" />
</p>

<p align="center"><em>Select a node to inspect its contract, batch calls, and the rows that reached it.</em></p>

During the first sample, the agent can include a `response_json` cell containing
the decoded response body from the real source call. Click it to inspect the full
JSON, then tell the agent which fields should become normal columns for the run.

The **Timeline** is the plain-English history of the run. **Attention** focuses
on records whose explicit `error` field needs a look. **How it works** shows the
workflow's `EXPLAIN.md` statement of intent.

<p align="center">
  <img alt="Observer attention view showing records with explicit errors" src="assets/dashboard-attention.png" width="960" />
</p>

<p align="center"><em>Attention stays focused on declared failures instead of guessing from row text.</em></p>

### Talk To The Agent

- **Command-click** a table cell or column header to open a conversation about
  that exact spot. (`Ctrl-click` works on non-macOS systems.)
- Use **Message agent** in the run monitor to discuss the whole run, especially
  after it has paused or finished.
- The agent receives these notes through the run watcher, replies in the same
  thread, and can update the script or resume the run.

![Message the agent about the whole run](assets/dashboard-chat.png)

### Run Controls

During an active run, the monitor offers:

- **Pause**: requests a pause at the script's next checkpoint.
- **Stop after this record**: lets the current record finish, then pauses.
- **Approve full run**: appears after a dry-run sample and records your approval
  for the agent to start the intentional full-run command.

Clicking Pause or Stop sends the control request immediately and opens the
normal chat so you can explain what the agent should inspect. A green check
means the worker acknowledged the request. The dashboard does not kill a
process; the script pauses at a checkpoint where it has recorded its progress.

## Install

The repository ships one installable Python package, two agent skills, and a
CLI (including an **AXI** agent surface). **Product runtime** (runguard,
dashboard, chat watch, lint, flow validator, axi) lives in the `observer_kit`
package. The Observer Kit skill is the execution **playbook**; the Observer
Flow skill is the graph-design playbook. Neither skill tree is the canonical
home for product `.py` implementation.

Install the package/CLI first. During agent-led setup, the skills probe for the
CLI, install it in a writable Python environment when needed, and verify it
before initializing the project. Skill trees ship playbook markdown only (no
product runtime Python); use `observer-kit validate-flow` for graph structure
checks.

The CLI keeps the established `observer-kit` command so existing projects and
install instructions continue to work.

Use the skills as the source of truth for operator **behavior**: how the agent
selects a sample, interprets dashboard controls, fixes a run, and asks for
full-run approval. Use the package + CLI as the product runtime and repeatable
plumbing for the same contract. See the
[install matrix](docs/install-matrix.md) for the supported paths and
compatibility expectations.

Install the repository's skills for all local projects:

```bash
npx skills add edsmkt/observer-kit -g
```

Or add them only to the current project:

```bash
npx skills add edsmkt/observer-kit
```

Install the CLI directly from GitHub into a writable Python environment:

```bash
python3 -m pip install git+https://github.com/edsmkt/observer-kit.git
observer-kit --help
```

For development, the CLI also runs from this checkout:

```bash
python3 -m pip install -e .
python3 -m observer_kit --help
observer-kit --help
```

## Start A Project

With the CLI, run these commands from the project containing your workflow
script or flow coordinator:

```bash
observer-kit init .
observer-kit dashboard .observer --port 8484
```

`init` prepares a private `.observer` state directory (with `runs/` for
per-lane ledgers) and an `EXPLAIN.md` template. It does **not** copy product
runtime into the project; workflows import the observed-run API from the
installed package. Keep the dashboard running while the agent works.

```python
from observer_kit.runguard import start_observed_run
```

Package install is required for runtime. Create `.observer` (including `runs/`)
with `observer-kit init`, seed `EXPLAIN.md`, and launch dashboard/watch through
the CLI. Workflows import `observer_kit.runguard` rather than vendoring copies.

Then ask your agent to wire Observer Kit into the real script. With the CLI, a
typical first run is:

```bash
observer-kit run --state-dir .observer --dashboard -- \
  python3 enrich_companies.py --dry-run --limit 10
```

After you review the sample and explicitly approve it:

```bash
observer-kit run --state-dir .observer -- \
  python3 enrich_companies.py --full-run
```

`observer-kit run` attaches to an existing dashboard, starts the command, and
creates or reuses one watcher for that run. Separate run IDs keep independent
watchers. For one long-lived project session, an all-run watcher can cover the
project and subsequent runs reuse it. Check ownership with:

```bash
observer-kit watch .observer --status
```

The watcher remains transport; your agent makes decisions and requests full-run approval.

The ledger is append-only JSONL by design. For very long backfills, split work
into bounded runs or chunks with stable source identities, persist authoritative
results in a durable store, and keep raw provider responses to samples or debug
cases. The dashboard is for live review and audit, not a replacement for the
workflow's durable destination.

## A Simple Script

For a new Python workflow, the helper keeps the integration small:

```python
from observer_kit.runguard import start_observed_run

run = start_observed_run(
    'enrich-leads',
    source=args.input,
    dry_run=args.dry_run,
    todo=len(leads),
    progress_table='companies',
    summary_metrics=[
        {'key': 'processed', 'label': 'processed'},
        {'key': 'planned', 'label': 'planned'},
        {'key': 'written', 'label': 'written'},
    ],
)

try:
    for lead in leads:
        run.check_controls()
        with run.step('enrich_lead', table='companies', key=lead.id,
                      company=lead.domain):
            result = enrich_lead(lead)
            if not run.dry_run:
                update_crm_lead(lead.id, result)
            run.count('planned' if run.dry_run else 'written')
            run.count('processed')
            run.checkpoint('last_lead', lead.id)
    run.success(processed=len(leads))
except Exception as exc:
    run.fail(exc)
    raise
```

The important part is simple: keep two guarantees separate. Emit each row while
the real work happens so the dashboard stays live, and save the actual result to
a re-readable destination before moving on so a restart can continue from
durable progress.

## For Builders

Observer Kit provides practical run-management pieces when a workflow needs them:

- Source-based run locks and durable checkpoints for restarts.
- Append-only JSONL ledgers for live records, progress, and audit history.
- Shared provider throttling across local processes.
- Input snapshots, sample previews, validation, policy checks, and quality gates.
- Write intents and receipts for CRM, spreadsheet, database, file, webhook, and
  API delivery steps.
- Reconciliation and targeted replay candidates for incomplete work.

Use the workflow's real source identity for `source=`: a resolved file path,
Sheet or export ID, table plus query identity, or similar stable identifier.
That lets Observer Kit identify the same dataset across retries and show its
history in one run lane.

For implementation patterns and event vocabulary, see
[.claude/skills/observer-kit/references/pattern.md](.claude/skills/observer-kit/references/pattern.md).
The agent skill is at [.claude/skills/observer-kit/SKILL.md](.claude/skills/observer-kit/SKILL.md).

For dependency-driven workflows, see the
[Observer Flow skill](.claude/skills/observer-flow/SKILL.md) and its
[graph contract](.claude/skills/observer-flow/references/flow-contract.md).

## CLI Reference

```bash
observer-kit init [project]
observer-kit lint workflow.py
observer-kit dashboard [state_dir] --port 8484
observer-kit dashboard .observer --parent-pid $$          # exit when this shell dies
observer-kit dashboard .observer --idle-timeout 1800      # exit after 30m idle
observer-kit run --state-dir .observer -- python3 workflow.py --dry-run --limit 10
observer-kit watch .observer --run runguard:my-run --follow
observer-kit reply .observer --run runguard:my-run --anchor run --text "I fixed this."
observer-kit ps .observer                                 # list dashboards/watchers
observer-kit stop --orphans                               # dead-parent processes
observer-kit stop --sweep .observer                       # end-of-session cleanup
observer-kit validate-flow pipeline.flow.json             # Flow graph structure
observer-kit --version                                    # package + git sha
observer-kit axi help                                     # agent API catalog
observer-kit axi --state-dir .observer                    # agent home (TOON)
observer-kit axi runs --state-dir .observer
observer-kit axi run --state-dir .observer --id runguard:lane
observer-kit axi attention --state-dir .observer --id runguard:lane
observer-kit axi sample-status --state-dir .observer --id runguard:lane
observer-kit doctor [project]
observer-kit test
```

### Agent AXI

Observer ships an **[AXI](https://github.com/kunchenguid/axi)**-style agent
surface: token-efficient TOON on stdout, definitive empty states, structured
exit codes, and `help[]` next steps. It is for **agents operating the harness**,
not a replacement for the skill playbook or the human dashboard.

| Surface | Audience | Job |
| --- | --- | --- |
| **Skills** (Kit / Flow) | Agent judgment | When to sample, lock, approve, graph |
| **`observer-kit axi`** | Agent orientation | Runs, live?, orphans, doctor, next commands |
| **`observer-kit dashboard`** | Human review | Rows, flow graph, chat, controls |
| **Package API** | Workflow scripts | `start_observed_run`, intents, receipts |

```bash
# Canonical agent probe (detect PATH vs package skew)
observer-kit --version
observer-kit axi help
python3 -m observer_kit axi help

# Home: state, live runs, orphan count, next steps (TOON)
observer-kit axi --state-dir .observer

observer-kit axi runs --state-dir .observer
observer-kit axi run --state-dir .observer --id runguard:my-lane
observer-kit axi attention --state-dir .observer --id runguard:my-lane
observer-kit axi sample-status --state-dir .observer --id runguard:my-lane
observer-kit axi doctor .
observer-kit axi ps --state-dir .observer

# Human visual review (separate process)
observer-kit dashboard .observer

# Chat respond loop (still use poll for listening UI)
observer-kit poll .observer --all
```

Canonical install for agents: editable install in CI (`python3 -m pip install -e .`).
`doctor` / `axi home` emit `install_skew: true` plus the upgrade command when
PATH and package disagree. Full-run without approval exits **4**.
`observer-kit run` lint-gates workflow scripts by default (`--no-lint` to skip).

Tell agents in `AGENTS.md` / project instructions:

```text
Use observer-kit axi for status and next steps (TOON on stdout).
Use the Observer Kit skill for sample, locks, and full-run approval.
Use observer-kit dashboard for the human; do not scrape the HTML.
```

### Side-effect compliance gate

Observer ships a **compliance nudge** so agents do not quietly write or run
side-effect scripts outside the harness. It is **not a security boundary** and
must not be treated as one.

This repo wires Claude Code hooks:

1. **UserPromptSubmit** — if you say e.g. “no need to use observer kit”, injects
   a note so the agent stamps `# observer: ignore` on side-effect files.
2. **PreToolUse** — denies Write / Read / Bash on **side-effect** scripts
   (CRM/API writes, DB mutations, webhooks, metered loops, …) that are **not**
   under Observer Kit, unless the file has `# observer: ignore`.

CLI and hooks:

- Gate engine: `observer-kit gate path.py` or `observer-kit gate --command '…'`
- Hooks: [`.claude/hooks/observer-gate.sh`](.claude/hooks/observer-gate.sh),
  [`.claude/hooks/observer-opt-out.py`](.claude/hooks/observer-opt-out.py)
- Project settings: [`.claude/settings.json`](.claude/settings.json)

Prefer wiring `start_observed_run` and launching with
`observer-kit run --state-dir .observer -- …`. Use `# observer: ignore` only when
you intentionally opt out.

#### What it is (and is not)

| | |
| --- | --- |
| **Is** | A regex heuristic that steers agents toward the harness during Write / Bash |
| **Is not** | Access control, sandboxing, or a guarantee that all side effects are observed |
| **Allows** | Scripts that import `observer_kit` / call `start_observed_run`, or run via `observer-kit run`, or carry `# observer: ignore` |
| **Skips** | Tests, `setup.py`, anything under `observer_kit/`, non-`.py` paths |

#### Known false positives

The `write_sdk` / `orm_write` patterns match method names such as `.create(`,
`.update(`, `.insert(`, `.save(`. Innocent code can trip them:

- Dataclass or Pydantic factory helpers named `create`
- `dict.update(` / mapping helpers
- Local builders that only mutate in-memory objects

When that happens: wire the real side-effect path under Observer, or stamp
`# observer: ignore` with a short reason if the file truly has no external write.

#### Known bypasses (why it is not a security boundary)

Detection is intentionally shallow. It can be dodged by:

- Renaming helpers so call sites no longer match the regex
- Side effects in non-Python (shell, SQL clients, other languages)
- Dynamic import / `getattr` indirection that hides write APIs
- Running outside Claude Code hooks (or with hooks disabled)
- Opting out with `# observer: ignore` (honored by design)

Treat the gate as a **reminder and early friction**, not enforcement. Trust
comes from the harness itself: sample → dashboard review → explicit full-run
approval, plus locks, ledger, and AXI for live status.

Run the full acceptance suite from this repository with:

```bash
python3 -m observer_kit test
```

## License

MIT
