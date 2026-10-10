"""Steps 2-5: tag comments (Maria vs guest), test the claim, plot, export PNGs.

Claim under test: viewers engage more with Maria Sharapova than with the guest.

Usage:
    python analyze_youtube.py comments.csv guests.csv

Inputs
    comments.csv  columns: video_id, comment, likes, (is_reply, date, video_title)
    guests.csv    columns: video_id, guest, guest_aliases (pipe-separated, lowercase), (guest_tier)

Method
    - Each comment gets one tag from classify.py, first match wins: Noise, Request (asks for a guest; wins over
      any name), Maria / Guest / Both (names, incl. Russian/Chinese spellings and stretched letters), Show
      (judges the show/episode), Pair (about both hosts, no names), Unnamed (she/her, no name), Host (host/presenter, no name),
      Reaction (emoji or short feeling), Topic (readable comment about the subject), Neither.
      Only Maria, Guest and Both enter the Maria-vs-guest gap. The tone score plays no part in tagging.
    - Per episode: share of ALL comments, and share of NAMED comments (Maria+Guest),
      each also like-weighted. Maria share minus guest share is the episode's gap.
    - Test: sign test and bootstrap CI on the per-episode gap, across episodes.
    - Sentiment: VADER compound on comments that name only Maria or only the guest.
"""
import re
import sys
import math
import numpy as np
import pandas as pd
import nltk
import classify
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from nltk.sentiment.vader import SentimentIntensityAnalyzer

# Palette checked with the dataviz validator (light surface): all PASS. Neither is a neutral gray, not a hue.
C_MARIA, C_GUEST, C_BOTH, C_SHOW, C_NEITHER, GRID = "#C8553D", "#1B74B8", "#B8860B", "#2A9D6F", "#C9C9C9", "#E6E6E6"
RNG = np.random.default_rng(42)


def load(comments_path, guests_path):
    c = pd.read_csv(comments_path)
    g = pd.read_csv(guests_path)
    g["guest_aliases"] = g["guest_aliases"].fillna("").astype(str).str.lower()
    missing = g[(g["guest_aliases"].str.strip() == "")]
    if len(missing):
        raise SystemExit(f"guests.csv: {len(missing)} videos have no guest_aliases. Fill them in first.")
    c["comment"] = c["comment"].fillna("").astype(str)
    c["likes"] = pd.to_numeric(c.get("likes", 0), errors="coerce").fillna(0)
    c = c[c["comment"].str.len() > 0].drop_duplicates(subset=["video_id", "comment"])
    return c.merge(g, on="video_id", how="inner")


def tag(df):
    df["about"] = classify.classify(df)
    df["focus"] = classify.focus_all(df, df["about"].values)   # Maria / Guest / Joint for Both comments, else None
    df["mode"] = classify.mode_all(df, df["about"].values)   # address vs discussion, for Maria/Guest/Both comments
    df["work"], df["topic"] = classify.flags_all(df, df["about"].values)   # Work-term side (Maria/Guest/Both) and doping topic flag
    return df


def score(df):
    nltk.download("vader_lexicon", quiet=True)
    sia = SentimentIntensityAnalyzer()
    df["compound"] = df["comment"].apply(lambda t: sia.polarity_scores(t)["compound"])
    return df


VIEWS_MAX_AGE_DAYS = 10   # a view count read more than this long after upload is not comparable across episodes


def _col(d, name):
    return d[name] if name in d else pd.Series(np.nan, index=d.index)


def _kw_text(raw):
    """Stored JSON keyword list -> 'lakers (41), basketball (33)' for the CSV."""
    import json
    try:
        return ", ".join(f"{k['w']} ({k['n']})" for k in json.loads(raw))
    except Exception:
        return ""


def _req_cols(raw):
    import json
    try:
        j = json.loads(raw)
        return {"requests_total": j["total"], "requests_naming_someone": j["named"],
                "requested_top": ", ".join(f"{x['name']} ({x['n']})" for x in j["names"][:5])}
    except Exception:
        return {"requests_total": np.nan, "requests_naming_someone": np.nan, "requested_top": ""}


def _engagement(d, mar, gst):
    """Reach and depth measures that do not depend on the Maria/guest tags."""
    n = len(d)
    out = {}
    views = pd.to_numeric(_col(d, "views"), errors="coerce").iloc[0]
    age = (pd.to_datetime(_col(d, "views_at").iloc[0], errors="coerce") - pd.to_datetime(_col(d, "published").iloc[0], errors="coerce")).days
    ok = bool(pd.notna(views) and views > 0 and pd.notna(age) and age <= VIEWS_MAX_AGE_DAYS)
    out.update(views=views, views_age_days=age, views_comparable=ok,
               comments_per_1k_views=n / views * 1000 if ok else np.nan,
               maria_per_1k_views=mar.sum() / views * 1000 if ok else np.nan,
               guest_per_1k_views=gst.sum() / views * 1000 if ok else np.nan)
    out["gap_per_1k_views"] = out["maria_per_1k_views"] - out["guest_per_1k_views"]
    au = pd.to_numeric(_col(d, "authors"), errors="coerce").iloc[0]
    out.update(distinct_commenters=au, comments_per_commenter=n / au if pd.notna(au) and au > 0 else np.nan)
    sc, w, rp = _col(d, "script"), pd.to_numeric(_col(d, "words"), errors="coerce"), pd.to_numeric(_col(d, "replies"), errors="coerce")
    out.update(share_cyrillic=(sc == "cyrillic").mean() if sc.notna().any() else np.nan,
               median_words=w.median(), long_share=(w >= 30).mean() if w.notna().any() else np.nan,
               reply_share=(rp[~d["is_reply"].astype(bool)] > 0).mean() if rp.notna().any() else np.nan)
    ts = pd.to_datetime(_col(d, "posted_at"), errors="coerce", utc=True)
    pub = pd.to_datetime(_col(d, "published").iloc[0], errors="coerce", utc=True)
    out["first_day_share"] = ((ts - pub) < pd.Timedelta(days=1))[ts.notna()].mean() if ts.notna().any() and pd.notna(pub) else np.nan
    return out


def per_episode(df, min_named=10):
    rows = []
    for vid, d in df.groupby("video_id"):
        mar = d["about"].isin(["Maria", "Both"])
        gst = d["about"].isin(["Guest", "Both"])
        named = mar | gst
        w = d["likes"] + 1  # like-weighted: each comment counts 1 + its likes
        foc = d["focus"] if "focus" in d else pd.Series(None, index=d.index, dtype=object)
        both = d["about"] == "Both"
        mar_f = (d["about"] == "Maria") | (both & (foc != "Guest"))      # Both counts for Maria unless it is guest-focused
        gst_f = (d["about"] == "Guest") | (both & (foc != "Maria"))      # ...and for the guest unless it is Maria-focused
        eng = _engagement(d, mar, gst)
        md = d["mode"] if "mode" in d else pd.Series(None, index=d.index, dtype=object)
        wd = pd.to_numeric(d["words"], errors="coerce") if "words" in d else pd.Series(np.nan, index=d.index)
        disc_m, disc_g = mar & (md == "discussion"), gst & (md == "discussion")
        eng.update(maria_address_share=(md[mar] == "address").mean() if mar.any() and md[mar].notna().any() else np.nan,
                   guest_address_share=(md[gst] == "address").mean() if gst.any() and md[gst].notna().any() else np.nan,
                   gap_discussion=disc_m.mean() - disc_g.mean() if md.notna().any() else np.nan,
                   gap_words=(wd[mar].sum() - wd[gst].sum()) / wd.sum() if wd.notna().any() and wd.sum() > 0 else np.nan)
        eng["keywords"] = _kw_text(_col(d, "keywords").iloc[0])
        eng.update(_req_cols(_col(d, "requested").iloc[0]))
        rows.append({
            **eng,
            "video_id": vid, "guest": d["guest"].iloc[0], "guest_tier": d.get("guest_tier", pd.Series([np.nan])).iloc[0],
            "n_comments": len(d), "n_named": int(named.sum()),
            "maria_share_all": mar.mean(), "guest_share_all": gst.mean(),
            "maria_share_named": mar.sum() / named.sum() if named.sum() else np.nan,
            "guest_share_named": gst.sum() / named.sum() if named.sum() else np.nan,
            "maria_likeshare": w[mar].sum() / w.sum(), "guest_likeshare": w[gst].sum() / w.sum(),
            "maria_share_focus": mar_f.mean(), "guest_share_focus": gst_f.mean(),
            "only_maria_share": (d["about"] == "Maria").mean(), "only_guest_share": (d["about"] == "Guest").mean(),
            "both_share": (d["about"] == "Both").mean(), "show_share_all": (d["about"] == "Show").mean(),
            "neither_share_all": (d["about"] == "Neither").mean(),
            "work_maria_share": d["work"].isin(["Maria", "Both"]).mean() if "work" in d else np.nan,
            "work_guest_share": d["work"].isin(["Guest", "Both"]).mean() if "work" in d else np.nan,
            "doping_share": (d["topic"] == "doping").mean() if "topic" in d else np.nan,
            **{f"share_{c.lower()}": (d["about"] == c).mean() for c in classify.CATEGORIES},
            "show_pos_share": ((d["about"] == "Show") & (d["compound"] > 0)).mean(),
            "show_neg_share": ((d["about"] == "Show") & (d["compound"] < 0)).mean(),
        })
    e = pd.DataFrame(rows)
    e["gap_all"] = e["maria_share_all"] - e["guest_share_all"]
    e["gap_likes"] = e["maria_likeshare"] - e["guest_likeshare"]
    # Counting Both for both sides cancels out of the gap (same as leaving Both out), so the real sensitivity check is focus:
    e["gap_focus"] = e["maria_share_focus"] - e["guest_share_focus"]   # Both assigned by focus
    # Worst case for the "Maria is named more" claim: every Unnamed comment (she/her, host, presenter...) is really about the guest.
    e["gap_unnamed_worst"] = e["gap_all"] - e["share_unnamed"]
    e["reliable"] = e["n_named"] >= min_named
    return e


def sign_test(gaps):
    gaps = gaps[gaps != 0]
    n, k = len(gaps), int((gaps > 0).sum())
    if n == 0:
        return n, k, float("nan")
    p = sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n   # one-sided: Maria > guest
    p2 = min(1.0, 2 * min(p, sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n))
    return n, k, p2


def boot_ci(x, n=10000):
    x = np.asarray(x)
    means = RNG.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return np.percentile(means, [2.5, 97.5])


def verdict(e):
    r = e[e["reliable"]]
    out = []
    for col, label in [("gap_all", "share of all comments"), ("gap_likes", "like-weighted share"),
                       ("gap_focus", "sensitivity: Both comments assigned by focus"),
                       ("gap_discussion", "only comments that discuss (not just address) Maria or the guest"),
                       ("gap_words", "depth-weighted: share of all words written about Maria minus about the guest"),
                       ("gap_unnamed_worst", "extreme case: every Unnamed (she/her, no name) comment given to the guest")]:
        x = r[col].dropna()
        if x.empty:
            continue
        n, k, p = sign_test(x)
        lo, hi = boot_ci(x)
        out.append(f"{label}: mean gap (Maria - guest) = {x.mean():+.3f} [95% CI {lo:+.3f}, {hi:+.3f}]; "
                   f"Maria ahead in {k}/{n} episodes; two-sided sign test p = {p:.4f}")
    if "gap_per_1k_views" in r and r["gap_per_1k_views"].notna().any():
        g = r["gap_per_1k_views"].dropna()
        out.append(f"(per audience size, {len(g)} episode(s) with a comparable view count) comments naming Maria "
                   f"{r['maria_per_1k_views'].mean():.2f} vs guest {r['guest_per_1k_views'].mean():.2f} per 1,000 views; "
                   f"Maria ahead in {int((g > 0).sum())}/{len(g)}")
    if "work_maria_share" in r and r["work_maria_share"].notna().any():
        out.append("(not part of the gap) comments mentioning work terms: Maria's "
                   f"{r['work_maria_share'].mean():.1%}, guests' {r['work_guest_share'].mean():.1%} of comments; "
                   f"doping topic list: {r['doping_share'].mean():.1%} of comments (list is off unless set on in work_terms.csv)")
    return out


def style(fig, title, w, h):
    fig.update_layout(title=dict(text=title, x=0.02, font=dict(size=22)), template="simple_white",
                      width=w, height=h, font=dict(family="Helvetica, Arial, sans-serif", size=14),
                      margin=dict(l=90, r=60, t=90, b=80), legend=dict(orientation="h", y=-0.12, x=0.02))


def chart_dumbbell(e):
    r = e[e["reliable"]].sort_values("gap_all")
    fig = go.Figure()
    for _, row in r.iterrows():
        fig.add_trace(go.Scatter(x=[row.guest_share_all, row.maria_share_all], y=[row.guest, row.guest],
                                 mode="lines", line=dict(color="#BBB", width=3), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=r["guest_share_all"], y=r["guest"], mode="markers", name="Guest named",
                             marker=dict(size=13, color=C_GUEST)))
    fig.add_trace(go.Scatter(x=r["maria_share_all"], y=r["guest"], mode="markers", name="Maria named",
                             marker=dict(size=13, color=C_MARIA)))
    style(fig, "Who do viewers talk about? Share of comments naming Maria vs the guest", 1500, max(700, 60 * len(r) + 250))
    fig.update_xaxes(title_text="Share of episode comments", tickformat=".0%", gridcolor=GRID)
    fig.update_yaxes(title_text="")
    return fig


def chart_sentiment(df):
    d = df[df["about"].isin(["Maria", "Guest", "Show"])]
    fig = go.Figure()
    for who, col in [("Maria", C_MARIA), ("Guest", C_GUEST), ("Show", C_SHOW)]:
        s = d[d["about"] == who]["compound"]
        fig.add_trace(go.Box(y=s, name=f"{who} (n={len(s)})", marker_color=col, boxmean=True))
    style(fig, "Tone of comments about only Maria, only the guest, or the show itself", 1000, 900)
    fig.update_yaxes(title_text="VADER compound (-1 to +1)", range=[-1, 1], gridcolor=GRID)
    return fig


MIX = [("Maria", "only Maria", C_MARIA), ("Guest", "only guest", C_GUEST), ("Both", "both named", C_BOTH),
       ("Show", "the show/episode", C_SHOW), ("Other", "everything else (see category chart)", C_NEITHER)]


def _grouped(about):
    """Share per chart segment: Maria, Guest, Both, Show; every other category is folded into Other."""
    sh = about.value_counts(normalize=True)
    out = {k: float(sh.get(k, 0.0)) for k in ("Maria", "Guest", "Both", "Show")}
    out["Other"] = 1.0 - sum(out.values())
    return out


def chart_mix(df):
    """What are comments about? Stacked 100% bars per episode, plus an all-episodes row with direct labels."""
    order = ("published" if "published" in df.columns else "guest")
    names = df.drop_duplicates("video_id").sort_values(order)["guest"].tolist()
    rows = [(g, _grouped(d["about"])) for g, d in
            sorted(df.groupby("guest"), key=lambda kv: names.index(kv[0]))]
    rows.append(("ALL EPISODES", _grouped(df["about"])))
    labels = [r[0] for r in rows]
    fig = go.Figure()
    for key, nice, col in MIX:
        vals = [r[1][key] for r in rows]
        txt = [f"{v:.0%}" if (lab == "ALL EPISODES" and v >= 0.04) else "" for lab, v in zip(labels, vals)]
        fig.add_trace(go.Bar(y=labels, x=vals, orientation="h", name=nice, marker=dict(color=col, line=dict(color="#FFFFFF", width=2)),
                             text=txt, textposition="inside", textfont=dict(color="#FFFFFF" if key != "Other" else "#333333"),
                             hovertemplate="%{y}: %{x:.1%}<extra>" + nice + "</extra>"))
    style(fig, "What are the comments about? Share of each episode's comments", 1500, max(700, 40 * len(rows) + 250))
    fig.update_layout(barmode="stack")
    fig.update_xaxes(title_text="Share of episode comments", tickformat=".0%", range=[0, 1], gridcolor=GRID)
    fig.update_yaxes(title_text="", autorange="reversed")
    return fig


def chart_categories(df):
    """All comments, every category, one bar each (one hue, sorted): the full breakdown behind 'everything else'."""
    sh = df["about"].value_counts(normalize=True).reindex(classify.CATEGORIES).fillna(0).sort_values()
    fig = go.Figure(go.Bar(y=sh.index, x=sh.values, orientation="h", marker=dict(color=C_GUEST),
                           text=[f"{v:.0%}" for v in sh.values], textposition="outside",
                           hovertemplate="%{y}: %{x:.1%}<extra></extra>"))
    style(fig, "Share of all comments by category", 1100, 750)
    fig.update_layout(showlegend=False)
    fig.update_xaxes(title_text="Share of comments", tickformat=".0%", gridcolor=GRID, range=[0, max(0.1, sh.max() * 1.2)])
    fig.update_yaxes(title_text="")
    return fig


def chart_engagement(e):
    """Comments naming Maria vs naming the guest, per 1,000 views, for episodes whose view count was read within 10 days of upload."""
    r = e[e["views_comparable"].fillna(False).astype(bool)].sort_values("published")
    if r.empty:
        return None
    fig = go.Figure()
    for col, name, color in (("maria_per_1k_views", "Maria (incl. both-named)", C_MARIA), ("guest_per_1k_views", "Guest (incl. both-named)", C_GUEST)):
        fig.add_trace(go.Bar(y=r["guest"], x=r[col], orientation="h", name=name, marker=dict(color=color, line=dict(color="#FFFFFF", width=2)),
                             hovertemplate="%{y}: %{x:.2f} per 1,000 views<extra>" + name + "</extra>"))
    style(fig, "Comments naming Maria vs the guest, per 1,000 views", 1300, max(600, 55 * len(r) + 220))
    fig.update_layout(barmode="group")
    fig.update_xaxes(title_text="Comments per 1,000 views", gridcolor=GRID)
    fig.update_yaxes(title_text="", autorange="reversed")
    return fig


def chart_requests_table(wide, top=10, last=8):
    """Picture of the requested-guest tally: top names, overall total, then the most recent episodes (full table is in the CSV)."""
    if wide is None or wide.empty:
        return None
    t = wide.head(top)
    cols = ["total"] + [c for c in t.columns if c != "total"][-last:]
    t = t[cols]
    header = ["Requested guest"] + ["All episodes" if c == "total" else c for c in cols]
    cells = [list(t.index)] + [t[c].astype(int).tolist() for c in cols]
    fig = go.Figure(go.Table(
        header=dict(values=header, fill_color="#E9E9E9", align="left", font=dict(size=13, color="#222222"), height=34),
        cells=dict(values=cells, align=["left"] + ["right"] * len(cols), fill_color="#FFFFFF", font=dict(size=13, color="#222222"), height=30,
                   line_color="#DDDDDD")))
    fig.update_layout(title=dict(text=f"Most requested guests (top {len(t)}; latest {min(last, len(cols) - 1)} episodes shown)", x=0.02, font=dict(size=22)),
                      width=max(900, 120 * len(header) + 200), height=90 + 36 * (len(t) + 1) + 60, margin=dict(l=40, r=40, t=80, b=30))
    return fig


def alias_audit(df, e=None, min_held=100):
    """Check each guest's aliases against the comment text still held. Per alias: hits in its own episode and in all the other
    episodes (per 1,000 comments). Flags: no hits in a well-covered episode (a missing nickname/spelling is likely elsewhere), a
    very short alias, and an alias that matches as often in OTHER episodes (a common word or another person with that name).
    Also lists the episode's stored top words, where a fan nickname that is not an alias tends to show up. Returns a DataFrame."""
    h = df[df["comment"].notna()]
    if h.empty:
        return pd.DataFrame()
    norm = h["comment"].map(lambda t: classify.variants(t)[0])
    vid = h["video_id"].values
    rows = []
    kws = {}
    if "keywords" in df:
        import json
        for v, raw in df.drop_duplicates("video_id")[["video_id", "keywords"]].values:
            try:
                kws[v] = ", ".join(k["w"] for k in json.loads(raw)[:8])
            except Exception:
                kws[v] = ""
    for v, d in df.drop_duplicates("video_id").groupby("video_id"):
        own = vid == v
        n_own, n_oth = int(own.sum()), int((~own).sum())
        if n_own == 0:
            continue
        for al in str(d["guest_aliases"].iloc[0]).split("|"):
            al = al.strip()
            rx = classify.alias_regex(al)
            if not al or rx is None:
                continue
            hit = norm.map(lambda t: bool(rx.search(t))).values
            oh, xh = int(hit[own].sum()), int(hit[~own].sum())
            o_rate, x_rate = oh / n_own * 1000, (xh / n_oth * 1000 if n_oth else 0.0)
            flags = []
            if len(classify._strip(al).strip()) <= 3:
                flags.append("very short alias")
            if xh >= 5 and x_rate >= 0.5 * max(o_rate, 0.001):
                flags.append("also matches other episodes (common word or another person?)")
            rows.append({"episode_guest": d["guest"].iloc[0], "alias": al, "own_comments_held": n_own, "own_hits": oh,
                         "own_per_1k": round(o_rate, 1), "other_comments_held": n_oth, "other_hits": xh,
                         "other_per_1k": round(x_rate, 1), "flag": "; ".join(flags), "episode_top_words": kws.get(v, "")})
    out = pd.DataFrame(rows)
    if len(out):   # an unused spelling is harmless; the real warning is an episode where NO alias matched anything
        tot = out.groupby("episode_guest")["own_hits"].transform("sum")
        cov = out["own_comments_held"] >= min_held
        out["flag"] = [("no alias matched anything in this episode; " if (t == 0 and c) else "") + f for t, c, f in zip(tot, cov, out["flag"])]
        out["flag"] = out["flag"].str.rstrip("; ")
    if e is not None and len(out) and "guest_share_all" in e:
        med = e["guest_share_all"].median()
        low = set(e.loc[e["guest_share_all"] < 0.5 * med, "guest"]) if med > 0 else set()
        out["episode_note"] = out["episode_guest"].map(lambda g: "guest named far less often than the typical episode: look for missing nicknames" if g in low else "")
    return out.sort_values(["flag", "episode_guest"], ascending=[False, True]) if len(out) else out


def chart_fame(e):
    r = e[e["reliable"] & e["guest_tier"].notna()]
    if r.empty:
        return None
    fig = go.Figure(go.Scatter(x=r["guest_tier"], y=r["gap_all"], mode="markers+text", text=r["guest"],
                               textposition="top center", marker=dict(size=14, color=C_MARIA)))
    fig.add_hline(y=0, line=dict(color="#999", dash="dash"))
    style(fig, "Does guest fame change the Maria-vs-guest gap?", 1200, 900)
    fig.update_xaxes(title_text="Guest tier (1 = mega-famous, 3 = niche)", dtick=1, gridcolor=GRID)
    fig.update_yaxes(title_text="Maria share minus guest share", tickformat=".0%", gridcolor=GRID)
    return fig


def main(cpath, gpath):
    df = tag(score(load(cpath, gpath)))
    e = per_episode(df)
    df.to_csv("tagged_comments.csv", index=False)
    e.to_csv("episode_summary.csv", index=False)
    print(f"{len(df)} comments, {df['video_id'].nunique()} episodes "
          f"({int(e['reliable'].sum())} with >=10 comments naming Maria or the guest)")
    print(df["about"].value_counts(normalize=True).round(3).to_string())
    print("\nTEST OF THE CLAIM")
    for line in verdict(e):
        print(" -", line)
    figs = [("who_gets_talked_about", chart_dumbbell(e)), ("tone_maria_vs_guest", chart_sentiment(df)),
            ("what_comments_are_about", chart_mix(df)),
            ("comment_categories", chart_categories(df))]
    fame = chart_fame(e)
    if fame is not None:
        figs.append(("guest_fame_vs_gap", fame))
    for name, fig in figs:
        fig.write_image(f"{name}.png", scale=2)
        print("wrote", f"{name}.png")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
