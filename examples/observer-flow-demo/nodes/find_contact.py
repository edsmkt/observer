"""Simulate conditional contact enrichment for qualified rows."""
from __future__ import annotations


FIRST_NAMES = ["Mara", "Jon", "Lena", "Ivo", "Nadia", "Theo", "Anika", "Sam"]


def run(row: dict) -> dict:
    if row["index"] == 13:
        raise RuntimeError("Synthetic contact provider timed out after two attempts")
    first = FIRST_NAMES[(row["index"] - 1) % len(FIRST_NAMES)]
    company_word = row["company"].split()[0]
    email = f"{first.lower()}@{row['domain']}"
    return {
        "fields": {
            "contact_name": f"{first} {company_word}",
            "contact_email": email,
            "contact_source": "synthetic contact API",
            "contact_confidence": round(0.78 + (row["index"] % 5) * 0.04, 2),
        },
        "evidence": {"provider": "synthetic_contact_api", "attempts": 1},
        "spend_units": 1,
    }
