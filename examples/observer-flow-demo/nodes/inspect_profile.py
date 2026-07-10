"""Simulate one bounded website/profile inspection call."""
from __future__ import annotations


LABELS = {
    "software": "subscription software",
    "services": "professional services",
    "marketplace": "transaction marketplace",
    "ambiguous": "mixed product and consulting language",
}


def run(row: dict) -> dict:
    profile = row["profile"]
    if profile == "invalid":
        raise ValueError("Synthetic profile response omitted its required category field")
    category = LABELS[profile]
    slug = row["domain"].split(".")[0]
    response = {
        "title": f"{row['company']} | {category.title()}",
        "description": f"{row['company']} describes {category} for modern operations teams.",
        "category": category,
        "social": {
            "linkedin": f"https://linkedin.example/company/{slug}",
            "x": f"https://x.example/{slug.replace('-', '')}",
        },
        "source": "synthetic profile API",
    }
    return {
        "fields": {
            "site_title": response["title"],
            "category_hint": category,
            "social_links": response["social"],
            "profile_response_json": response,
        },
        "evidence": {"provider": "synthetic_profile_api"},
        "spend_units": 1,
    }
