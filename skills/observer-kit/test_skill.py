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

required_paths = [PATTERN, BUILD_GUIDE, LINTER]
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
ok('skill uses positive steering', not skill_negations, str(skill_negations))
ok('production reference uses positive steering', not pattern_negations,
   str(pattern_negations[:10]))

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

prove_match = re.search(r'^## 5\. Prove The Sample\n(.*?)(?=^## 6\.)',
                        skill, re.MULTILINE | re.DOTALL)
prove_section = prove_match.group(1) if prove_match else ''
prove_words = ' '.join(prove_section.split())
pattern_words = ' '.join(pattern.split())
ok('sample gate requires crash-resume proof beyond a green linter',
   'forced mid-sample failure resumes in the same lane from saved work' in prove_words and
   'linter exits zero' in prove_words and
   'direct evidence' in prove_words and
   'Treat its zero exit as one piece of evidence' in pattern_words and
   'confirm the real sink during the sample' in pattern_words)

short_match = re.search(r'short_description:\s*"([^"]+)"', metadata)
prompt_match = re.search(r'default_prompt:\s*"([^"]+)"', metadata)
short_description = short_match.group(1) if short_match else ''
default_prompt = prompt_match.group(1) if prompt_match else ''
ok('UI metadata matches the harness model',
   25 <= len(short_description) <= 64 and 'harness' in short_description.lower())
ok('default prompt invokes the skill explicitly', '$observer-kit' in default_prompt)

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
