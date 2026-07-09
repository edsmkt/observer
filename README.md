# observer-kit

Guardrails and a live localhost dashboard for any script that **spends API
credits** or **mutates shared state** (CRM, database, spreadsheets) — packaged
as an installable [agent skill](https://github.com/vercel-labs/skills).

It gives batch / enrichment / scraping scripts three things, all stdlib-only,
no dependencies:

- **Run locks** — a second accidental run refuses to start, so nothing
  double-spends or corrupts data. Crash-safe: recovery is "just re-run", never a
  manual cleanup.
- **An audit ledger + cross-process rate limiting** — every submission, result,
  and credit recorded; parallel runs share one rate budget per provider.
- **A boring default wrapper** — `start_observed_run(...)` gives new scripts a
  run id, lock, dry-run flag, visible step rows, counters, checkpoints, and
  `success()` / `fail()` lifecycle closure without inventing a run harness.
- **A read-only web dashboard** (`http://localhost:8484`) — a live per-record
  table, an at-a-glance activity strip, an Attention tab for failures/refusals,
  a plain-English timeline, a run-info tab, and a **"How it works"** tab that
  renders a plain-English + ASCII `EXPLAIN.md` so a non-technical operator can
  verify what a run is doing and stop it if it's wrong.

## What it looks like

**Data** — one row per item; columns are whatever the run logs, and pills fill in live as results land:

![Data view](assets/per-company.png)

**Timeline** — every step in plain English, newest work as it happens:

![Timeline view](assets/timeline.png)

**How it works** — a plain-English + ASCII "statement of intent" (from
`EXPLAIN.md`) the operator reads to confirm what a run will do *before* it spends:

![How it works view](assets/how-it-works.png)

**Collaborate on a sample** — click any column header or cell to chat with the
agent, anchored to that exact spot. The intended loop: the agent runs a small
sample, you leave notes on what to change, it replies and marks them ✓, and the
next run shows **before/after** inline (`· was X`) so you see what changed —
before committing the full (expensive) run.

![Inline chat anchored to a cell](assets/collaborate.png)

## Any workflow, any columns

The dashboard is **not** hardcoded to contacts/phones/emails. Log a generic
`record` event and the **Data** tab builds a table whose columns are exactly the
fields you logged — for *any* pipeline:

```python
# log where each row came FROM and went TO — lineage is just more columns
runguard.ledger('my-run', 'record', table='companies', key=domain,
                company=domain, source='northdata',        # pulled_from
                condition='met', supabase='inserted',       # per-sink outcomes
                hubspot='pushed', csv='written', status='done')
```

- **`table`** groups records into separate **sub-tabs** — a multi-step workflow
  emits a different shape at each step (e.g. `companies` → `contacts` → `enriched`),
  each its own table, so a later step's rows don't bury an earlier one's.
- **`key`** is the row identity — repeat it to update a row in place; a changed
  value renders `· was X` (before/after). Log the outcome onto the same key at each
  step and the row accumulates its whole path (source → each sink).
- Every other field becomes a **column**, in first-seen order. A destination
  (`supabase`, `hubspot`, `cloudflare`, `csv`, a webhook…) or a source is just a
  column — there's no fixed list.
- **Outcome columns auto-color**: values are classed green (`done/ok/inserted/pushed/
  written/created/appended`), amber (`skipped/not met/held/pending`), red (`fail/
  error/refused/4xx/5xx`), grey (`—`/not attempted) by a universal vocabulary — no
  per-workflow config. So "what landed where" reads straight off the colored cells.
- **Top bar stays lean**: per-table row counts (companies, people…), the run's own
  headline totals from `run_finished` (e.g. `emails_found`), and credits used — not
  a per-value dump.
- Table UX applies everywhere: first column frozen on horizontal scroll, sticky
  header on vertical scroll, drag a header edge to resize, double-click a cell to
  expand long values, click any cell/header to chat.

The bundled contact-enrichment view (phones/emails/CRM pills) is just the *example*
renderer that kicks in for `phone_found`/`email_found` events — remove it or ignore
it; `record` events are the general path.

## The boring wrapper

For new scripts, use the small helper instead of hand-assembling the lock,
ledger, dry-run, counters, and lifecycle every time:

```python
from runguard import start_observed_run

run = start_observed_run('enrich-leads', dry_run=args.dry_run, todo=len(leads))

try:
    for lead in leads:
        with run.step('enrich_lead', table='companies', key=lead.id,
                      company=lead.domain):
            enriched = enrich_lead(lead)
            if not run.dry_run:
                update_crm_lead(lead.id, enriched)
            run.count('leads_enriched')
            run.checkpoint('last_lead', lead.id)

    run.success(processed=len(leads))
except Exception as exc:
    run.fail(exc)
    raise
```

Drop to `acquire_lock()` + `ledger()` only when a pipeline needs custom event
vocabulary. The default path is meant to stay small enough to add in minutes.

## Install

Into your user scope (available in every project you open):

```bash
npx skills add edsmkt/observer-kit -g
```

Or into a single project's `./.claude/skills/`:

```bash
npx skills add edsmkt/observer-kit
```

Then, in any project, ask your agent to "wire in observer-kit" — or it will
reach for the skill on its own when it's about to write a credit-spending or
state-mutating batch script.

## Try it in 30 seconds

```bash
git clone https://github.com/edsmkt/observer-kit
cd observer-kit/skills/observer-kit
python3 test_runguard.py          # verify the safety core — 15 checks, all pass
python3 run_dashboard.py          # open http://localhost:8484, pick the sample run
python3 example_worker.py --table alpha   # watch a run fill the table live
python3 example_worker.py --table alpha   # a second copy REFUSES — the guard working
```

## What's inside `skills/observer-kit/`

| File | What it is |
|------|-----------|
| `SKILL.md` | Agent entry point — when to use it and how to wire it in |
| `runguard.py` | Locks + append-only ledger + cross-process throttle — **a library; vendor it into your project** (your script imports it) |
| `run_dashboard.py` | The localhost observer — **a standalone app; run ONE instance** pointed at any project's ledger dir (`python3 run_dashboard.py <dir>`), don't vendor it per-project |
| `watch_chat.py` | Run-scoped chat watcher — surfaces the operator's dashboard notes for **one** run so the right agent session gets them (multi-session safe). Wire into your harness's wake-up (Claude Code: the Monitor tool) |
| `observer_hook.py` | Claude Code PostToolUse hook — catches the `run_started` marker and reminds the agent to start that run's watcher. Add to `.claude/settings.json` on setup (see SKILL) |
| `EXPLAIN.md` | Template for the plain-English + ASCII "statement of intent" |
| `example_worker.py` | Runnable end-to-end example (parallel datasets + throttle) |
| `test_runguard.py` | Acceptance tests for the safety core (lock exclusivity, stale-lock takeover, re-entrancy, scope isolation, ledger append/continuity, cross-process throttle). Run it after vendoring `runguard.py` to prove the guards hold |
| `README.md` | The full pattern, event vocabulary, safety rules |
| `BUILD-GUIDE.md` | Rebuild the whole stack from scratch, with acceptance tests |

## License

MIT
