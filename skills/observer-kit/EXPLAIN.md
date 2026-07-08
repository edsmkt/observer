# What this run does

A plain-English description of what this automated run will do — for anyone,
technical or not. Read it **before** the run spends money or changes records. If
any line here is wrong, stop the run (see "How to stop it") — nothing is lost.

> The agent that set up this pipeline writes this file. If it looks out of date,
> ask the agent to refresh it: it should match exactly what the run does today.
> (This is a TEMPLATE — replace the bracketed bits with your real pipeline.)

## In one sentence

<e.g. "For each company on the list, find up to 2 phone numbers and one email for
the best contacts, and save them — without ever paying for the same person twice.">

## The flow

```
   your work list  (CSV / database / spreadsheet)
         |
         v
   +-----------+   only ONE run at a time — a second run refuses to start,
   |   LOCK    |   so nothing is ever done twice or double-charged
   +-----------+
         |
         v
   for each record, best candidate first:
         |
         +--> provider 1 --+
         +--> provider 2 --+   (rate-limited; budget shared across runs)
         +--> provider 3 --+
         |
         v
   stop at the cap        e.g. at most 2 results per company (credits cost money)
         |
         v
   save to the store      fill-only: never overwrites what is already there
         |
         v
   this dashboard         shows every result the moment it lands
```

## What it WILL do

- <bullet: which records it processes and in what order>
- <bullet: which providers it asks, and for what>

## What it will NOT do

- Never overwrite an existing value — it only fills blanks.
- Never spend past the cap of **<N>** per company.
- Never run twice at once — the lock blocks a second start.
- <add any pipeline-specific guarantee>

## How to stop it

If something looks wrong, stop the run — press Ctrl-C in the terminal, or stop
the process ID shown in the **Run info** tab. There is no cleanup to do: results
already saved stay saved, and starting again simply resumes what is left.
