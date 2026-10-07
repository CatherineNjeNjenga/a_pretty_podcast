"""List the channel's recent uploads and playlists (with IDs) straight from the YouTube API.

Meant to run in GitHub Actions (workflow: list-episodes), so nothing has to be installed locally,
but it also works on your own machine:
    export YOUTUBE_API_KEY=...
    python list_channel.py [max_uploads]

Unlike watch_feed.py --list, nothing is filtered out here; a column shows whether each title matches
the current TITLE_FILTER. Playlist IDs (PL...) are printed so you can pick the Pretty Tough playlist.
Writes uploads.csv, playlists.csv and episodes_draft.csv, and a readable table to the Actions job summary.
"""
import os
import sys
import csv

import youtube_api
from watch_feed import CHANNEL_ID, PLAYLIST_ID, is_episode


def md(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("MAX_ITEMS") or 200)
    uploads = (youtube_api.playlist_videos(PLAYLIST_ID, max_items=n) if PLAYLIST_ID
               else youtube_api.channel_uploads(CHANNEL_ID, max_items=n))
    if PLAYLIST_ID:
        banner = f"Source: playlist {PLAYLIST_ID} ({len(uploads)} videos listed)"
    else:
        banner = ("Source: ALL channel uploads. PLAYLIST_ID is empty or not set, so videos that are not part of "
                  "the podcast are included. Set it under Settings > Secrets and variables > Actions > "
                  "Variables (not Secrets), and make sure the latest files are committed to the repo.")
    print(banner)
    playlists = youtube_api.channel_playlists(CHANNEL_ID)

    for u in uploads:
        u["url"] = f"https://www.youtube.com/watch?v={u['video_id']}"
        u["matches_filter"] = "yes" if is_episode(u) else "no"

    with open("uploads.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["published", "video_id", "title", "url", "matches_filter"])
        w.writeheader()
        for u in sorted(uploads, key=lambda x: x["published"], reverse=True):
            w.writerow({k: u[k] for k in w.fieldnames})
    # Draft in the exact shape episodes.csv needs. Guest, aliases and tier are left blank for you to fill in
    # (the extra title_for_reference column is ignored by the pipeline, so you can leave it in).
    with open("episodes_draft.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["video_url", "published", "guest", "guest_aliases", "guest_tier",
                                          "title_for_reference"])
        w.writeheader()
        for u in sorted(uploads, key=lambda x: x["published"]):
            if is_episode(u):
                w.writerow({"video_url": u["url"], "published": u["published"], "guest": "", "guest_aliases": "",
                            "guest_tier": "", "title_for_reference": u["title"]})
    with open("playlists.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["playlist_id", "title", "video_count", "url"])
        w.writeheader()
        w.writerows(playlists)

    print(f"Channel {CHANNEL_ID}: {len(uploads)} uploads, {len(playlists)} playlists\n")
    print("PLAYLISTS")
    for p in playlists:
        print(f"  {p['playlist_id']}  ({p['video_count']} videos)  {p['title']}")
    print("\nUPLOADS (newest first)")
    for u in sorted(uploads, key=lambda x: x["published"], reverse=True):
        print(f"  {u['published']}  {u['video_id']}  [{u['matches_filter']:>3}]  {u['title']}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"**{banner}**\n\n")
            f.write(f"## Playlists ({len(playlists)})\n\n| Playlist ID | Title | Videos |\n|---|---|---|\n")
            for p in playlists:
                f.write(f"| `{p['playlist_id']}` | {md(p['title'])} | {p['video_count']} |\n")
            f.write(f"\n## Uploads ({len(uploads)}, newest first)\n\n"
                    "| Published | Video ID | Title | Matches filter |\n|---|---|---|---|\n")
            for u in sorted(uploads, key=lambda x: x["published"], reverse=True):
                f.write(f"| {u['published']} | [`{u['video_id']}`]({u['url']}) | {md(u['title'])} | {u['matches_filter']} |\n")


if __name__ == "__main__":
    main()
