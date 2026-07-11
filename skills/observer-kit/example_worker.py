#!/usr/bin/env python3
"""Deterministic Observer Kit example for a generic data transformation.

Start the dashboard first, then run a reviewed sample and an intentional write:

  observer-kit dashboard .observer
  python3 example_worker.py --table alpha --dry-run --limit 2
  python3 example_worker.py --table alpha --full-run

Use ``--table beta`` for a disjoint source that can run concurrently. A second
``alpha`` process receives the source-lock warning. Every simulated provider
call shares one cross-process throttle, and every full-run row is appended and
flushed to disk before its dashboard checkpoint.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from runguard import (PendingWrite, RunPaused, input_snapshot, ledger,
                      operation_key, start_observed_run, throttle)


TABLES = {
    'alpha': [
        {'id': 'alpha-001', 'name': 'North', 'value': 12},
        {'id': 'alpha-002', 'name': 'South', 'value': 7},
        {'id': 'alpha-003', 'name': 'East', 'value': 19},
    ],
    'beta': [
        {'id': 'beta-001', 'name': 'West', 'value': 4},
        {'id': 'beta-002', 'name': 'Central', 'value': 15},
    ],
}
PROVIDER_RATE = 20


def transform(row: dict) -> dict:
    """Stand in for a deterministic provider-backed transformation."""
    throttle('observer-kit-example-provider', PROVIDER_RATE)
    score = row['value'] * 2
    return {
        'name': row['name'],
        'source_value': row['value'],
        'score': score,
        'segment': 'high' if score >= 20 else 'standard',
        'provider': 'example-provider',
    }


def read_delivered(path: Path) -> dict[str, dict]:
    delivered: dict[str, dict] = {}
    if path.is_file():
        with path.open(encoding='utf-8') as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    delivered[str(row['id'])] = row
    return delivered


def append_delivered(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--table', choices=sorted(TABLES), default='alpha')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--full-run', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--output', type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = TABLES[args.table]
    if args.limit is not None:
        rows = rows[:max(0, args.limit)]

    output = (args.output or Path('.observer') / f'example-{args.table}-output.jsonl').resolve()
    source = f'observer-kit-example:{args.table}'
    run = start_observed_run(
        'example-transform',
        source=source,
        input_snapshot=input_snapshot(source, records=TABLES[args.table]),
        destination=str(output),
        transform_version='v1',
        script=__file__,
        dry_run=args.dry_run,
        description=f'Transform fixture table {args.table}',
        todo=len(rows),
        progress_table='records',
        summary_metrics=[
            {'key': 'processed', 'label': 'processed'},
            {'key': 'delivered', 'label': 'delivered'},
            {'key': 'skipped', 'label': 'already delivered'},
        ],
        max_provider_calls=len(rows),
    )

    try:
        delivered = read_delivered(output)
        remaining = [row for row in rows if row['id'] not in delivered]
        run.preview(rows, estimates={
            'provider_calls': len(remaining),
            'writes': len(remaining),
        })

        for row in rows:
            run.check_controls()
            key = row['id']
            if key in delivered:
                existing = {field: value for field, value in delivered[key].items()
                            if field != 'id'}
                if run.dry_run:
                    ledger(run.scope, 'record', table='records', key=key,
                           **existing, destination='already_appended', status='skipped')
                else:
                    try:
                        ticket = run.write_intent(key, 'example-jsonl', payload=existing)
                    except PendingWrite:
                        ticket = {
                            'operation_key': operation_key(key, 'example-jsonl', 'v1'),
                            'record_key': key,
                            'destination': 'example-jsonl',
                            'transform_version': 'v1',
                        }
                    if ticket:
                        run.write_receipt(
                            ticket,
                            destination_id=key,
                            verified=True,
                            record_table='records',
                            outcome_field='destination',
                            outcome='already_appended',
                            record_fields=existing,
                            reconciled=True,
                        )
                    else:
                        ledger(run.scope, 'record', table='records', key=key,
                               **existing, destination='already_appended', status='skipped')
                run.count('skipped')
                run.checkpoint('last_record', key)
                run.check_controls(after_record=True)
                continue

            with run.step('transform', table='records', key=key,
                          label=row['name'], source_value=row['value']):
                result = transform(row)
                run.count('provider_calls')
                ledger(run.scope, 'credits', provider='example-provider',
                       used=run.counters['provider_calls'],
                       left=len(remaining) - run.counters['provider_calls'])
                ticket = run.write_intent(key, 'example-jsonl', payload=result)

                if run.dry_run:
                    ledger(run.scope, 'record', table='records', key=key,
                           **result, destination='planned', status='preview')
                elif ticket:
                    delivered_row = {'id': key, **result}
                    append_delivered(output, delivered_row)
                    run.write_receipt(
                        ticket,
                        destination_id=key,
                        verified=True,
                        record_table='records',
                        outcome_field='destination',
                        outcome='appended',
                        record_fields=result,
                    )
                    run.count('delivered')

                run.count('processed')
                run.checkpoint('last_record', key)
            run.check_controls(after_record=True)

        run.reconcile()
        run.success()
        return 0
    except RunPaused:
        # Deliberate pause already closed the run; do not mark it failed.
        raise
    except Exception as exc:
        run.fail(exc)
        raise


if __name__ == '__main__':
    raise SystemExit(main())
