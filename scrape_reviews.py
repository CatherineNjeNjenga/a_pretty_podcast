"""Step 1: Scrape podcast reviews with Apify.

Usage:
    export APIFY_TOKEN=your_token_here
    python scrape_reviews.py

Edit CONFIG below. Find an Apple Podcasts reviews actor in the Apify Store
(search "apple podcasts reviews"), copy its ID (format: username/actor-name),
and check its Input tab for the exact field names its input expects.
"""
import os
import json
import pandas as pd
from apify_client import ApifyClient

CONFIG = {
    # Replace with the actor you choose in the Apify Store
    "actor_id": "REPLACE_WITH_USERNAME/ACTOR_NAME",
    # Pretty Tough: find the Apple Podcasts URL for the show and paste it here
    "run_input": {
        "startUrls": [{"url": "REPLACE_WITH_APPLE_PODCASTS_URL_FOR_PRETTY_TOUGH"}],
        "maxItems": 5000,
    },
    "raw_output": "raw_reviews.json",
    "csv_output": "reviews.csv",
}

# Actors name fields differently; map the likely candidates to a common schema.
FIELD_CANDIDATES = {
    "text": ["text", "review", "content", "body", "reviewText"],
    "title": ["title", "reviewTitle", "headline"],
    "date": ["date", "timestamp", "updated", "publishedAt", "createdAt", "reviewDate"],
    "votes": ["votes", "helpfulCount", "voteSum", "helpful", "likes", "voteCount"],
    "rating": ["rating", "stars", "score"],
}


def pick(item, names):
    for n in names:
        if n in item and item[n] not in (None, ""):
            return item[n]
    return None


def main():
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        raise SystemExit("Set APIFY_TOKEN first (Apify Console > Settings > Integrations).")

    client = ApifyClient(token)
    run = client.actor(CONFIG["actor_id"]).call(run_input=CONFIG["run_input"])
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

    with open(CONFIG["raw_output"], "w") as f:
        json.dump(items, f, indent=2, default=str)

    rows = []
    for it in items:
        rows.append({k: pick(it, v) for k, v in FIELD_CANDIDATES.items()})
    df = pd.DataFrame(rows)
    df.to_csv(CONFIG["csv_output"], index=False)
    print(f"Saved {len(df)} reviews -> {CONFIG['csv_output']}")
    print("Fields found:", {c: int(df[c].notna().sum()) for c in df.columns})


if __name__ == "__main__":
    main()
