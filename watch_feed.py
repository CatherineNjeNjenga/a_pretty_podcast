"""Detect new Pretty Tough uploads from the channel's public RSS feed and open a GitHub issue
asking you to add them to episodes.csv (with a suggested row to paste).

Env (all optional):
    YOUTUBE_API_KEY  if set, uploads are read through the official API (otherwise the public RSS feed)
    CHANNEL_ID     default: Maria Sharapova's channel
    TITLE_FILTER   regex for episode titles, default "pretty tough" (case-insensitive)
    WATCH_FROM_DATE  only alert about uploads published on/after this date (YYYY-MM-DD)
    GITHUB_TOKEN, GITHUB_REPOSITORY   set automatically in GitHub Actions; without them this just prints

Usage:
    python watch_feed.py            # normal run
    python watch_feed.py --list     # print what the feed returns (use this to check TITLE_FILTER)
"""
import os
import re
import sys
import csv
import xml.etree.ElementTree as ET
import requests

CHANNEL_ID = os.environ.get("CHANNEL_ID") or "UCX8NFD2nhnXv6gYt0Cj6ogw"
TITLE_FILTER = re.compile(os.environ.get("TITLE_FILTER") or r"pretty tough", re.I)
WATCH_FROM = os.environ.get("WATCH_FROM_DATE") or ""   # YYYY-MM-DD: ignore older uploads
FEED = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
GUEST_RX = re.compile(r"\bwith\s+([A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){0,3})")


def fetch_feed():
    if os.environ.get("YOUTUBE_API_KEY"):
        import youtube_api
        return youtube_api.channel_uploads(CHANNEL_ID)
    return fetch_rss()


def fetch_rss():
    r = requests.get(FEED, timeout=30, headers={"User-Agent": "pretty-tough-watcher/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for e in root.findall("a:entry", NS):
        out.append({
            "video_id": e.findtext("yt:videoId", namespaces=NS),
            "title": e.findtext("a:title", namespaces=NS) or "",
            "published": (e.findtext("a:published", namespaces=NS) or "")[:10],
        })
    return out


def known_ids(path="episodes.csv"):
    try:
        with open(path, newline="") as f:
            return {m.group(1) for row in csv.DictReader(f)
                    if (m := re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", row.get("video_url", "")))}
    except FileNotFoundError:
        return set()


def draft_guest(title):
    m = GUEST_RX.search(title)
    return m.group(1).strip() if m else ""


def issue_body(v):
    guest = draft_guest(v["title"])
    aliases = "|".join(w.lower() for w in re.split(r"\s+", guest) if w) if guest else ""
    row = f"https://www.youtube.com/watch?v={v['video_id']},{v['published']},{guest},{aliases},"
    return (f"A new episode was detected: **{v['title']}** (published {v['published']}).\n\n"
            "Add this row to `episodes.csv`, fix the guest and aliases (include nicknames), and optionally set "
            "`guest_tier` (1 mega-famous, 2 well known, 3 niche) at the end of the row:\n\n"
            f"```\n{row}\n```\n\n"
            "The pipeline will process it automatically once it is 7 days old. Close this issue when done.\n"
            f"<!-- video:{v['video_id']} -->")


def gh_issue_exists(session, repo, vid):
    r = session.get(f"https://api.github.com/search/issues",
                    params={"q": f"repo:{repo} in:body video:{vid}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("total_count", 0) > 0


def main():
    feed = fetch_feed()
    if "--list" in sys.argv:
        for v in feed:
            flag = "MATCH" if TITLE_FILTER.search(v["title"]) else "skip "
            print(f"{flag} {v['published']} {v['video_id']} {v['title']}")
        return
    known = known_ids()
    new = [v for v in feed if TITLE_FILTER.search(v["title"]) and v["video_id"] not in known
           and v["published"] >= WATCH_FROM]
    print(f"{len(feed)} videos in feed, {len(new)} new episode(s) not in episodes.csv")
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    for v in new:
        if not (token and repo):
            print("\n[dry run, no GitHub credentials]\n", issue_body(v))
            continue
        s = requests.Session()
        s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        if gh_issue_exists(s, repo, v["video_id"]):
            print(f"  issue already open for {v['video_id']}")
            continue
        r = s.post(f"https://api.github.com/repos/{repo}/issues", timeout=30,
                   json={"title": f"New episode to add: {v['title']}", "body": issue_body(v)})
        r.raise_for_status()
        print(f"  opened issue for {v['video_id']}: {r.json()['html_url']}")


if __name__ == "__main__":
    main()
