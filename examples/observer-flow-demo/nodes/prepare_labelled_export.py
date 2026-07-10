"""Prepare one simulated destination row after its batch label is durable."""
from __future__ import annotations


def run(row: dict) -> dict:
    return {
        "fields": {
            "export_status": "ready",
            "export_row": row["index"] + 1,
            "export_destination": "Synthetic Labelled Website Table",
        },
        "evidence": {
            "destination": "synthetic-labelled-websites",
            "confirmation": f"simulated-row-{row['index'] + 1}",
        },
        "spend_units": 0,
    }
