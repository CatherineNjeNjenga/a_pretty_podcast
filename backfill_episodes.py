"""Draft episodes.csv rows for the back catalogue, straight from the channel's uploads.

Usage:
    export YOUTUBE_API_KEY=your_key
    python backfill_episodes.py --from 2026-01-01 [--max 300] [--out episodes_draft.csv]

It lists the channel's uploads (YouTube API, ~1 quota unit per 50 videos), keeps titles matching
TITLE_FILTER (default "pretty tough"), skips videos already in episodes.csv, and writes a draft you
MUST review: fix guest names, add nicknames to guest_aliases, optionally set guest_tier, then append
the rows to episodes.csv. Check with `python watch_feed.py --list` that the filter matches your titles.

Older episodes can be added safely: the weekly pipeline fetches their available comments but retains
only comments posted within the first 7 days after each episode's YouTube upload, making back-catalogue
and newly matured episodes comparable.
"""
import re
import csv
import argparse

import youtube_api
from watch_feed import CHANNEL_ID, PLAYLIST_ID, draft_guest, is_episode, known_ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="", help="ignore uploads before this date (YYYY-MM-DD)")
    ap.add_argument("--max", type=int, default=300, help="how many recent uploads to scan")
    ap.add_argument("--out", default="episodes_draft.csv")
    args = ap.parse_args()

    uploads = (youtube_api.playlist_videos(PLAYLIST_ID, max_items=args.max) if PLAYLIST_ID
               else youtube_api.channel_uploads(CHANNEL_ID, max_items=args.max))
    known = known_ids()
    rows = []
    for v in sorted(uploads, key=lambda v: v["published"]):
        if not is_episode(v) or v["video_id"] in known or v["published"] < args.start:
            continue
        guest = draft_guest(v["title"])
        aliases = "|".join(w.lower() for w in re.split(r"\s+", guest) if w) if guest else ""
        rows.append({"video_url": f"https://www.youtube.com/watch?v={v['video_id']}", "published": v["published"],
                     "guest": guest, "guest_aliases": aliases, "guest_tier": "", "title_for_reference": v["title"]})
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["video_url", "published", "guest", "guest_aliases", "guest_tier",
                                          "title_for_reference"])
        w.writeheader()
        w.writerows(rows)
    blank = sum(1 for r in rows if not r["guest"])
    print(f"Scanned {len(uploads)} uploads, drafted {len(rows)} episode rows -> {args.out}")
    print(f"{blank} row(s) have no guessed guest. Delete the title_for_reference column before appending to episodes.csv.")


if __name__ == "__main__":
    main()
