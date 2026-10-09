"""Weekly runner: ingest due episodes, store in Turso, recompute cumulative stats, draw charts.

Usage:
    python run_weekly.py [--force] [--today YYYY-MM-DD] [--out out]

Flow
  1. Load episodes.csv (one row per episode you add) into the episodes table.
  2. For each pending episode that is at least SNAPSHOT_DAYS old, fetch its comments ONCE and keep only those
     posted in its first SNAPSHOT_DAYS days (so old and new episodes are comparable), score sentiment, tag who each comment is about (Maria / guest / both / the show / neither), store, mark done.
  3. Re-tag stored comments whose text is still held (so tag rule changes apply), then purge raw comment text older than PURGE_AFTER_DAYS (YouTube's developer policies cap
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
import classify

SNAPSHOT_DAYS = 7
PURGE_AFTER_DAYS = 28     # margin under the 30-day limit
EPISODES_CSV = "episodes.csv"
QUOTA_REASONS = ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded")
SCAN_CAP = int(os.environ.get("MAX_SCAN") or 30000)   # safety cap on comments scanned per episode
FAILED = []                                           # episodes whose fetch failed unexpectedly this run
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


def fetch_one(e):
    if os.environ.get("COMMENT_SOURCE") == "apify":
        from scrape_youtube import fetch_comments
        return fetch_comments([e["video_url"]])
    import youtube_api
    return youtube_api.fetch_comments([e["video_id"]], max_comments=SCAN_CAP)


def windowed(d, e):
    """Keep only comments posted in the first SNAPSHOT_DAYS days (upload day plus the next six, UTC), so
    old and new episodes are measured over the same period. Returns None if the window can't be proven complete."""
    vid = e["video_id"]
    if os.environ.get("COMMENT_SOURCE") == "apify" or "date" not in d.columns:
        print(f"  {vid}: no usable comment dates, {SNAPSHOT_DAYS}-day window NOT applied")
        return d
    start = pd.Timestamp(e["published"], tz="UTC")
    end = start + pd.Timedelta(days=SNAPSHOT_DAYS)
    ts = pd.to_datetime(d["date"], errors="coerce", utc=True)
    if ts.notna().mean() < 0.5:
        print(f"  {vid}: comment dates missing, {SNAPSHOT_DAYS}-day window NOT applied")
        return d
    if len(d) >= SCAN_CAP:
        # Comments arrive newest-first, so hitting the cap means the earliest comments may not have been reached.
        print(f"  {vid}: scan cap ({SCAN_CAP}) reached before the first {SNAPSHOT_DAYS} days could be confirmed; "
              "leaving pending (raise the MAX_SCAN variable)")
        return None
    kept = d[ts.notna() & (ts < end)].copy()
    print(f"  {vid}: kept {len(kept)} of {len(d)} comments (first {SNAPSHOT_DAYS} days only)")
    return kept


def ingest(db, episodes, today):
    done = 0
    for e in episodes:
        try:
            d = fetch_one(e)
        except Exception as ex:   # one bad episode must not block the others (SystemExit still stops the run)
            if getattr(ex, "reason", "") in QUOTA_REASONS:
                print("  YouTube quota is used up for today; the remaining episodes will be retried tomorrow")
                break
            print(f"  {e['video_id']}: fetch failed ({ex}); will retry on the next run")
            FAILED.append(e["video_id"])
            continue
        if d.empty:
            print(f"  {e['video_id']}: no comments returned, leaving pending")
            continue
        d = windowed(d, e)
        if d is None:
            continue
        if d.empty:
            db.execute("UPDATE episodes SET status='done', n_comments=0, snapshot_at=? WHERE video_id=?",
                       (today.isoformat(), e["video_id"]))
            print(f"  {e['video_id']}: no comments in the first {SNAPSHOT_DAYS} days, marked done with 0 comments")
            done += 1
            continue
        d = d.copy()
        d["comment"] = d["comment"].fillna("").astype(str)
        d = d[d["comment"].str.len() > 0]
        has_id = "comment_id" in d and d["comment_id"].notna().all()
        d["comment_key"] = d["comment_id"].astype(str) if has_id else d["comment"].apply(
            lambda t: hashlib.sha1((e["video_id"] + t).encode()).hexdigest())
        d = d.drop_duplicates("comment_key")
        d["guest_aliases"] = e["guest_aliases"]
        d = a.tag(a.score(d))            # score, then tag (Show needs the tone); tag + sentiment at ingest, so they survive the text purge
        d["likes"] = pd.to_numeric(d["likes"], errors="coerce").fillna(0).astype(int)
        rows = [(e["video_id"], r.comment_key, r.comment, int(r.likes), int(bool(r.is_reply)),
                 float(r.compound), r.about, today.isoformat(), classify.RULES_VERSION) for r in d.itertuples()]
        db.executemany("INSERT OR IGNORE INTO comments "
                       "(video_id, comment_key, text, likes, is_reply, compound, about, fetched_at, rules_version) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", rows)
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


def retag_stored(db):
    """Re-tag comments whose text is still stored (so rule or alias changes apply) and stamp them with the current
    rules version. Comments whose text is already purged keep the tag and version they had."""
    rows = db.execute("""SELECT c.video_id, c.comment_key, c.text AS comment, c.compound, c.about AS old,
                                c.rules_version AS ver, e.guest_aliases
                         FROM comments c JOIN episodes e USING (video_id) WHERE c.text IS NOT NULL""")
    if not rows:
        return
    d = pd.DataFrame(rows)
    d["old"] = d["old"].astype(str)
    d["new"] = a.tag(d.copy())["about"].values
    todo = d[(d["new"] != d["old"]) | (d["ver"] != classify.RULES_VERSION)]
    if len(todo):
        db.executemany("UPDATE comments SET about=?, rules_version=? WHERE video_id=? AND comment_key=?",
                       [(r.new, classify.RULES_VERSION, r.video_id, r.comment_key) for r in todo.itertuples()])
    print(f"Re-tagged {int((d['new'] != d['old']).sum())} stored comment(s) with text still held "
          f"(rules {classify.RULES_VERSION}); {len(todo)} stamped with this version")


def load_all(db):
    c = pd.DataFrame(db.execute(
        """SELECT c.video_id, c.text AS comment, c.likes, c.is_reply, c.compound, c.about AS stored_about, c.rules_version,
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
    retag_stored(db)
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
    ver = df.assign(rules_version=df["rules_version"].fillna("pre-versioning")).groupby("video_id")["rules_version"].agg(
        lambda x: ", ".join(sorted(x.unique())))
    e["rules_versions"] = e["video_id"].map(ver)
    counts = df["rules_version"].fillna("pre-versioning").value_counts()
    print("Comments by rules version:", counts.to_dict())
    if len(counts) > 1:
        print("WARNING: tags come from more than one rules version; compare episodes with the same version "
              "(see rules_versions in episode_summary.csv).")
    os.makedirs(args.out, exist_ok=True)
    # Exports contain no comment text and no usernames: only derived fields.
    df[["video_id", "guest", "published", "likes", "is_reply", "about", "compound", "rules_version"]].to_csv(
        f"{args.out}/tagged_comments.csv", index=False)
    e.to_csv(f"{args.out}/episode_summary.csv", index=False)

    lines = record_metrics(db, today, e)
    if lines:
        print("\nTEST OF THE CLAIM")
        for ln in lines:
            print(" -", ln)
        with open(f"{args.out}/verdict.txt", "w") as f:
            f.write("\n".join(lines))

    figs = [("who_gets_talked_about", a.chart_dumbbell(e)), ("tone_maria_vs_guest", a.chart_sentiment(df)),
            ("what_comments_are_about", a.chart_mix(df)),
            ("comment_categories", a.chart_categories(df))]
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
    if FAILED:
        raise SystemExit(f"{len(FAILED)} episode(s) could not be fetched and will be retried: {', '.join(FAILED)}")
