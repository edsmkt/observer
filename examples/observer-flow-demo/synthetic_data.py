"""Deterministic source rows for the live Observer Flow demo."""
from __future__ import annotations


BASE_ROWS = [
    ("Northstar Systems", "northstar-systems.test", "DE", "software"),
    ("Lattice Harbor", "lattice-harbor.test", "GB", "software"),
    ("Copperline Studio", "copperline-studio.test", "US", "services"),
    ("Meridian Cloud", "meridian-cloud.test", "FR", "software"),
    ("Atlas Workshop", "atlas-workshop.test", "NL", "services"),
    ("Paper Kite", "paper-kite.test", "SE", "ambiguous"),
    ("Signal Foundry", "signal-foundry.test", "DE", "software"),
    ("Juniper Market", "juniper-market.test", "CA", "marketplace"),
    ("Orbit Ledger", "orbit-ledger.test", "IE", "software"),
    ("Morrow Advisory", "morrow-advisory.test", "CH", "services"),
    ("Kernel Street", "kernel-street.test", "DK", "software"),
    ("Open Acre", "open-acre.test", "AU", "ambiguous"),
    ("Bright Relay", "bright-relay.test", "US", "software"),
    ("Harbor Metric", "harbor-metric.test", "DE", "software"),
    ("Common Thread", "common-thread.test", "GB", "marketplace"),
    ("Cinder Labs", "cinder-labs.test", "FI", "software"),
    ("Drift Assembly", "drift-assembly.test", "AT", "services"),
    ("Field Note", "field-note.test", "NO", "ambiguous"),
    ("Quiet Current", "quiet-current.test", "NL", "software"),
    ("Beacon Supply", "beacon-supply.test", "US", "marketplace"),
    ("Summit Logic", "summit-logic.test", "DE", "software"),
    ("Elm Bureau", "elm-bureau.test", "BE", "services"),
    ("Vector Grove", "vector-grove.test", "PL", "software"),
    ("Broken Envelope", "broken-envelope.test", "GB", "invalid"),
    ("Woven Stack", "woven-stack.test", "ES", "software"),
    ("Slate House", "slate-house.test", "IT", "services"),
    ("Pilot River", "pilot-river.test", "US", "ambiguous"),
    ("Amber Circuit", "amber-circuit.test", "DE", "software"),
    ("Civic Loop", "civic-loop.test", "CA", "marketplace"),
    ("Mosaic Engine", "mosaic-engine.test", "FR", "software"),
]


def build_rows(limit: int = 30) -> list[dict]:
    rows = []
    for index, (company, domain, country, profile) in enumerate(BASE_ROWS[:limit], start=1):
        rows.append({
            "index": index,
            "company": company,
            "domain": domain,
            "country": country,
            "source": "synthetic account export",
            "profile": profile,
        })
    return rows
