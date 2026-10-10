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
import json
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
        "SELECT video_id, video_url, published, guest, guest_aliases FROM episodes WHERE status='pending'")
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
        authors = int(d["author"].nunique()) if "author" in d and d["author"].notna().any() else None
        d = d.drop(columns=["author"], errors="ignore")      # commenter ids are counted, never stored
        d["guest_aliases"] = e["guest_aliases"]
        d["guest"] = e["guest"]
        d["posted_at"] = d["date"].astype(str).where(d["date"].notna(), None) if "date" in d else None
        d["script"] = d["comment"].apply(classify.script_of)
        d["words"] = d["comment"].apply(classify.n_words)
        d["replies"] = pd.to_numeric(d["replies"], errors="coerce") if "replies" in d else None
        d = a.tag(a.score(d))            # score, then tag (Show needs the tone); tag + sentiment at ingest, so they survive the text purge
        kw = classify.keywords(d)         # candidates for the week's keyword, from text we still hold (counts only are stored)
        req = classify.requested_names(d)  # which guests viewers ask for (names and counts only)
        d["likes"] = pd.to_numeric(d["likes"], errors="coerce").fillna(0).astype(int)
        rows = [(e["video_id"], r.comment_key, r.comment, int(r.likes), int(bool(r.is_reply)),
                 float(r.compound), r.about, today.isoformat(), classify.RULES_VERSION,
                 r.focus if isinstance(r.focus, str) else None,
                 r.work if isinstance(r.work, str) else None, r.topic if isinstance(r.topic, str) else None,
                 r.mode if isinstance(r.mode, str) else None,
                 r.posted_at if isinstance(r.posted_at, str) else None, r.script, int(r.words),
                 int(r.replies) if pd.notna(r.replies) else None) for r in d.itertuples()]
        db.executemany("INSERT OR IGNORE INTO comments "
                       "(video_id, comment_key, text, likes, is_reply, compound, about, fetched_at, rules_version, focus, work, topic, mode, posted_at, script, words, replies) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.execute("UPDATE episodes SET status='done', n_comments=?, snapshot_at=?, authors=?, keywords=?, requested=? WHERE video_id=?",
                   (len(rows), today.isoformat(), authors, json.dumps(kw, ensure_ascii=False),
                    json.dumps(req, ensure_ascii=False), e["video_id"]))
        store_video_stats(db, e["video_id"], today)
        print(f"  KEYWORD CANDIDATES for {e['guest']}: " + (", ".join(f"{k['w']} ({k['n']}{', work' if k['kind'] == 'work' else ''})" for k in kw[:5]) or "none reached 3 comments"))
        print(f"  REQUESTED GUESTS in {e['guest']}'s comments: {req['named']} of {req['total']} requests named someone"
              + (": " + ", ".join(f"{x['name']} ({x['n']})" for x in req["names"][:5]) if req["names"] else ""))
        print(f"  {e['video_id']}: stored {len(rows)} comments from {authors if authors is not None else '?'} distinct commenters")
        done += 1
    return done


def store_video_stats(db, vid, today):
    """Views / likes / comment count of the video as of today (the snapshot day), so engagement can be put per 1,000 views.
    A failure here never blocks the run; views_at records the date so the age of the count is always known."""
    if os.environ.get("COMMENT_SOURCE") == "apify":
        return
    try:
        import youtube_api
        s = youtube_api.video_stats(vid)
    except Exception as ex:
        print(f"  {vid}: could not read video statistics ({ex}); will try again on a later run")
        return
    db.execute("UPDATE episodes SET views=?, video_likes=?, video_comments=?, views_at=? WHERE video_id=?",
               (s["views"], s["video_likes"], s["video_comments"], today.isoformat(), vid))


def backfill_video_stats(db, today):
    """Episodes finished before views were stored (or whose first read failed): read the current counts once. The date is
    kept in views_at; counts taken long after upload are not used in the per-1,000-views chart."""
    for r in db.execute("SELECT video_id FROM episodes WHERE status='done' AND views_at IS NULL"):
        store_video_stats(db, r["video_id"], today)


def backfill_requests(db):
    """Episodes finished before this existed: work out the requested names, but only when every comment of the episode still has
    its text (otherwise the tally would be partial and misleading)."""
    eps = db.execute("""SELECT e.video_id, e.guest, e.guest_aliases FROM episodes e
                        WHERE e.status='done' AND e.requested IS NULL
                          AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.video_id=e.video_id AND c.text IS NULL)
                          AND EXISTS (SELECT 1 FROM comments c WHERE c.video_id=e.video_id)""")
    for e in eps:
        d = pd.DataFrame(db.execute("SELECT text AS comment FROM comments WHERE video_id=?", (e["video_id"],)))
        d["guest"], d["guest_aliases"] = e["guest"], e["guest_aliases"]
        d["video_id"] = e["video_id"]
        d["about"] = classify.classify(d)
        db.execute("UPDATE episodes SET requested=? WHERE video_id=?",
                   (json.dumps(classify.requested_names(d), ensure_ascii=False), e["video_id"]))
        print(f"  {e['guest']}: requested guests filled in from the comments still held")


def request_tables(df):
    """Per-episode tally of requested guests. Returns (long, wide): long has one row per episode and name with this episode's
    count, the running total up to and including it (episodes in publish order) and the name's overall rank; wide has one row
    per name and one column per episode. Names are merged across episodes (same spelling rules as within an episode)."""
    eps = df.drop_duplicates("video_id").sort_values("published")
    per = []
    for r in eps.itertuples():
        try:
            j = json.loads(r.requested) if isinstance(r.requested, str) else None
        except Exception:
            j = None
        if j is not None:
            per.append((r.guest, r.published, {x["name"]: x["n"] for x in j["names"]}, j["total"], j["named"]))
    if not per:
        return None, None
    allc = {}
    for _, _, c, _, _ in per:
        for k, v in c.items():
            allc[k] = allc.get(k, 0) + v
    _, disp = classify.merge_names(allc, with_map=True)   # every stored spelling -> its merged display name
    running, rows = {}, []
    for guest, pub, c, total, named in per:
        here = {}
        for k, v in c.items():
            here[disp[k]] = here.get(disp[k], 0) + v
        for n, v in here.items():
            running[n] = running.get(n, 0) + v
        for n in sorted(running, key=lambda x: -running[x]):
            rows.append({"episode_guest": guest, "published": pub, "requested_name": n,
                         "mentions_this_episode": here.get(n, 0), "running_total": running[n],
                         "requests_in_episode": total, "requests_naming_someone": named})
    long = pd.DataFrame(rows)
    rank = long.groupby("requested_name")["running_total"].max().rank(ascending=False, method="min").astype(int)
    long["overall_rank"] = long["requested_name"].map(rank)
    wide = long.pivot_table(index="requested_name", columns="episode_guest", values="mentions_this_episode", aggfunc="sum", fill_value=0)
    wide = wide[[g for g, *_ in per if g in wide.columns]]
    wide.insert(0, "total", wide.sum(axis=1))
    wide = wide.sort_values("total", ascending=False)
    return long[long["mentions_this_episode"] > 0].sort_values(["published", "mentions_this_episode"], ascending=[True, False]), wide


def purge_old_text(db, today):
    cutoff = (today - timedelta(days=PURGE_AFTER_DAYS)).isoformat()
    n = db.execute("SELECT count(*) AS n FROM comments WHERE text IS NOT NULL AND fetched_at <= ?",
                   (cutoff,))[0]["n"]
    if n:
        db.execute("UPDATE comments SET text=NULL WHERE text IS NOT NULL AND fetched_at <= ?", (cutoff,))
    print(f"Purged raw text of {n} comment(s) older than {PURGE_AFTER_DAYS} days")


def retag_stored(db):
    """Re-tag comments whose text is still stored (so rule, alias or work-term changes apply), refresh their Both-focus, Work flag
    and doping flag and stamp them with the current rules version. Comments whose text is already purged keep what they had."""
    rows = db.execute("""SELECT c.video_id, c.comment_key, c.text AS comment, c.compound, c.about AS old,
                                c.focus AS oldfocus, c.work AS oldwork, c.topic AS oldtopic, c.mode AS oldmode, c.rules_version AS ver, c.script, c.words,
                                e.guest, e.guest_aliases
                         FROM comments c JOIN episodes e USING (video_id) WHERE c.text IS NOT NULL""")
    if not rows:
        return
    d = pd.DataFrame(rows)
    d["old"] = d["old"].astype(str)
    for k in ("focus", "work", "topic", "mode"):
        d["old" + k] = d["old" + k].fillna("").astype(str)
    t = a.tag(d.copy())
    d["new"] = t["about"].values
    for k in ("focus", "work", "topic", "mode"):
        d["new" + k] = pd.Series(t[k].values).fillna("").astype(str).values
    changed = (d["new"] != d["old"]) | (d["newfocus"] != d["oldfocus"]) | (d["newwork"] != d["oldwork"]) | (d["newtopic"] != d["oldtopic"]) | (d["newmode"] != d["oldmode"])
    todo = d[changed | (d["ver"] != classify.RULES_VERSION)]
    if len(todo) or d["script"].isna().any() or d["words"].isna().any():
        db.executemany("UPDATE comments SET script=?, words=? WHERE video_id=? AND comment_key=? AND (script IS NULL OR words IS NULL)",
                       [(classify.script_of(r.comment), classify.n_words(r.comment), r.video_id, r.comment_key)
                        for r in d.itertuples() if pd.isna(r.script) or pd.isna(r.words)])
    if len(todo):
        db.executemany("UPDATE comments SET about=?, focus=?, work=?, topic=?, mode=?, rules_version=? WHERE video_id=? AND comment_key=?",
                       [(r.new, r.newfocus or None, r.newwork or None, r.newtopic or None, r.newmode or None, classify.RULES_VERSION,
                         r.video_id, r.comment_key) for r in todo.itertuples()])
    print(f"Re-tagged {int((d['new'] != d['old']).sum())} stored comment(s) with text still held "
          f"(rules {classify.RULES_VERSION}); {len(todo)} stamped with this version")


def load_all(db):
    c = pd.DataFrame(db.execute(
        """SELECT c.video_id, c.text AS comment, c.likes, c.is_reply, c.compound, c.about AS stored_about, c.focus AS stored_focus,
                  c.work AS stored_work, c.topic AS stored_topic, c.mode AS stored_mode, c.rules_version,
                  c.posted_at, c.script AS stored_script, c.words AS stored_words, c.replies,
                  e.guest, e.guest_aliases, e.guest_tier, e.published,
                  e.views, e.video_likes, e.video_comments, e.views_at, e.authors, e.keywords, e.requested
           FROM comments c JOIN episodes e USING (video_id) WHERE e.status='done'"""))
    if c.empty:
        return c
    c["guest_tier"] = pd.to_numeric(c["guest_tier"], errors="coerce")
    has_text = c["comment"].notna()
    for k in ("about", "focus", "work", "topic", "mode"):
        c[k] = c["stored_" + k]
    if has_text.any():   # alias / term fixes re-tag comments whose text is still within the retention window
        t = a.tag(c[has_text].copy())
        for k in ("about", "focus", "work", "topic", "mode"):
            c.loc[has_text, k] = t[k].values
    c["script"], c["words"] = c["stored_script"], c["stored_words"]
    if has_text.any():
        c.loc[has_text, "script"] = c.loc[has_text, "comment"].apply(classify.script_of)
        c.loc[has_text, "words"] = c.loc[has_text, "comment"].apply(classify.n_words)
    return c.drop(columns=["stored_" + k for k in ("about", "focus", "work", "topic", "mode", "script", "words")])


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
    missing = classify.guests_without_terms([r["guest"] for r in db.execute("SELECT guest FROM episodes ORDER BY published")])
    print("Work terms: " + (f"none yet for {len(missing)} guest(s): {', '.join(missing)} (add rows to work_terms.csv)"
                            if missing else "every guest has at least one row in work_terms.csv"))
    todo = due_episodes(db, today)
    print(f"{len(todo)} episode(s) due for a snapshot")
    n_new = ingest(db, todo, today)
    retag_stored(db)
    backfill_video_stats(db, today)
    backfill_requests(db)
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
    df[["video_id", "guest", "published", "likes", "is_reply", "about", "focus", "work", "topic", "mode", "compound", "rules_version", "posted_at", "script", "words", "replies"]].to_csv(
        f"{args.out}/tagged_comments.csv", index=False)
    e.to_csv(f"{args.out}/episode_summary.csv", index=False)
    audit = a.alias_audit(df, e)
    if len(audit):
        audit.to_csv(f"{args.out}/alias_audit.csv", index=False)
        bad = audit[(audit["flag"] != "") | (audit.get("episode_note", "") != "")]
        print(f"\nALIAS AUDIT: {len(bad)} of {len(audit)} alias row(s) flagged (see alias_audit.csv; text still held for "
              f"{int(df['comment'].notna().sum())} comments)")
        for r in bad.head(12).itertuples():
            print(f"  - {r.episode_guest} / '{r.alias}': {r.flag or r.episode_note}")
    req_long, req_wide = request_tables(df)
    if req_long is not None and len(req_wide):
        req_long.to_csv(f"{args.out}/guest_requests.csv", index=False)
        req_wide.to_csv(f"{args.out}/guest_requests_table.csv")
        try:
            fig = a.chart_requests_table(req_wide)
            if fig is not None:
                fig.write_image(f"{args.out}/guest_requests_table.png", scale=2)
        except Exception as ex:      # the table picture is a convenience; the CSVs above are the data
            print(f"(could not draw guest_requests_table.png: {ex})")
        print("\nMOST REQUESTED GUESTS SO FAR (all tallied episodes): " +
              ", ".join(f"{n} ({int(t)})" for n, t in req_wide["total"].head(10).items()))

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
    eng = a.chart_engagement(e)
    if eng is not None:
        figs.append(("comments_per_1k_views", eng))
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
