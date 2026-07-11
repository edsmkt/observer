"""Prepare one simulated destination row after its batch label is durable."""
from __future__ import annotations


def run(row: dict, *, dry_run: bool = False) -> dict:
    status = "planned" if dry_run else "simulated append"
    return {
        "fields": {
            "export_status": status,
            "export_row": row["index"] + 1,
            "export_destination": "Synthetic Labelled Website Table",
        },
        "evidence": {
            "destination": "synthetic-labelled-websites",
            "confirmation": "" if dry_run else f"simulated-row-{row['index'] + 1}",
            "mode": "dry_run" if dry_run else "full_run_simulation",
        },
        "spend_units": 0,
    }
