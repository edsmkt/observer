# Three-auditor panel + fix loop

When auditing a list of checks:

1. **One shared prompt per check** (scope, intent, deliberate decisions, non-goals).
2. **Three seats, concurrent:** buggie · no-mistakes · ponytail ultra. These are operator-local agent skills and may not be present in the published tree; see `auditor-loop/AUDIT_PANEL.md` for the full local protocol.
3. **Synthesize → fix** agreed majors (verify before/after).
4. **Auditor loop:** update `auditor-loop/PROMPT.md` (or residual brief), run `./auditor-loop/run.sh`, read `LATEST.md`, fix again if needed.
5. **Repeat triad + auditor-loop** on the **same** check until no major issues.
6. **Only then** start the next check. Never fan out (N × 3) in one blast.

```text
for each check:
  while majors remain:
    buggie + no-mistakes + ponytail ultra  (wait)
    fix
    ./auditor-loop/run.sh → LATEST.md → fix
  next check
```

Full protocol, prompt template, major vs residual:

- Local harness (often git-excluded): `auditor-loop/AUDIT_PANEL.md`
- Operator notes: `auditor-loop/HOW_GROK_USES_THIS.md`

**no-mistakes** needs a feature branch with real commits ahead of `main`. Same tip as main → skipped steps, not acceptance.
