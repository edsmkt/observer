"""Classify inspected accounts and keep the reasoning visible."""
from __future__ import annotations


def run(row: dict) -> dict:
    category = row["category_hint"]
    if category == "subscription software":
        qualification, is_software, confidence = "qualified", True, 0.94
        reason = "The profile describes a recurring software product and a defined user workflow."
    elif category == "mixed product and consulting language":
        qualification, is_software, confidence = "review", None, 0.56
        reason = "The profile mixes product and service language, so a person should review it."
    else:
        qualification, is_software, confidence = "not_software", False, 0.88
        reason = "The profile centers on services or transactions rather than a software product."
    return {
        "fields": {
            "qualification": qualification,
            "is_software": is_software,
            "confidence": confidence,
            "qualification_reason": reason,
        },
        "evidence": {"rule_set": "synthetic-qualification-v1"},
        "spend_units": 0,
    }
