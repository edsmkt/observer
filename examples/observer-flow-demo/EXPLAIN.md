# Synthetic Account Routing

This is a local, synthetic Observer Flow demonstration. It performs zero real
API calls and zero external writes.

```text
Synthetic account rows
        |
        v
Inspect profile
        |
        v
Qualify account
   | qualified       | uncertain        | not software
   v                 v                  v
Find contact     Human review       Out of scope
   |
   v
Prepare a simulated sheet row
```

Each node is a separate Python module. The coordinator stores every terminal
node result in SQLite before appending the matching JSONL event. The dashboard
then shows both the evolving account table and the live dependency graph.

One synthetic profile response and one synthetic contact lookup intentionally
fail so the Attention and failure paths can be inspected.
