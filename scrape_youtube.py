"""Step 1: Pull YouTube comments for Pretty Tough episodes with Apify.

Usage:
    export APIFY_TOKEN=your_token
    python scrape_youtube.py

Fill in CONFIG first. In the Apify Store, look for a YouTube comments scraper
(for example "streamers/youtube-comments-scraper") and check its Input tab for
the exact field names. Videos: paste the episode URLs (or the channel's
videos tab URL into a YouTube scraper actor to list them first).
"""
import os
import json
import pandas as pd
from apify_client import ApifyClient

CONFIG = {
    "actor_id": "REPLACE_WITH_USERNAME/YOUTUBE_COMMENTS_ACTOR",
    "video_urls": [
        # "https://www.youtube.com/watch?v=XXXXXXXXXXX",
    ],
    "max_comments_per_video": 2000,
    "csv_output": "comments.csv",
}

# Actors differ in field names, so try the common ones for each column.
FIELD_CANDIDATES = {
    "video_id": ["videoId", "video_id", "id_video"],
    "video_url": ["pageUrl", "videoUrl", "url"],
    "video_title": ["title", "videoTitle"],
    "comment": ["comment", "text", "content", "commentText"],
    "likes": ["voteCount", "likes", "likeCount", "votes"],
    "date": ["date", "publishedAt", "publishedTimeText", "timestamp"],
    "replies": ["replyCount", "replies", "totalReplies"],
    "is_reply": ["replyToCid", "isReply", "parentId"],
    "comment_id": ["cid", "commentId", "comment_id", "id"],
}


def pick(item, names):
    for n in names:
        if n in item and item[n] not in (None, ""):
            return item[n]
    return None


def fetch_comments(video_urls, actor_id=None, max_comments=None, token=None):
    """Run the Apify actor for the given episode URLs and return a normalized DataFrame."""
    token = token or os.environ.get("APIFY_TOKEN")
    actor_id = actor_id or os.environ.get("APIFY_ACTOR_ID") or CONFIG["actor_id"]
    if not token:
        raise SystemExit("Set APIFY_TOKEN first (Apify Console > Settings > Integrations).")
    if "REPLACE_WITH" in actor_id:
        raise SystemExit("Set APIFY_ACTOR_ID (or CONFIG['actor_id']) to your YouTube comments actor.")
    client = ApifyClient(token)
    run = client.actor(actor_id).call(run_input={
        "startUrls": [{"url": u} for u in video_urls],
        "maxComments": max_comments or CONFIG["max_comments_per_video"],
    })
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    df = pd.DataFrame([{k: pick(it, v) for k, v in FIELD_CANDIDATES.items()} for it in items])
    if df.empty:
        return df
    df["is_reply"] = df["is_reply"].notna() & (df["is_reply"] != False)  # noqa: E712
    return df


def main():
    if not CONFIG["video_urls"]:
        raise SystemExit("Add episode URLs to CONFIG['video_urls'].")
    df = fetch_comments(CONFIG["video_urls"])
    df.to_csv(CONFIG["csv_output"], index=False)
    print(f"Saved {len(df)} comments across {df['video_id'].nunique()} videos -> {CONFIG['csv_output']}")
    print("Fields found:", {c: int(df[c].notna().sum()) for c in df.columns})

    # Draft the guest sheet from titles; you must review it by hand.
    vids = df.drop_duplicates("video_id")[["video_id", "video_title"]].copy()
    vids["guest"] = ""          # e.g. "Serena Williams"
    vids["guest_aliases"] = ""  # pipe-separated, e.g. "serena|williams|rena"
    vids["guest_tier"] = ""     # optional: 1 = mega-famous, 2 = well known, 3 = niche
    vids.to_csv("guests.csv", index=False)
    print("Wrote guests.csv. Fill in guest, guest_aliases (and optionally guest_tier) for every video.")


if __name__ == "__main__":
    main()
