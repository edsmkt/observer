# What this run does

This is the operator-facing statement of intent for one observed workflow. The
agent replaces every bracketed field before the sample run and refreshes this
file whenever the source, transform, destination, ceilings, or run lane changes.

## In one sentence

[Describe the records being read, how they change, and where the completed
results go.]

## Source and destination

- Source: [system, resolved path, sheet ID, table plus query, or export ID]
- Stable source identity: [fingerprint or canonical identifier]
- Stable record key: [field or composite key]
- Bounded schema read: [endpoint or query and maximum sample entities]
- Observed schema: [ledger `schema_observed` event and optional governed artifact]
- Raw response field: [`response_json` or a governed `payload_ref`]
- Durable result store: [path, table, or service that resume reads]
- External destination: [shared file, CRM, database, spreadsheet, webhook, API,
  or "durable result store only"]
- Run lane: [update the current view or open a separate comparison view]

## The flow

```text
source snapshot
      |
      v
source-derived lock -> representative dry-run sample -> user review
      |
      v
read -> transform or enrich -> persist durable result -> emit dashboard row
      |
      v
[optional external delivery -> confirmed receipt]
```

## Boundaries and ceilings

- Sample work limit: [earliest query, page, batch, or provider stop condition;
  usually 5 to 25 stratified records]
- Resume boundary: [one item or one bounded chunk]
- Spend ceiling: [amount and unit]
- Write ceiling: [maximum destination mutations]
- Rate ceiling: [provider or destination rate]
- Full-run approval: [pending or approved, with timestamp]

## Verification branches

Keep the selected branch IDs and replace each bracketed trigger reason. Remove
the remaining examples before operator review.

- `paid_provider`: [metered, credit, quota, or account-rate-limited call]
- `external_destination`: [delivery beyond the authoritative durable result
  store]
- `long_running`: [loop, pool, or page set suited to operator pause or stop]
- `schema_policy_quality`: [schema, policy, or quality threshold]
- `iterative_comparison`: [updates, retries, redos, or comparison lanes]

## Dashboard view

- Operator decision: [what the sample helps the user decide]
- Tables and stable keys: [table -> key]
- Projected columns: [fields chosen from the observed schema for the user's objective]
- Headline metrics: [three to five scalar counters covering material outcomes]
- Outcome coverage: [planned/write, skip, hold, missing, failure]
- Attention or pause rules: [conditions the user wants surfaced or paused]
- Progress source: [source table or item count]
- Attention signal: [the explicit `error` field and its meaning]

## Stop, resume, and review

Pause or stop reaches the next durable boundary. Starting the same lane again
reads the durable result store and selects the remaining work. Dashboard notes
travel through the watcher to the active agent session for inspection, script
changes, replies, and deliberate resume.
