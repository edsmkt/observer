"""Simulate one individual homepage request per website."""
from __future__ import annotations


COPY = {
    "software": "Subscription software that automates team workflows with dashboards and integrations.",
    "ecommerce": "Browse our online catalog, add products to cart, and complete checkout with delivery.",
    "services": "Advisory and consulting projects delivered by an experienced client services team.",
    "media": "Independent reporting, daily stories, interviews, and analysis from our newsroom.",
    "nonprofit": "Support our community mission through donations, volunteering, and local programs.",
    "ambiguous": "Digital solutions and strategic services for organizations navigating change.",
}


def run(row: dict) -> dict:
    kind = row["fixture_kind"]
    if kind == "unreachable":
        raise RuntimeError("Synthetic homepage returned HTTP 503 after two attempts")
    slug = row["domain"].split(".")[0]
    response = {
        "url": f"https://{row['domain']}/",
        "status": 200,
        "title": row["company"],
        "text": COPY[kind],
        "links": {
            "linkedin": f"https://linkedin.example/company/{slug}",
            "contact": f"https://{row['domain']}/contact",
        },
    }
    return {
        "fields": {
            "scrape_status": "scraped",
            "homepage_title": response["title"],
            "homepage_text": response["text"],
            "homepage_links": response["links"],
            "homepage_response_json": response,
        },
        "evidence": {"provider": "synthetic_html_fetch", "call_shape": "single"},
        "spend_units": 1,
    }
