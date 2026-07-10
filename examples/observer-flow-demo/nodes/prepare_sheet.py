"""Prepare a simulated destination row while performing zero external writes."""
from __future__ import annotations


def run(row: dict) -> dict:
    return {
        "fields": {
            "sheet_status": "simulated append",
            "sheet_row": row["index"] + 1,
            "destination_name": "Synthetic Review Sheet",
        },
        "evidence": {
            "destination": "synthetic-review-sheet",
            "confirmation": f"simulated-row-{row['index'] + 1}",
        },
        "spend_units": 0,
    }
