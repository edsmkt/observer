# BUILD GUIDE — recreate the run-guard + observer stack from scratch

Audience: an AI agent (or developer) in a fresh session, in ANY project, with no
prior context. Follow this top to bottom and you will produce: (1) a run-guard
module giving batch scripts exclusive locks, cross-process throttling, and audit
ledgers; (2) a localhost dashboard that renders those ledgers as a live
per-record table. Working reference implementations sit next to this file —
`runguard.py`, `run_dashboard.py`, `example_worker.py` — copy them when you can;
this guide exists for when you must rebuild or adapt.

Everything is Python 3.9+ stdlib. No pip installs. POSIX (macOS/Linux) for the
flock-based throttle.

---

## 0. The problem this solves (read before building)

Batch scripts that spend money (per-lookup API credits) or mutate shared state
(CRM records, database rows) have a failure class that testing rarely catches:

- **The double-runner.** Someone (or some agent) starts a second instance while
  the first is still alive — often *because* the first looks stuck. Both see
  the same "work remaining" in the database, both submit it, credits are spent
  twice or records are written twice — and it gets worse when someone then tries
  to "clean up" by hand.
- **The invisible run.** Nobody can tell what a running batch has done so far —
  what was found, what was spent, what was written — without grepping logs or
  querying the datastore mid-run.
- **The unbounded loop.** A retry or pagination bug turns a 500-credit job into
  a whole-balance job before anyone notices.

The cure is three primitives, all local, all boring:

| primitive | guarantees |
|---|---|
| per-scope PID lock | at most one live process per resource scope |
| cross-process throttle | N processes ≤ one shared requests/second budget |
| append-only run ledger | every attempt/outcome/cost on disk, tail-able live |

And four design rules the primitives depend on:

1. **No cleanup step, ever.** Write each result to the durable store the moment
   it lands (fill-only writes). Then a crash loses nothing and "recovery" is
   just re-running — the plan is recomputed from the store, done work is
   skipped. If your script needs manual cleanup after a crash, redesign it.
2. **Never re-buy.** Mark attempted-but-empty outcomes in the store (e.g. an
   `outcome` column set to `NO_DATA`) and exclude anything with an outcome from
   future plans. Found values count toward caps.
3. **Spend ceiling in code.** Compute the plan's worst-case cost up front
   (sum of remaining need) and refuse to exceed it, even if the loop is buggy.
4. **In-flight ≤ remaining need, per entity.** If the provider charges per
   result and you cap results per company at N, never have more than
   (N − already_have) of that company's lookups in flight at once. Then the
   worst case (everything in flight succeeds) equals the cap exactly.

---

## 1. Build `runguard.py`

One module, ~120 lines, three public functions. Full reference implementation
is in this folder — the load-bearing details you must not lose when rebuilding:

### 1a. `acquire_lock(scope: str)`

- State dir: `$RUNGUARD_STATE_DIR` env var, else `./.runguard` next to the
  module. **Every process that must coordinate has to resolve the same dir.**
- Lockfile per scope: `<state>/<scope>.lock` containing JSON
  `{"pid": ..., "started": "...", "scope": ...}`.
- Algorithm:
  1. If the module already holds this scope in this process → return
     (re-entrancy — critical when a library acquires on every mutating call).
  2. If the lockfile exists: parse it, `os.kill(pid, 0)` to probe liveness.
     - PID alive and not ours → **`SystemExit(1)`** with a message that names
       the PID, its start time, and says: *stop it deliberately
       (`kill <pid>`), never start a parallel run to "fix" a stuck one.*
     - PID dead / file unparsable → stale; fall through and take over. Print
       one line saying so. (This is why crash recovery needs no cleanup.)
  3. Write our own lockfile; remember it in a module-level dict; register an
     `atexit` handler that deletes it **only if the PID inside is still ours**
     (guards against deleting a successor's lock).
- Escape hatch: env `RUNGUARD_DISABLE=1` makes it a no-op — for deliberate,
  understood parallel use only.

**Scope naming:** one scope per *resource*, not per script. `crm-write`,
`sourcing`, `phone-enrich`. For parallel datasets, parameterize:
`acquire_lock(f'enrich-{table}')` — same table twice refuses, different tables
coexist. Only safe if the datasets share no records (see rule 4 above — the
in-flight invariant is per-process).

**Pitfall found in the original build:** don't name the first parameter `name`
— callers pass record fields like `name="Jane"` as kwargs to the ledger and it
collides. Use `scope`.

### 1b. `ledger(scope: str, event: str, **fields)`

- One JSONL file per (run, scope): `<state>/<YYYY-MM-DD-HHMMSS>-<scope>.jsonl`,
  path chosen on first call and cached in a module dict.
- Each call appends one line: `{"ts": ISO_SECONDS, "event": event, **fields}`.
  Use `json.dumps(..., ensure_ascii=False, default=str)` so odd values never
  crash a run just to log it.
- This file is BOTH the human audit trail and the dashboard's feed. Append-only,
  never rewritten.

### 1c. `throttle(resource: str, per_second: float)`

Cross-process rate limiter. One tiny file per resource holds the next free
time slot; `fcntl.flock` makes claiming a slot atomic:

```python
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    stored = float(contents or 0)
    grant = max(time.time(), stored)          # my slot
    truncate_and_write(fd, grant + 1.0/per_second)  # advance for the next caller
finally:
    fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
wait = grant - time.time()
if wait > 0: time.sleep(wait)                  # sleep OUTSIDE the flock
```

- Sleeping outside the flock is what lets N processes queue distinct future
  slots instead of serializing on the lock — that's the whole trick.
- Call it before EVERY request to the shared provider, with one resource
  string per provider ACCOUNT (rate limits are account-level).
- Verify after building (acceptance test below): two processes × 10 calls at
  5/s must complete in ~3.8s with no two timestamps closer than ~1/rate.

---

## 2. Instrument your worker scripts

Skeleton (see `example_worker.py` for a runnable version with a fake provider):

```python
from runguard import acquire_lock, ledger, throttle

acquire_lock(f'enrich-{table}')                        # 1. exclusivity

plans = build_plans_from_durable_store()               # done work already excluded
ceiling = sum(remaining_need(p) for p in plans)        # 2. worst-case spend
ledger(scope, 'run_started', companies=len(plans), worst_case_credits=ceiling)

while spent < ceiling:                                 # 3. ceiling enforced in code
    batch = take_at_most_remaining_need_per_company(plans)   # 4. in-flight ≤ need
    if not batch: break
    ledger(scope, 'bc_submitted', round=r, leads=len(batch),
           contacts=[{'id','company','name','tier'} per lead])
    for lead in batch:
        throttle('provider-account', RATE)             # cross-process rate cap
        result = provider_lookup(lead)
        if result:
            write_fill_only_to_durable_store(lead, result)   # crash-safe point
            ledger(scope, 'phone_found', company=..., name=..., tier=..., phone=result)
        else:
            mark_attempted_in_store(lead)              # never re-buy
            ledger(scope, 'phone_not_found', company=..., name=...)
    ledger(scope, 'bc_credits', credits_consumed=spent, credits_left=ceiling-spent)

ledger(scope, 'run_finished', **final_stats)
```

If a shared client library performs the writes (e.g. a CRM wrapper), acquire
the lock INSIDE the library's request function, gated on mutating HTTP methods
(`PATCH/PUT/DELETE`, and `POST` except search-style read endpoints). Then every
present and future script that imports the library inherits the guard with
zero per-script effort. Keep reads lock-free or read-only scripts will block
writers.

### Event vocabulary (the dashboard's contract)

Per-record events MUST carry `company` + `name` — that pair is the table's row
key. The dashboard gives first-class rendering to:

`run_started` (fields: companies/todo, worst_case_credits) · `run_finished`
(any stats) · `bc_submitted` (round, leads, contacts:[{id,company,name,tier}]
→ flips those rows to "searching…") · `bc_credits` (credits_consumed,
credits_left → counter chips) · `phone_found`/`phone_not_found` ·
`email_found`(email, source)/`email_not_found`. Unknown events still render as
timeline lines; extend `humanize()` in the dashboard for new first-class ones.

---

## 3. Build `run_dashboard.py`

Architecture: a single-file stdlib HTTP server + a single embedded HTML page.
No frameworks, no websockets — the page polls every 2s and the server tails
files by byte offset. Read-only by construction: it opens ledger files and
lockfiles, nothing else, so it can never affect a run.

### Server (Python)

- `SOURCES` dict at the top maps kind → directory. Two layouts supported:
  flat dirs of `*.jsonl` ledgers (runguard style), and per-run SUBDIRECTORIES
  containing `events.jsonl` (+ `api-calls.jsonl`) for projects that already
  have a run-log convention.
- `GET /` → the HTML page (embedded string).
- `GET /api/runs` → newest ~40 run files across all sources:
  `{id: "<kind>:<file>", label, kind, mtime, live}` where `live` = mtime within
  120s. Sort newest first.
- `GET /api/locks` → scan the source dirs for `*.lock`, parse JSON, probe the
  PID with `os.kill(pid, 0)` → `{scope, pid, started, alive}`.
- `GET /api/events?run=<id>&offsets=<json>` → **incremental tail**: client
  echoes back `{path: byte_offset}`; server seeks, reads up to 512KB of new
  bytes, JSON-parses complete lines, returns `{events, offsets}`. If the file
  shrank (rotation), reset the offset to 0. Sanitize the run id against a
  whitelist regex before joining paths (`[\w.\-:TZ]+`) — never let the client
  traverse directories.
- Bind `127.0.0.1` only.

### Page (embedded HTML/JS) — what made it "human-readable"

The first version printed raw key=value events; the user verdict was "hard to
read". The redesign that worked:

- **Default view = a TABLE, one row per (company, person).** Events fold into
  columns: Company · Person · Tier (map numeric tiers to labels) · Phone ·
  Email · CRM id. Cell states are colored pills: green pill with the found
  value; amber "not found"; grey "searching…" (set when a `bc_submitted` names
  the person, before any result); grey "—" for untouched.
- **A "Run info" tab** (NOT a card under the table) for this run's identity
  (name, description, ledger file path) and its run-level progress events
  (rounds, credit counters, start/finish). Keeping it off the table means a
  10k-row run never buries it.
- **A "How it works" tab** that renders an `EXPLAIN.md` (plain-English + ASCII
  diagram), fetched fresh from the state dir on every view. It is a statement of
  intent the operator reads to confirm the agent is doing the right thing — and
  stop the run (externally) if not. Never cache it: stale intent is worse than
  none. The agent should write this file BEFORE the first spend/mutation.
- **A Timeline tab** with one plain-English sentence per event
  (`humanize(event)` switch: "📞 Found phone for **Anna Adler** at acme.de:
  +49…"), color-coded ok/warn/err.
- **Technical noise hidden by default** behind a "show raw API calls"
  checkbox (with an "(N hidden)" count) — raw API calls render only when asked,
  EXCEPT failures (status ≥ 400) which always show in red.
- **Counter chips** across the top: found, no-result, CRM writes, associations,
  credits used/left (read straight from `bc_credits` events), errors.
- Left sidebar (collapsible, state persisted): "Who is writing right now"
  (locks panel, ● alive / ○ stale) and the run list with live markers, human
  names + descriptions, URL-hash deep links, and a filter box.
- Auto-scroll that disengages when the user scrolls up (compare scrollTop to
  scrollHeight on scroll events).
- Escape every user-data string into HTML (`&<>`), since names/titles come
  from external providers.

### Client polling loop

Keep `offsets` and an `all` events array in JS; each 2s tick fetches locks,
runs, and (if a run is selected) new events; append and re-render. Selecting a
run resets `offsets={}, all=[]`.

---

## 4. Acceptance tests (run all before calling it done)

1. **Lock refusal:** start a worker; start the same scope again → second exits
   code 1 printing the live PID. Different scope in parallel → runs.
2. **Re-entrancy:** same process acquires the same scope twice → no error.
3. **Stale takeover:** write a lockfile with a dead PID (e.g. 99999999) →
   next run prints "stale" and proceeds.
4. **Throttle:** two processes × 10 `throttle('x', 5)` calls each, print
   timestamps → combined span ≈ (20−1)/5 s; min gap between ANY two ≥ ~0.9/rate.
5. **Ledger→dashboard:** append events via `ledger()` while the dashboard is
   open → new run appears in the list marked live; rows appear in the table
   within ~2s; counters move.
6. **Worst-case spend:** simulate a provider that succeeds on EVERY lookup →
   total charged per company == its cap, run total == ceiling, never more.
7. **Read-only dashboard:** confirm the dashboard process opens files only
   (no writes anywhere in its code path).

## 5. Known pitfalls (each cost us a debug cycle — don't repeat)

- Parameter named `name` colliding with `name=` event fields → use `scope`.
- Sleeping INSIDE the flock serializes all processes → sleep outside.
- `atexit` deleting a lock the process no longer owns → verify PID before delete.
- Trusting client-supplied file paths in the tail endpoint → whitelist regex.
- Counting "submitted" as "spent" — many providers charge per RESULT; the
  ceiling should govern submissions but the ledger should record the
  provider's own consumed/remaining numbers as ground truth.
- Old Python + modern TLS: if `urllib` fails with TLSV1_ALERT on some API,
  shell out to `curl` for those calls (the reference worker does this).
- A run list that shows only your new ledgers: also supporting the project's
  EXISTING run-log directory layout makes the dashboard retroactively useful
  for all historic runs — cheap win, do it.
