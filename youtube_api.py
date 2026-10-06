"""Official YouTube Data API v3 client: episode comments and channel uploads.

Needs an API key (Google Cloud console > enable "YouTube Data API v3" > Credentials > API key),
passed as the YOUTUBE_API_KEY environment variable. Reads cost 1 quota unit per call
(default allowance 10,000 units/day), so a weekly run uses a tiny fraction of it.

Policy note: YouTube's developer policies limit how long public comment data may be stored
(30 days). run_weekly.py keeps only derived data (tags, sentiment, likes) beyond that.
"""
import os
import requests
import pandas as pd

BASE = "https://www.googleapis.com/youtube/v3"
DEFAULT_MAX_COMMENTS = int(os.environ.get("MAX_COMMENTS", "5000"))
INCLUDE_REPLIES = os.environ.get("INCLUDE_REPLIES", "false").lower() == "true"


class ApiError(RuntimeError):
    def __init__(self, status, reason, message):
        super().__init__(f"YouTube API error {status} ({reason}): {message}")
        self.status, self.reason = status, reason


def _get(endpoint, params, api_key):
    r = requests.get(f"{BASE}/{endpoint}", params={**params, "key": api_key}, timeout=30)
    if r.status_code != 200:
        try:
            err = r.json()["error"]
            reason = (err.get("errors") or [{}])[0].get("reason", "")
            msg = err.get("message", "")
        except Exception:
            reason, msg = "", r.text[:200]
        raise ApiError(r.status_code, reason, msg)
    return r.json()


def _key(api_key):
    api_key = api_key or os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise SystemExit("Set YOUTUBE_API_KEY (Google Cloud console > Credentials > API key).")
    return api_key


def _row(video_id, item_id, sn, is_reply, reply_count=None):
    # Author names are deliberately not collected.
    return {"video_id": video_id, "video_url": f"https://www.youtube.com/watch?v={video_id}",
            "video_title": None, "comment": sn.get("textDisplay", ""), "likes": sn.get("likeCount", 0),
            "date": sn.get("publishedAt"), "replies": reply_count, "is_reply": is_reply, "comment_id": item_id}


def _replies(video_id, parent_id, api_key, cap):
    rows, token = [], None
    while len(rows) < cap:
        data = _get("comments", {"part": "snippet", "parentId": parent_id, "maxResults": 100,
                                 "textFormat": "plainText", **({"pageToken": token} if token else {})}, api_key)
        rows += [_row(video_id, c["id"], c["snippet"], True) for c in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            break
    return rows


def comments_for_video(video_id, api_key=None, max_comments=None, include_replies=None):
    api_key, cap = _key(api_key), max_comments or DEFAULT_MAX_COMMENTS
    include_replies = INCLUDE_REPLIES if include_replies is None else include_replies
    rows, token = [], None
    while len(rows) < cap:
        params = {"part": "snippet,replies" if include_replies else "snippet", "videoId": video_id,
                  "maxResults": 100, "order": "time", "textFormat": "plainText"}
        if token:
            params["pageToken"] = token
        try:
            data = _get("commentThreads", params, api_key)
        except ApiError as e:
            if e.reason in ("commentsDisabled", "videoNotFound"):
                print(f"  {video_id}: {e.reason}, skipping")
                return rows
            raise
        for it in data.get("items", []):
            top = it["snippet"]["topLevelComment"]
            n_rep = it["snippet"].get("totalReplyCount", 0)
            rows.append(_row(video_id, top["id"], top["snippet"], False, n_rep))
            if include_replies and n_rep:
                inline = it.get("replies", {}).get("comments", [])
                if n_rep > len(inline):
                    rows += _replies(video_id, top["id"], api_key, cap)
                else:
                    rows += [_row(video_id, c["id"], c["snippet"], True) for c in inline]
        token = data.get("nextPageToken")
        if not token:
            break
    return rows[:cap]


def fetch_comments(video_ids, api_key=None, max_comments=None, include_replies=None):
    """Same output columns as scrape_youtube.fetch_comments, so the pipeline can use either."""
    rows = []
    for vid in video_ids:
        got = comments_for_video(vid, api_key, max_comments, include_replies)
        print(f"  {vid}: fetched {len(got)} comments")
        rows += got
    return pd.DataFrame(rows)


def video_published_times(video_ids, api_key=None):
    """Return exact YouTube upload timestamps (UTC) keyed by video id.

    episodes.csv stores a calendar date for readability, but the first-7-days
    comparison should use the actual upload time whenever the official API is
    available. videos.list accepts up to 50 IDs per request.
    """
    api_key = _key(api_key)
    ids = list(dict.fromkeys(video_ids))
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        data = _get("videos", {"part": "snippet", "id": ",".join(chunk), "maxResults": 50}, api_key)
        for it in data.get("items", []):
            out[it["id"]] = it.get("snippet", {}).get("publishedAt")
    return out


def playlist_videos(playlist_id, api_key=None, max_items=50):
    """Videos in any playlist, newest-added first (1 quota unit per 50)."""
    api_key = _key(api_key)
    out, token = [], None
    while len(out) < max_items:
        params = {"part": "snippet,contentDetails", "playlistId": playlist_id,
                  "maxResults": min(50, max_items - len(out))}
        if token:
            params["pageToken"] = token
        data = _get("playlistItems", params, api_key)
        for it in data.get("items", []):
            if it["snippet"].get("title") in ("Private video", "Deleted video"):
                continue
            out.append({"video_id": it["contentDetails"]["videoId"], "title": it["snippet"].get("title", ""),
                        "published": (it["contentDetails"].get("videoPublishedAt")
                                      or it["snippet"]["publishedAt"])[:10]})
        token = data.get("nextPageToken")
        if not token:
            break
    return out[:max_items]


def channel_uploads(channel_id, api_key=None, max_items=50):
    """Latest uploads via the channel's uploads playlist (everything the channel posts)."""
    return playlist_videos("UU" + channel_id[2:], api_key, max_items)


def channel_playlists(channel_id, api_key=None, max_items=100):
    """The channel's public playlists with IDs (1 quota unit per 50)."""
    api_key = _key(api_key)
    out, token = [], None
    while len(out) < max_items:
        params = {"part": "snippet,contentDetails", "channelId": channel_id, "maxResults": 50}
        if token:
            params["pageToken"] = token
        data = _get("playlists", params, api_key)
        for it in data.get("items", []):
            out.append({"playlist_id": it["id"], "title": it["snippet"].get("title", ""),
                        "video_count": it.get("contentDetails", {}).get("itemCount", ""),
                        "url": f"https://www.youtube.com/playlist?list={it['id']}"})
        token = data.get("nextPageToken")
        if not token:
            break
    return out
