#!/usr/bin/env python3
"""Cold-start contract tests for the Observer Kit agent skill."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


passed = failed = 0
HERE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent
SKILL = HERE / 'SKILL.md'
PATTERN = HERE / 'references' / 'pattern.md'
BUILD_GUIDE = HERE / 'references' / 'build-guide.md'
LINTER = HERE / 'references' / 'lint_emit.py'
EXPLAIN = HERE / 'EXPLAIN.md'


def ok(name: str, condition: bool, detail: str = '') -> None:
    global passed, failed
    print(f"  {'PASS' if condition else 'FAIL'} {name}" +
          (f" - {detail}" if detail and not condition else ''))
    if condition:
        passed += 1
    else:
        failed += 1


def prose(text: str) -> str:
    """Remove fenced code before checking natural-language steering."""
    lines: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith('```'):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines.append(line)
    return '\n'.join(lines)


print(f'Testing cold-start skill contract at {HERE}\n')

skill = SKILL.read_text(encoding='utf-8')
pattern = PATTERN.read_text(encoding='utf-8')
explain = EXPLAIN.read_text(encoding='utf-8')
metadata = (HERE / 'agents' / 'openai.yaml').read_text(encoding='utf-8')
skill_words = ' '.join(skill.split())
description_match = re.search(r'^description:\s*(.+)$', skill, re.MULTILINE)
description = description_match.group(1) if description_match else ''

steps = re.findall(r'^## ([1-7])\. ', skill, re.MULTILINE)
criteria = re.findall(r'^\*\*Complete when:\*\*', skill, re.MULTILINE)
ok('cold start has seven ordered steps', steps == list('1234567'), str(steps))
ok('every step has a checkable completion gate', len(criteria) == len(steps) == 7,
   f'steps={len(steps)} criteria={len(criteria)}')
ok('primary skill stays compact', len(skill.splitlines()) <= 250,
   f'{len(skill.splitlines())} lines')

ok('model description front-loads the harness leading word',
   description.startswith('Harness for '), description[:100])
ok('description covers workflow, operations, and core-maintenance branches',
   all(term in description for term in (
       'writing, adapting, or running', 'run controls', 'maintaining Observer Kit')),
   description)

required_paths = [PATTERN, BUILD_GUIDE, LINTER, EXPLAIN]
ok('every bundled context pointer resolves', all(path.is_file() for path in required_paths),
   ', '.join(str(path) for path in required_paths))
ok('README and production pattern have explicit purpose pointers',
   '../../README.md' in skill and
   '[`references/pattern.md`](references/pattern.md)' in skill and
   'read' in skill[skill.index('references/pattern.md') - 120:
                   skill.index('references/pattern.md') + 160].lower() and
   'single source of truth' in skill_words)

negative_steering = re.compile(
    r"\b(?:do not|don't|never|avoid|without|unless|not)\b|anti-pattern",
    re.IGNORECASE,
)
skill_negations = negative_steering.findall(prose(skill))
pattern_negations = negative_steering.findall(prose(pattern))
explain_negations = negative_steering.findall(prose(explain))
ok('skill uses positive steering', not skill_negations, str(skill_negations))
ok('production reference uses positive steering', not pattern_negations,
   str(pattern_negations[:10]))
ok('operator explainer template uses positive steering', not explain_negations,
   str(explain_negations[:10]))

legacy_terms = ('Per company', 'phone_found', 'email_found', 'bc_submitted',
                'edit the SOURCES', 'company + name')
ok('production reference stays domain-generic',
   all(term not in pattern for term in legacy_terms),
   ', '.join(term for term in legacy_terms if term in pattern))
ok('production reference co-locates the critical runtime contracts',
   all(heading in pattern for heading in (
       '## Source Identity And Run Lanes', '## Durable Boundaries And Resume',
       '## External Delivery', '## Controls, Chat, And Watchers',
       '## Production Verification')))
ok('cold-start agents create new workflows or adapt unfamiliar scripts',
   all(term in skill_words for term in (
       'for new work, inspect the source and destination contracts first',
       'Create new logic and CLI or preserve existing ones while wiring these paths for optimum operator visibility',
       'A cold-start agent owns the initial proposal, then asks concise questions',
       'an observed field catalog with paths/types/examples',
       'observed field catalog with paths/types/examples, projected columns',
       'Emit stable business rows as source items and classifications become known',
       'each slow phase emits a record before its terminal event')) and
   all(term in ' '.join(pattern.split()) for term in (
       'unfamiliar existing scripts after tracing their actual CLI and work paths',
       'Treat Observer Kit examples as illustrations',
       'A cold-start agent produces the complete initial projection from mapped evidence')))
ok('operator questions refine evidence-backed defaults',
   all(term in skill_words for term in (
       'decisions, fields, response retention, metrics, attention rules, limits, and lane',
       'every unresolved operator choice has an answer')) and
   all(term in ' '.join(pattern.split()) for term in (
       'Ask two to five concise questions after presenting that recommendation',
       'What decision should the sample help the user make',
       'Which outcomes belong in Attention or should pause further work',
       "Record the user's answers")) and
   all(term in ' '.join(explain.split()) for term in (
       'Operator decision', 'Attention or pause rules')))
ok('field catalogs and retained responses support later columns',
   all(term in skill_words for term in (
       'Project retained per-key responses into same-key updates',
       'use a bounded re-read for fields absent from retained state')) and
   all(term in ' '.join(pattern.split()) for term in (
       'Build an observed field catalog from `schema_observed`',
       'representative value from the response samples',
       'Sample-only retention', 'Governed response retention',
       'Later columns re-project from that store with zero additional API spend',
       'append a `record` event with the same table and key')) and
   'Response retention' in ' '.join(explain.split()))

prove_match = re.search(r'^## 5\. Prove The Sample\n(.*?)(?=^## 6\.)',
                        skill, re.MULTILINE | re.DOTALL)
prove_section = prove_match.group(1) if prove_match else ''
prove_words = ' '.join(prove_section.split())
map_match = re.search(r'^## 2\. Map The Real Workflow\n(.*?)(?=^## 3\.)',
                      skill, re.MULTILINE | re.DOTALL)
map_section = map_match.group(1) if map_match else ''
map_words = ' '.join(map_section.split())
propose_match = re.search(r'^## 3\. Propose The Operator View\n(.*?)(?=^## 4\.)',
                          skill, re.MULTILINE | re.DOTALL)
propose_section = propose_match.group(1) if propose_match else ''
propose_words = ' '.join(propose_section.split())
wire_match = re.search(r'^## 4\. Wire The Harness\n(.*?)(?=^## 5\.)',
                       skill, re.MULTILINE | re.DOTALL)
wire_section = wire_match.group(1) if wire_match else ''
wire_words = ' '.join(wire_section.split())
pattern_words = ' '.join(pattern.split())
branch_ids = (
    'paid_provider', 'external_destination', 'long_running',
    'schema_policy_quality', 'iterative_comparison',
)
explain_words = ' '.join(explain.split())
ok('sample gate requires crash-resume proof beyond a green linter',
   'forced mid-sample failure resumes in the same lane from saved work' in prove_words and
   'linter exits zero' in prove_words and
   'direct evidence' in prove_words and
   'Treat its zero exit as one piece of evidence' in pattern_words and
   'confirm the real sink during the sample' in pattern_words)
ok('sample work and row-surface liveness are explicit contracts',
   'earliest query/page/batch' in wire_words and
   'Emit stable business rows as source items and classifications become known' in wire_words and
   'reserve phase rows for work that has no business key yet' in wire_words and
   'each slow phase emits a record before its terminal event' in prove_words and
   'sample limit bounds the earliest query, page, batch, or provider loop' in prove_words and
   'repeated progress loops have a stable record-row path' in pattern_words and
   'Phase rows cover work before a business key exists and then yield to the business table' in pattern_words and
   'Sample work limit' in explain_words)
ok('bounded source discovery drives a reviewable table projection',
   'declared API/schema contract and observed response shape from bounded read calls' in map_words and
   'clickable `response_json`' in propose_words and
   'run.schema_sample()' in wire_words and
   'bounded schema sample opens as full JSON' in prove_words and
   'cumulative `schema_observed` path/type profile' in pattern_words and
   all(term in explain_words for term in (
       'Bounded schema read', 'Observed schema', 'Raw response field',
       'Projected columns')))
ok('response discovery triangulates code, declared schemas, and live probes',
   all(term in pattern_words for term in (
       "workflow's client code, tests, fixtures, cached responses",
       'OpenAPI', 'GraphQL introspection', 'CRM property metadata',
       'Execute bounded read-only probes through the exact production client and query shape',
       'Compare declared and observed envelopes, paths, types, nulls, optional fields',
       'Metered probes belong to the `paid_provider` branch')) and
   'Response evidence' in explain_words)
ok('material outcomes have rows and scalar headline totals',
   'scalar headline metrics covering the material outcomes' in propose_words and
   'stratified dry-run sample across planned, write, skip, hold, missing, and failure outcomes' in propose_words and
   'Emit stable business rows as source items and classifications become known' in wire_words and
   'summary_metrics` whose keys advance through `run.count()`' in wire_words and
   'scalar headline counts reconcile with stratified write, skip, hold, missing, and failure rows' in prove_words and
   'Advance each selected key with `run.count()` during work' in pattern_words and
   'maps to a scalar numeric field on the terminal event' in pattern_words and
   'Outcome coverage' in explain_words)
ok('canary visibility and watcher ownership are explicit contracts',
   'a canary row visibly moves through selected, writing, verifying, and verified or failed' in prove_words and
   'Watcher ownership refuses overlapping bridges' in skill_words and
   'Different run IDs may own independent watchers' in pattern_words and
   'Parent-owned watcher children exit with their CLI process' in pattern_words and
   'through `selected`, `writing`, `verifying`, and `verified` or `failed`' in pattern_words and
   'observer-kit watch .runguard --status' in pattern_words)
ok('sample verification separates universal proof from active branches',
   'verify this universal minimum' in prove_words and
   'Verify every selected branch' in prove_words and
   all(branch in prove_words for branch in (
       'Paid provider or metered API', 'External destination mutation',
       'Long-running supervised job', 'Schema, policy, or quality contract',
       'Iterative enrichment or comparison')) and
   all(branch in pattern_words for branch in (
       'Paid provider or metered API', 'External destination mutation',
       'Long-running supervised job', 'Schema, policy, or quality contract',
       'Iterative enrichment or comparison')) and
   'beyond the authoritative durable result store' in prove_words and
   'beyond the authoritative durable result store' in pattern_words and
   'every universal check and active branch has direct evidence' in prove_words and
   'Universal evidence for every workflow' in pattern_words and
   'Active-branch evidence' in pattern_words)
ok('workflow map selects the branch set consumed by sample verification',
   all(branch in map_words and branch in prove_words and branch in pattern_words
       for branch in branch_ids) and
   'trigger reasons in `EXPLAIN.md`' in map_words and
   'branch list recorded in Step 2 and `EXPLAIN.md`' in prove_words and
   'same selected set' in pattern_words and
   'every selected verification branch has a recorded trigger reason' in map_words)
ok('skill supports both CLI-helper and bundled-script launch paths',
   'observer-kit --help' in skill and
   'bundled-script path' in skill and
   '## Helper Availability And Launch Paths' in pattern and
   'python3 -m observer_kit --help' in pattern and
   'run_dashboard.py .runguard --port 8484' in pattern and
   'watch_chat.py <run-id> --state-dir .runguard --follow' in pattern)
ok('cold-start setup installs and verifies a missing CLI',
   'Establish a verified CLI command prefix before project setup' in skill and
   'install the CLI from the public repository into a writable Python environment' in skill_words and
   'then repeat the probes' in skill_words and
   'python3 -m pip install git+https://github.com/edsmkt/observer-kit.git' in pattern and
   'Repeat both probes and retain the exact successful prefix' in pattern_words and
   'Package-manager, network, or permission constraints lead to the bundled-script path' in pattern_words)
ok('operator explainer is generic and ready for branch selection',
   all(branch in explain_words for branch in branch_ids) and
   all(term in explain_words for term in (
       'Stable source identity', 'Stable record key', 'Durable result store',
       'Run lane', 'Resume boundary', 'Dashboard view')) and
   all(term not in explain_words for term in (
       'phone number', 'best-titled', 'provider 1', 'per company')))

short_match = re.search(r'short_description:\s*"([^"]+)"', metadata)
prompt_match = re.search(r'default_prompt:\s*"([^"]+)"', metadata)
short_description = short_match.group(1) if short_match else ''
default_prompt = prompt_match.group(1) if prompt_match else ''
ok('UI metadata matches the harness model',
   25 <= len(short_description) <= 64 and 'harness' in short_description.lower())
ok('default prompt invokes the skill for creation and adaptation',
   '$observer-kit' in default_prompt and 'create or adapt' in default_prompt)

with tempfile.TemporaryDirectory(prefix='observer-skill-example-') as tmp:
    root = Path(tmp)
    output = root / 'output.jsonl'
    env = os.environ.copy()
    env['RUNGUARD_STATE_DIR'] = str(root / 'state')
    base = [sys.executable, '-B', str(HERE / 'example_worker.py'),
            '--table', 'alpha', '--limit', '2', '--output', str(output)]

    dry = subprocess.run(base + ['--dry-run'], env=env, capture_output=True,
                         text=True, timeout=30)
    full = subprocess.run(base + ['--full-run'], env=env, capture_output=True,
                          text=True, timeout=30)
    resume = subprocess.run(base + ['--full-run'], env=env, capture_output=True,
                            text=True, timeout=30)
    rows = ([json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
            if output.is_file() else [])
    ok('bundled example proves dry-run, full-run, and idempotent resume',
       dry.returncode == full.returncode == resume.returncode == 0 and
       len(rows) == 2 and [row.get('id') for row in rows] == ['alpha-001', 'alpha-002'],
       (dry.stderr + full.stderr + resume.stderr)[-800:])

    third = {
        'id': 'alpha-003', 'name': 'East', 'source_value': 19,
        'score': 38, 'segment': 'high', 'provider': 'example-provider',
    }
    with output.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(third, sort_keys=True) + '\n')

    previous_state_dir = os.environ.get('RUNGUARD_STATE_DIR')
    os.environ['RUNGUARD_STATE_DIR'] = env['RUNGUARD_STATE_DIR']
    sys.path.insert(0, str(HERE))
    try:
        from runguard import start_observed_run
        pending_run = start_observed_run(
            'example-transform', source='observer-kit-example:alpha',
            destination=str(output), transform_version='v1')
        pending_run.write_intent('alpha-003', 'example-jsonl', payload={
            key: value for key, value in third.items() if key != 'id'})
        pending_run.fail('simulated crash after destination append')
    finally:
        sys.path.pop(0)
        if previous_state_dir is None:
            os.environ.pop('RUNGUARD_STATE_DIR', None)
        else:
            os.environ['RUNGUARD_STATE_DIR'] = previous_state_dir

    recover = subprocess.run(
        [sys.executable, '-B', str(HERE / 'example_worker.py'),
         '--table', 'alpha', '--limit', '3', '--output', str(output), '--full-run'],
        env=env, capture_output=True, text=True, timeout=30)
    recovered_rows = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
    ledgers = list((root / 'state').glob('*.jsonl'))
    ledger_text = '\n'.join(path.read_text(encoding='utf-8') for path in ledgers)
    ok('bundled example reconciles append-before-receipt recovery',
       recover.returncode == 0 and len(recovered_rows) == 3 and
       '"reconciled": true' in ledger_text,
       recover.stderr[-800:])

print(f'\n{passed} passed, {failed} failed')
sys.exit(1 if failed else 0)
