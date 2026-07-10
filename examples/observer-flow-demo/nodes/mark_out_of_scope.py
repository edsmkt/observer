"""Record the expected non-software route as a healthy outcome."""
from __future__ import annotations


def run(row: dict) -> dict:
    return {
        "fields": {
            "routing_status": "out of scope",
            "routing_reason": row["qualification_reason"],
        },
        "evidence": {"route": "non-software"},
        "spend_units": 0,
    }
