"""Deterministic websites for the mixed single-call and batch-call demo."""
from __future__ import annotations


BASE_ROWS = [
    ("Alpine Desk", "alpine-desk.test", "DE", "software"),
    ("Parcel Bloom", "parcel-bloom.test", "GB", "ecommerce"),
    ("Cedar Counsel", "cedar-counsel.test", "US", "services"),
    ("Wave Journal", "wave-journal.test", "FR", "media"),
    ("Civic Lantern", "civic-lantern.test", "NL", "nonprofit"),
    ("Nimbus Stack", "nimbus-stack.test", "SE", "software"),
    ("Market Loom", "market-loom.test", "DE", "ecommerce"),
    ("Bright Bureau", "bright-bureau.test", "CA", "services"),
    ("Signal Post", "signal-post.test", "IE", "media"),
    ("Delta Cloud", "delta-cloud.test", "CH", "software"),
    ("Cart Meadow", "cart-meadow.test", "DK", "ecommerce"),
    ("Stone Advisory", "stone-advisory.test", "AU", "services"),
    ("Open Field", "open-field.test", "US", "nonprofit"),
    ("Metric Harbor", "metric-harbor.test", "DE", "software"),
    ("Willow Store", "willow-store.test", "GB", "ecommerce"),
    ("Northline Studio", "northline-studio.test", "FI", "services"),
    ("Batch Malformed", "batch-malformed.test", "AT", "software"),
    ("Quiet Press", "quiet-press.test", "NO", "media"),
    ("Vertex OS", "vertex-os.test", "NL", "software"),
    ("Amber Shop", "amber-shop.test", "US", "ecommerce"),
    ("Ledger Partners", "ledger-partners.test", "DE", "services"),
    ("Faint Signal", "faint-signal.test", "BE", "ambiguous"),
    ("Broken Homepage", "broken-homepage.test", "PL", "unreachable"),
    ("Mosaic Platform", "mosaic-platform.test", "ES", "software"),
]


def build_rows(limit: int = 24) -> list[dict]:
    rows = []
    for index, (company, domain, country, fixture_kind) in enumerate(BASE_ROWS[:limit], start=1):
        rows.append({
            "index": index,
            "company": company,
            "domain": domain,
            "country": country,
            "source": "synthetic website list",
            "fixture_kind": fixture_kind,
        })
    return rows
