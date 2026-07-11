"""Prepare a simulated destination row while performing zero external writes."""
from __future__ import annotations


def run(row: dict, *, dry_run: bool = False) -> dict:
    status = "planned" if dry_run else "simulated append"
    return {
        "fields": {
            "sheet_status": status,
            "sheet_row": row["index"] + 1,
            "destination_name": "Synthetic Review Sheet",
        },
        "evidence": {
            "destination": "synthetic-review-sheet",
            "confirmation": "" if dry_run else f"simulated-row-{row['index'] + 1}",
            "mode": "dry_run" if dry_run else "full_run_simulation",
        },
        "spend_units": 0,
    }
