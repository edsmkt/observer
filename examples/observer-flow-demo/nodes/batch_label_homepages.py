"""Simulate one discounted labeling request for several homepage texts."""
from __future__ import annotations


RULES = [
    ("software", ("subscription", "software", "automates"), 0.96),
    ("ecommerce", ("catalog", "cart", "checkout"), 0.95),
    ("services", ("advisory", "consulting", "projects"), 0.93),
    ("media", ("reporting", "stories", "newsroom"), 0.92),
    ("nonprofit", ("community", "donations", "volunteering"), 0.94),
]


def classify(text: str) -> tuple[str, float, str]:
    lowered = text.lower()
    for label, signals, confidence in RULES:
        matched = [signal for signal in signals if signal in lowered]
        if len(matched) >= 2:
            return label, confidence, f"Matched homepage signals: {', '.join(matched)}."
    return "needs_review", 0.51, "The homepage mixes categories and needs human review."


def run(rows: list[dict], *, batch_id: str) -> dict:
    request_id = f"synthetic-label-request-{batch_id}"
    batch_cost = 2.0
    member_costs = [round(batch_cost / max(1, len(rows)), 4) for _ in rows]
    if member_costs:
        member_costs[-1] = round(batch_cost - sum(member_costs[:-1]), 4)
    results = {}
    for row, member_cost in zip(rows, member_costs):
        if row["domain"] == "batch-malformed.test":
            results[row["domain"]] = {
                "status": "failed",
                "fields": {"label_batch_id": batch_id, "label_status": "response invalid"},
                "evidence": {"provider": "synthetic_batch_label_api", "request_id": request_id},
                "error": "Synthetic batch response omitted the required label for this member",
                "spend_units": member_cost,
            }
            continue
        label, confidence, reasoning = classify(row["homepage_text"])
        response = {
            "domain": row["domain"],
            "label": label,
            "confidence": confidence,
            "reasoning": reasoning,
        }
        results[row["domain"]] = {
            "status": "succeeded",
            "fields": {
                "label": label,
                "label_confidence": confidence,
                "label_reasoning": reasoning,
                "label_status": "labelled",
                "label_batch_id": batch_id,
                "label_response_json": response,
            },
            "evidence": {"provider": "synthetic_batch_label_api", "request_id": request_id},
            "error": "",
            "spend_units": member_cost,
        }
    return {
        "request_id": request_id,
        "spend_units": batch_cost,
        "individual_equivalent_units": float(len(rows)),
        "results": results,
    }
