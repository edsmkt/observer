"""Route uncertain classifications into a visible review queue."""
from __future__ import annotations


def run(row: dict) -> dict:
    return {
        "fields": {
            "review_status": "queued",
            "review_reason": row["qualification_reason"],
        },
        "evidence": {"queue": "synthetic account review"},
        "spend_units": 0,
    }
