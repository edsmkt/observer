#!/usr/bin/env python3
"""Example worker — the full pattern, including parallel tables + shared throttling.

Simulates a credit-spending enrichment run so you can watch it live in the
dashboard without any real provider:

  terminal 1:  python3 run_dashboard.py                 # open http://localhost:8484
  terminal 2:  python3 example_worker.py --table alpha  # watch rows fill in live
  terminal 3:  python3 example_worker.py --table beta   # runs IN PARALLEL (own scope)
  terminal 4:  python3 example_worker.py --table alpha  # → REFUSES (same table locked)

Both parallel runs call throttle('fake-api', 2) before every lookup, so together
they never exceed 2 requests/second against the shared "provider" — that's the
cross-process rate control. Replace `fake_provider_lookup` with your real API
call and keep everything else: the per-table lock, the shared throttle, the
ceiling, the in-flight ≤ need rule, the ledger events, fill-only writes.
"""
import argparse
import random
import time

from runguard import acquire_lock, ledger, throttle

PHONE_CAP = 2          # stop buying once a company has this many
API_RATE_PER_S = 2     # shared across ALL concurrent runs (cross-process)

# Pretend work lists: two disjoint "tables".
TABLES = {
    'alpha': {
        'acme-widgets.de': [('Anna Adler', 1), ('Ben Bauer', 2), ('Cem Celik', 5)],
        'baltic-tools.de': [('Dora Dreyer', 1), ('Emil Ernst', 3)],
    },
    'beta': {
        'coast-freight.de': [('Fritz Falk', 5), ('Gina Groth', 1)],
        'delta-parts.de': [('Hans Huber', 2)],
    },
}


def fake_provider_lookup(name):
    """Stand-in for a paid API. throttle() makes ALL concurrent runs share one
    rate limit — the sleep happens across processes, first-come-first-served."""
    throttle('fake-api', API_RATE_PER_S)
    if random.random() < 0.6:
        return f'+49 1{random.randint(50, 79)} {random.randint(1000000, 9999999)}'
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--table', choices=sorted(TABLES), default='alpha')
    args = ap.parse_args()
    work = TABLES[args.table]
    scope = f'example-enrich-{args.table}'

    # 1. Exclusivity PER TABLE — the same table twice refuses; different tables
    #    run in parallel. Only safe because the tables share no records; the
    #    shared provider is protected by throttle(), not by this lock.
    acquire_lock(scope)

    # 2. Plan + hard ceiling: worst case = sum of remaining need, never more.
    plans = {co: {'found': 0, 'queue': list(people)} for co, people in work.items()}
    ceiling = sum(min(PHONE_CAP, len(p['queue'])) for p in plans.values())
    spent = 0
    ledger(scope, 'run_started', table=args.table, companies=len(plans),
           worst_case_credits=ceiling)

    # 3. Rounds: per company, never more in flight than its remaining need.
    rnd = 0
    while spent < ceiling:
        rnd += 1
        batch = []
        for co, p in plans.items():
            need = PHONE_CAP - p['found']
            for _ in range(min(need, len(p['queue']))):
                name, tier = p['queue'].pop(0)
                batch.append((co, name, tier))
        if not batch:
            break
        ledger(scope, 'bc_submitted', round=rnd, leads=len(batch),
               contacts=[{'id': n, 'company': c, 'name': n, 'tier': t} for c, n, t in batch])
        for co, name, tier in batch:
            phone = fake_provider_lookup(name)
            if phone:
                spent += 1  # provider charges on success only
                plans[co]['found'] += 1
                # In real life: fill-only write to your durable store HERE,
                # so a crash after this line loses nothing.
                ledger(scope, 'phone_found', company=co, name=name, tier=tier, phone=phone)
            else:
                ledger(scope, 'phone_not_found', company=co, name=name, tier=tier)
        ledger(scope, 'bc_credits', credits_consumed=spent, credits_left=ceiling - spent)

    ledger(scope, 'run_finished', table=args.table, credits_spent=spent,
           companies_at_cap=sum(1 for p in plans.values() if p['found'] >= PHONE_CAP))
    print(f'[{args.table}] done: spent {spent}/{ceiling} credits')


if __name__ == '__main__':
    main()
