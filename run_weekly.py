"""Weekly runner: ingest due episodes, store in Turso, recompute cumulative stats, draw charts.

Usage:
    python run_weekly.py [--force] [--today YYYY-MM-DD] [--out out]

Flow
  1. Load episodes.csv (one row per episode you add) into the episodes table.
  2. For each pending episode that is at least SNAPSHOT_DAYS old, fetch its comments ONCE
     (fixed snapshot so episodes are comparable), tag who each comment is about, score
     sentiment, store, mark done.
  3. Purge raw comment text older than PURGE_AFTER_DAYS (YouTube's developer policies cap
     storage of public comment data at 30 days). Tags, sentiment and likes are kept.
  4. Recompute the Maria-vs-guest statistics over ALL stored episodes, append a row to
     weekly_metrics, and write charts + CSVs (no comment text) to the output folder.
Exits quietly without charts when nothing new was ingested (unless --force).

COMMENT_SOURCE=youtube_api (default) uses the official API (needs YOUTUBE_API_KEY);
COMMENT_SOURCE=apify uses the Apify scraper in scrape_youtube.py instead.
"""
import os
import re
import argparse
import hashlib
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go

import store
import analyze_youtube as a

SNAPSHOT_DAYS = 7
PURGE_AFTER_DAYS = 28     # margin under the 30-day limit
EPISODES_CSV = "episodes.csv"
VID_RX = re.compile(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})")


def video_id(url):
    m = VID_RX.search(url)
    if not m:
        raise ValueError(f"Cannot find a YouTube video id in: {url}")
    return m.group(1)


def sync_episodes(db):
    df = pd.read_csv(EPISODES_CSV, dtype=str).fillna("")
    for need in ["video_url", "published", "guest", "guest_aliases"]:
        if need not in df.columns or (df[need].str.strip() == "").any():
            raise SystemExit(f"{EPISODES_CSV}: column '{need}' must be filled in for every episode.")
    rows = []
    for _, r in df.iterrows():
        tier = int(r["guest_tier"]) if str(r.get("guest_tier", "")).strip() else None
        rows.append((video_id(r["video_url"]), r["video_url"].strip(), r["published"].strip(),
                     r["guest"].strip(), r["guest_aliases"].strip().lower(), tier))
    # Upsert metadata but never touch status/n_comments/snapshot_at
    db.executemany(
        """INSERT INTO episodes (video_id, video_url, published, guest, guest_aliases, guest_tier)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(video_id) DO UPDATE SET video_url=excluded.video_url, published=excluded.published,
             guest=excluded.guest, guest_aliases=excluded.guest_aliases, guest_tier=excluded.guest_tier""",
        rows)
    print(f"Synced {len(rows)} episodes from {EPISODES_CSV}")


def due_episodes(db, today):
    cutoff = today - timedelta(days=SNAPSHOT_DAYS)
    pending = db.execute(
        "SELECT video_id, video_url, published, guest_aliases FROM episodes WHERE status='pending'")
    return [p for p in pending if datetime.strptime(p["published"], "%Y-%m-%d").date() <= cutoff]


def fetch(episodes):
    if os.environ.get("COMMENT_SOURCE", "youtube_api") == "apify":
        from scrape_youtube import fetch_comments
        return fetch_comments([e["video_url"] for e in episodes])
    import youtube_api
    return youtube_api.fetch_comments([e["video_id"] for e in episodes])


def ingest(db, episodes, today):
    if not episodes:
        return 0
    raw = fetch(episodes)
    done = 0
    for e in episodes:
        d = raw[raw["video_id"] == e["video_id"]].copy() if not raw.empty else raw
        if d.empty:
            print(f"  {e['video_id']}: no comments returned, leaving pending")
            continue
        d["comment"] = d["comment"].fillna("").astype(str)
        d = d[d["comment"].str.len() > 0]
        has_id = "comment_id" in d and d["comment_id"].notna().all()
        d["comment_key"] = d["comment_id"].astype(str) if has_id else d["comment"].apply(
            lambda t: hashlib.sha1((e["video_id"] + t).encode()).hexdigest())
        d = d.drop_duplicates("comment_key")
        d["guest_aliases"] = e["guest_aliases"]
        d = a.score(a.tag(d))            # tag + sentiment at ingest, so they survive the text purge
        d["likes"] = pd.to_numeric(d["likes"], errors="coerce").fillna(0).astype(int)
        rows = [(e["video_id"], r.comment_key, r.comment, int(r.likes), int(bool(r.is_reply)),
                 float(r.compound), r.about, today.isoformat()) for r in d.itertuples()]
        db.executemany("INSERT OR IGNORE INTO comments "
                       "(video_id, comment_key, text, likes, is_reply, compound, about, fetched_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", rows)
        db.execute("UPDATE episodes SET status='done', n_comments=?, snapshot_at=? WHERE video_id=?",
                   (len(rows), today.isoformat(), e["video_id"]))
        print(f"  {e['video_id']}: stored {len(rows)} comments")
        done += 1
    return done


def purge_old_text(db, today):
    cutoff = (today - timedelta(days=PURGE_AFTER_DAYS)).isoformat()
    n = db.execute("SELECT count(*) AS n FROM comments WHERE text IS NOT NULL AND fetched_at <= ?",
                   (cutoff,))[0]["n"]
    if n:
        db.execute("UPDATE comments SET text=NULL WHERE text IS NOT NULL AND fetched_at <= ?", (cutoff,))
    print(f"Purged raw text of {n} comment(s) older than {PURGE_AFTER_DAYS} days")


def load_all(db):
    c = pd.DataFrame(db.execute(
        """SELECT c.video_id, c.text AS comment, c.likes, c.is_reply, c.compound, c.about AS stored_about,
                  e.guest, e.guest_aliases, e.guest_tier, e.published
           FROM comments c JOIN episodes e USING (video_id) WHERE e.status='done'"""))
    if c.empty:
        return c
    c["guest_tier"] = pd.to_numeric(c["guest_tier"], errors="coerce")
    has_text = c["comment"].notna()
    c["about"] = c["stored_about"]
    if has_text.any():   # alias fixes re-tag comments whose text is still within the retention window
        c.loc[has_text, "about"] = a.tag(c[has_text].copy())["about"].values
    return c.drop(columns=["stored_about"])


def cumulative(e, min_k=3):
    r = e[e["reliable"]].sort_values("published")
    out = []
    for k in range(min_k, len(r) + 1):
        x = r["gap_all"].iloc[:k].values
        lo, hi = a.boot_ci(x, n=4000)
        out.append({"episodes": k, "last_guest": r["guest"].iloc[k - 1], "mean_gap": x.mean(), "lo": lo, "hi": hi})
    return pd.DataFrame(out)


def chart_cumulative(cum):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=cum["episodes"], y=cum["hi"], mode="lines", line=dict(width=0), showlegend=False))
    fig.add_trace(go.Scatter(x=cum["episodes"], y=cum["lo"], mode="lines", line=dict(width=0), fill="tonexty",
                             fillcolor="rgba(200,85,61,0.18)", name="95% interval"))
    fig.add_trace(go.Scatter(x=cum["episodes"], y=cum["mean_gap"], mode="lines+markers", name="Mean gap",
                             line=dict(color=a.C_MARIA, width=3)))
    fig.add_hline(y=0, line=dict(color="#999", dash="dash"))
    a.style(fig, "Maria minus guest: cumulative gap in share of comments, by episodes published", 1400, 850)
    fig.update_xaxes(title_text="Episodes included", dtick=1, gridcolor=a.GRID)
    fig.update_yaxes(title_text="Share naming Maria minus share naming guest", tickformat=".0%", gridcolor=a.GRID)
    return fig


def record_metrics(db, today, e):
    r = e[e["reliable"]]
    if len(r) < 3:
        print("Fewer than 3 reliable episodes so far: stats not yet meaningful.")
        return None
    x, xl = r["gap_all"].dropna(), r["gap_likes"].dropna()
    n, k, p = a.sign_test(x)
    lo, hi = a.boot_ci(x)
    db.execute("INSERT OR REPLACE INTO weekly_metrics VALUES (?,?,?,?,?,?,?,?)",
               (today.isoformat(), len(r), float(x.mean()), float(lo), float(hi), float(xl.mean()), float(p), k))
    return a.verdict(e)


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebuild charts even if nothing new")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD), for testing")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()
    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()

    db = store.connect()
    sync_episodes(db)
    todo = due_episodes(db, today)
    print(f"{len(todo)} episode(s) due for a snapshot")
    n_new = ingest(db, todo, today)
    purge_old_text(db, today)

    if n_new == 0 and not args.force:
        print("Nothing new. Done.")
        set_output("new_data", "false")
        return

    df = load_all(db)
    if df.empty:
        print("No stored comments yet.")
        set_output("new_data", "false")
        return
    e = a.per_episode(df).merge(
        df.drop_duplicates("video_id")[["video_id", "published"]], on="video_id")
    os.makedirs(args.out, exist_ok=True)
    # Exports contain no comment text and no usernames: only derived fields.
    df[["video_id", "guest", "published", "likes", "is_reply", "about", "compound"]].to_csv(
        f"{args.out}/tagged_comments.csv", index=False)
    e.to_csv(f"{args.out}/episode_summary.csv", index=False)

    lines = record_metrics(db, today, e)
    if lines:
        print("\nTEST OF THE CLAIM")
        for ln in lines:
            print(" -", ln)
        with open(f"{args.out}/verdict.txt", "w") as f:
            f.write("\n".join(lines))

    figs = [("who_gets_talked_about", a.chart_dumbbell(e)), ("tone_maria_vs_guest", a.chart_sentiment(df))]
    cum = cumulative(e)
    if not cum.empty:
        cum.to_csv(f"{args.out}/cumulative.csv", index=False)
        figs.append(("cumulative_gap", chart_cumulative(cum)))
    fame = a.chart_fame(e)
    if fame is not None:
        figs.append(("guest_fame_vs_gap", fame))
    for name, fig in figs:
        fig.write_image(f"{args.out}/{name}.png", scale=2)
        print("wrote", f"{args.out}/{name}.png")
    set_output("new_data", "true")


if __name__ == "__main__":
    main()
