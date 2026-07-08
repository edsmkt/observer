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
- **A read-only web dashboard** (`http://localhost:8484`) — a live per-record
  table, a plain-English timeline, a run-info tab, and a **"How it works"** tab
  that renders a plain-English + ASCII `EXPLAIN.md` so a non-technical operator
  can verify what a run is doing and stop it if it's wrong.

## What it looks like

**Per company** — one row per item; pills fill in live as results land:

![Per company view](assets/per-company.png)

**Timeline** — every step in plain English, newest work as it happens:

![Timeline view](assets/timeline.png)

**How it works** — a plain-English + ASCII "statement of intent" (from
`EXPLAIN.md`) the operator reads to confirm what a run will do *before* it spends:

![How it works view](assets/how-it-works.png)

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
python3 run_dashboard.py          # open http://localhost:8484, pick the sample run
python3 example_worker.py --table alpha   # watch a run fill the table live
python3 example_worker.py --table alpha   # a second copy REFUSES — the guard working
```

## What's inside `skills/observer-kit/`

| File | What it is |
|------|-----------|
| `SKILL.md` | Agent entry point — when to use it and how to wire it in |
| `runguard.py` | Locks + append-only ledger + cross-process throttle |
| `run_dashboard.py` | The localhost observer (a sample — adapt to your events) |
| `EXPLAIN.md` | Template for the plain-English + ASCII "statement of intent" |
| `example_worker.py` | Runnable end-to-end example (parallel datasets + throttle) |
| `README.md` | The full pattern, event vocabulary, safety rules |
| `BUILD-GUIDE.md` | Rebuild the whole stack from scratch, with acceptance tests |

## License

MIT
