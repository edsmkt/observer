# What this run does

A plain-English description of what this automated run will do — for anyone,
technical or not. Read it **before** the run spends money or changes records. If
any line here is wrong, stop the run (see "How to stop it") — nothing is lost.

> This is an EXAMPLE. The agent that sets up your pipeline rewrites this file to
> match what your run actually does, and refreshes it whenever the run changes.

## In one sentence

For each company that still needs contacts, find up to 2 phone numbers for the
best-titled people and save them — without ever paying for the same person twice.

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
   for each company, best candidate first:
         |
         +--> provider 1 --+
         +--> provider 2 --+   (rate-limited; budget shared across runs)
         +--> provider 3 --+
         |
         v
   stop at the cap        at most 2 phone numbers per company (credits cost money)
         |
         v
   save to the store      fill-only: never overwrites what is already there
         |
         v
   this dashboard         shows every result the moment it lands
```

## What it WILL do

- Try the best-titled contact at each company first, and stop once the cap is met.
- Charge a credit only when a phone number is actually found.
- Record every attempt and every credit spent in a local ledger you can audit.

## What it will NOT do

- Never overwrite a phone number that already exists — it only fills blanks.
- Never spend more than 2 credits per company.
- Never run twice at once — the lock blocks a second start.

## How to stop it

If something looks wrong, stop the run — press Ctrl-C in the terminal, or stop
the process ID shown in the **Run info** tab. There is no cleanup to do: results
already saved stay saved, and starting again simply resumes what is left.
