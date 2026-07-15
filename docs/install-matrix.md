# Observer Install Matrix

Observer has three distribution surfaces:

- **Installable package** (`observer_kit`): product runtime — runguard API,
  dashboard server, chat watch, lint. Import and CLI entry points live here.
- **Agent skills**: playbooks for how an agent designs, reviews, pauses, fixes,
  and resumes data work. Skill trees are not the canonical product implementation.
- **CLI** (`observer-kit` / `python -m observer_kit`): repeatable local plumbing
  that initializes a project, launches the dashboard, runs commands, watches
  dashboard messages, and writes the same JSONL ledger.

The skills are the source of truth for operator behavior. The package is the
source of truth for product runtime. The CLI makes that behavior repeatable.

## Supported Paths

| Path | Command | Use when | Expected behavior |
| --- | --- | --- | --- |
| Global skills | `npx skills add edsmkt/observer-kit -g` | You want Observer available to agents in every local project. | Installs the Observer Kit and Observer Flow playbooks. The agent still probes for the CLI before setup. |
| Project skills | `npx skills add edsmkt/observer-kit` | You want Observer available only in the current project. | Same playbooks, scoped to the project. Useful for teams that vendor skills with a repo. |
| CLI from GitHub | `python3 -m pip install git+https://github.com/edsmkt/observer-kit.git` | You want the normal `observer-kit` command without cloning this repo. | Provides package imports (`observer_kit.runguard`) and the full CLI surface including `axi`, `poll`, `lint`, `ps`, `stop`, `scaffold`, `gate`, `validate-flow`. Wheels ship runtime under the `observer_kit` package. |
| Editable checkout | `python3 -m pip install -e .` | You are developing Observer itself or testing local changes. **Preferred in CI.** | Provides the same CLI and package from this checkout. Canonical probe: `observer-kit axi help` then `python3 -m observer_kit axi help`. `observer-kit --version` prints package version + git sha. |

### PATH skew

If `observer-kit` on PATH is an older install while `python3 -m observer_kit`
loads a newer package, agents will conclude commands like `axi` / `poll` do not
exist. `observer-kit doctor` and `observer-kit axi home` emit
`install_skew: true` plus the exact upgrade command. Always document only
commands that exist on the published entry point of the same version.
| Deprecated `--vendor` init | `observer-kit init . --vendor` | Temporary bridge for workflows that still expect local `runguard.py`. | Copies package modules into the project; doctor warns. Prefer package import. Skill trees never ship product `.py`. |

## Compatibility Contract

All install paths should agree on these observable behaviors:

- The dashboard reads append-only JSONL events from the selected state
  directory.
- `record` events use `table` and `key` as the stable row identity, and later
  events for the same pair update the existing row.
- A run emits liveness evidence while work happens and persists durable progress
  before advancing past a completed item or bounded chunk.
- Controls are cooperative: Pause and Stop are acknowledged at script
  checkpoints; the dashboard does not kill the worker process.
- Watchers are transport only. They carry dashboard messages to the active
  agent session, while the agent decides how to inspect, fix, resume, or ask for
  full-run approval.
- Full runs require explicit operator approval after a bounded dry-run sample.

When changing runtime behavior, update package modules and skill playbooks in
the same change. When changing the playbooks, keep CLI examples and package
import paths aligned with the same contract.

## Source Of Truth

The canonical execution contract lives in:

- [`.claude/skills/observer-kit/SKILL.md`](../.claude/skills/observer-kit/SKILL.md)
- [`.claude/skills/observer-kit/references/pattern.md`](../.claude/skills/observer-kit/references/pattern.md)
- [`.claude/skills/observer-flow/SKILL.md`](../.claude/skills/observer-flow/SKILL.md)
- [`.claude/skills/observer-flow/references/flow-contract.md`](../.claude/skills/observer-flow/references/flow-contract.md)

The README is the product overview and quick start. This matrix documents the
supported distribution paths. Runtime code should stay small and defer
operator-facing decisions to the playbooks.

## Maintainer Browser Verification

Dashboard changes have a real-browser acceptance path while the shipped runtime
remains dependency-free:

```bash
python3 -m pip install -e ".[browser-test]"
python3 -m playwright install chromium
OBSERVER_REQUIRE_BROWSER_TEST=1 python3 -B -m observer_kit test
```

Without the environment flag, `observer-kit test` reports a clear skip when
Playwright or Chromium is unavailable. CI sets the flag so browser execution is
required for every pushed change and pull request.

## Ledger Size And Long Runs

The ledger is append-only JSONL by design. That keeps crashes recoverable and
makes live review simple, but it also means long backfills can create large
files.

Current guidance:

- Emit business-row updates at useful review boundaries, not every internal
  retry or polling tick.
- Persist authoritative results in a durable store and use the ledger as the
  live audit/review stream, not as the only database for a long-running job.
- For million-row backfills, split work into bounded chunks or lanes with
  stable source identities so each run remains inspectable.
- Keep raw provider responses only for bounded samples or explicit debug cases;
  project fields into normal columns for the full run.

Future runtime work may add paging, archive helpers, or compaction for completed
runs. Until then, design long jobs around bounded runs and durable external
state.

## Security Boundary

Observer is a visibility and review harness, not a sandbox. The CLI runs the
command passed after `--`, and the skills assume the active agent can edit and
resume that workflow. Keep the dashboard bound to `127.0.0.1`, avoid exposing
state directories publicly, and treat project scripts with the same trust level
as any other local automation that can spend credits or mutate data.
