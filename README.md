# Observer

**Watch your agent work — row by row, before it spends.**

An agent that enriches 5,000 records and writes them to your CRM works out of
sight, spends money, and is hard to reverse. Observer makes that run a
supervised session:

1. The agent wires the harness into the real script and runs a small sample.
2. You watch the real rows land on a localhost dashboard.
3. Click any cell to tell the agent what is wrong. It fixes the script, and the
   rerun updates the same rows in place.
4. When the sample is right, you approve the full run. Only then does the agent
   spend.

![Message the agent about a run or a specific cell](assets/dashboard-chat.png)

You also get guarantees, not just visibility:

- a second accidental run refuses to start, so nothing double-spends
- a crash resumes from its last checkpoint — recovery is always "just re-run"
- an append-only ledger records every submission and result, for live review
  and later audit
- a full run without your recorded approval exits with an error

Use it for any job that changes or moves many records: imports, exports,
enrichment, backfills, CRM updates, spreadsheet pushes. Dashboard columns are
whatever the workflow logs — nothing is hardcoded.

Stdlib-only runtime, zero dependencies (Python 3.9+, macOS / Linux, MIT).

## Why not just chain APIs?

Glued-together automations are brittle. One empty response, rate limit, or
changed field and the chain falls apart — fine for a personal demo, bad when a
client CRM is on the other end.

Observer does not promise pipelines never break. It promises **contained,
visible, recoverable failure**: errors land on specific rows in minutes (not at
month-end), the ledger shows what a bad run touched, checkpoints make re-runs
safe, and non-deterministic steps (LLM calls, flaky enrichment) go through the
sample → approve loop before full spend. Multi-step graphs declare inputs,
outputs, and conditions so `validate-flow` and a team can reason about every
step.

## Quick start

```bash
python3 -m pip install git+https://github.com/edsmkt/observer-kit.git
cd your-project
observer-kit init .
observer-kit run --state-dir .observer --dashboard -- \
  python3 enrich_companies.py --dry-run --limit 10
```

Review the sample at `http://localhost:8484`, leave notes, approve — then:

```bash
observer-kit run --state-dir .observer -- python3 enrich_companies.py --full-run
```

## The pieces

**Observer Kit** supervises each run. **Observer Flow** coordinates dependent
steps as a visible graph. Agents orient with the **AXI** CLI; humans review on
the dashboard.

| Piece | What it does for you |
| --- | --- |
| **Run locks** | Source-based locks and durable checkpoints — retries resume the same lane |
| **JSONL ledger** | Append-only events for live rows, progress, and audit |
| **Dashboard** | Localhost table, flow graph, cell-anchored chat, pause / stop / approve |
| **AXI** | `observer-kit axi` — dense TOON status for agents (live runs, orphans, `install_skew`, next commands) |
| **Compliance gate** | PreToolUse nudge that blocks side-effect scripts not under Observer (not a security boundary — [details](#side-effect-compliance-gate)) |
| **Observer Flow** | Dependency graph for multi-step pipelines under the same harness |

One script or a multi-node flow both go through the Kit harness. Agents use
`observer-kit axi` for status; you use the dashboard for rows, graph, chat, and
controls. After a fix, the same run continues and existing rows update in place.

```bash
# Multi-step flow under the same harness
observer-kit run --state-dir .observer -- \
  python3 flow_coordinator.py --flow pipeline.flow.json --dry-run --limit 10
```

Skills and contracts:
[Observer Kit](.claude/skills/observer-kit/SKILL.md) ·
[Observer Flow](.claude/skills/observer-flow/SKILL.md) ·
[flow contract](.claude/skills/observer-flow/references/flow-contract.md) ·
[pattern reference](.claude/skills/observer-kit/references/pattern.md) ·
[synthetic demos](examples/observer-flow-demo/README.md)

<p align="center">
  <img alt="Observer Flow dependency graph" src="assets/dashboard-flow.png" width="960" />
</p>

## Dashboard

Localhost only. It reads the JSONL ledger the workflow writes. Do not forward
the port or expose it on a network.

| Tab / control | Role |
| --- | --- |
| **Data** | One row per source item; columns come from the workflow. Filter text, category, and numbers. |
| **Flow** | Live dependency graph, node inspector, and the selected row's path. |
| **Timeline** | Plain-English history of the run. |
| **Attention** | Rows with an explicit `error` field. |
| **How it works** | The workflow's `EXPLAIN.md`. |
| **Chat** | Command-click (Ctrl-click) a cell or column, or **Message agent** for the whole run. |
| **Pause / Stop** | Request a checkpoint pause; does not kill the process. |
| **Approve full run** | After a dry-run sample; records approval for the intentional full-run command. |

During the first sample, a `response_json` cell can hold the decoded source
response — click it, then tell the agent which fields should become columns.

<p align="center">
  <img alt="Observer data table with stable business rows" src="assets/dashboard-data.png" width="960" />
</p>

<p align="center">
  <img alt="Observer Flow batch node inspector" src="assets/dashboard-batch-details.png" width="960" />
</p>

<p align="center">
  <img alt="Observer attention view showing records with explicit errors" src="assets/dashboard-attention.png" width="960" />
</p>

## Install

```bash
# CLI + package (required for runtime)
python3 -m pip install git+https://github.com/edsmkt/observer-kit.git

# Agent skills (global or project)
npx skills add edsmkt/observer-kit -g
# or: npx skills add edsmkt/observer-kit

# Development checkout
python3 -m pip install -e .
```

`observer-kit init .` creates `.observer/` (with `runs/`) and an `EXPLAIN.md`
template. Workflows import the package — nothing is vendored into the project:

```python
from observer_kit.runguard import start_observed_run
```

Ask the agent to wire the harness, then sample and full-run as in
[Quick start](#quick-start). `observer-kit run` attaches to a dashboard, starts
the command, and manages a watcher per run ID (`observer-kit watch .observer
--status`). The watcher is transport; the agent decides and requests approval.

For long backfills, split into bounded runs with stable source identities and
persist authoritative results outside the ledger. The dashboard is for live
review and audit, not the workflow's durable destination.

Package = runtime. Skills = operator playbook (sample, locks, approval). See
the [install matrix](docs/install-matrix.md) for paths and compatibility.

## A simple script

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

Two guarantees stay separate: emit each row while work happens (live dashboard),
and write the real result to a re-readable destination before moving on
(restart from durable progress). Use a stable `source=` (file path, Sheet ID,
table + query identity) so retries share one run lane.

When you need them, the harness also covers shared throttling, input snapshots,
validation and policy checks, quality gates, write intents/receipts, and
targeted replay candidates.

## CLI reference

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

**[AXI](https://github.com/kunchenguid/axi)**-style surface for agents: TOON on
stdout, empty states, structured exit codes, `help[]` next steps. Not a
replacement for the skill playbook or the human dashboard.

| Surface | Audience | Job |
| --- | --- | --- |
| **Skills** (Kit / Flow) | Agent judgment | When to sample, lock, approve, graph |
| **`observer-kit axi`** | Agent orientation | Runs, live?, orphans, doctor, next commands |
| **`observer-kit dashboard`** | Human review | Rows, flow graph, chat, controls |
| **Package API** | Workflow scripts | `start_observed_run`, intents, receipts |

```bash
observer-kit --version
observer-kit axi help
observer-kit axi --state-dir .observer
observer-kit axi runs --state-dir .observer
observer-kit axi run --state-dir .observer --id runguard:my-lane
observer-kit axi attention --state-dir .observer --id runguard:my-lane
observer-kit axi sample-status --state-dir .observer --id runguard:my-lane
observer-kit axi doctor .
observer-kit dashboard .observer
observer-kit poll .observer --all
```

`doctor` / `axi home` emit `install_skew: true` when PATH and package disagree.
Full-run without approval exits **4**. `observer-kit run` lint-gates by default
(`--no-lint` to skip).

In `AGENTS.md` / project instructions:

```text
Use observer-kit axi for status and next steps (TOON on stdout).
Use the Observer Kit skill for sample, locks, and full-run approval.
Use observer-kit dashboard for the human; do not scrape the HTML.
```

### Side-effect compliance gate

A **compliance nudge** so agents do not quietly write or run side-effect scripts
outside the harness. **Not a security boundary.**

Claude Code hooks in this repo:

1. **UserPromptSubmit** — phrases like “no need to use observer kit” inject a
   note so the agent stamps `# observer: ignore` on side-effect files.
2. **PreToolUse** — denies Write / Read / Bash on side-effect scripts that are
   not under Observer, unless the file has `# observer: ignore`.

CLI: `observer-kit gate path.py` or `observer-kit gate --command '…'`.  
Hooks: [`.claude/hooks/observer-gate.sh`](.claude/hooks/observer-gate.sh),
[`.claude/hooks/observer-opt-out.py`](.claude/hooks/observer-opt-out.py),
[`.claude/settings.json`](.claude/settings.json).

Prefer `start_observed_run` + `observer-kit run`. Use `# observer: ignore` only
for intentional opt-outs.

| | |
| --- | --- |
| **Is** | A regex heuristic that steers agents toward the harness during Write / Bash |
| **Is not** | Access control, sandboxing, or a guarantee that all side effects are observed |
| **Allows** | `observer_kit` / `start_observed_run`, `observer-kit run`, or `# observer: ignore` |
| **Skips** | Tests, `setup.py`, `observer_kit/`, non-`.py` paths |

**False positives:** `write_sdk` / `orm_write` match `.create(`, `.update(`,
`.insert(`, `.save(` — including dataclass factories, `dict.update(`, and
in-memory builders. Wire real side effects under Observer, or stamp
`# observer: ignore` with a short reason when there is no external write.

**Bypasses (why not security):** renamed helpers, non-Python side effects,
dynamic import / `getattr`, hooks disabled, or `# observer: ignore` (by design).
Treat the gate as early friction. Trust is the harness: sample → review →
full-run approval, plus locks, ledger, and AXI.

```bash
python3 -m observer_kit test
```

## License

MIT
