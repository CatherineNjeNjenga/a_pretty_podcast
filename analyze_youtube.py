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
      (judges the show/episode), Pair (about both hosts, no names), Unnamed (she/her or a role, no name),
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
    return df


def score(df):
    nltk.download("vader_lexicon", quiet=True)
    sia = SentimentIntensityAnalyzer()
    df["compound"] = df["comment"].apply(lambda t: sia.polarity_scores(t)["compound"])
    return df


def per_episode(df, min_named=10):
    rows = []
    for vid, d in df.groupby("video_id"):
        mar = d["about"].isin(["Maria", "Both"])
        gst = d["about"].isin(["Guest", "Both"])
        named = mar | gst
        w = d["likes"] + 1  # like-weighted: each comment counts 1 + its likes
        rows.append({
            "video_id": vid, "guest": d["guest"].iloc[0], "guest_tier": d.get("guest_tier", pd.Series([np.nan])).iloc[0],
            "n_comments": len(d), "n_named": int(named.sum()),
            "maria_share_all": mar.mean(), "guest_share_all": gst.mean(),
            "maria_share_named": mar.sum() / named.sum() if named.sum() else np.nan,
            "guest_share_named": gst.sum() / named.sum() if named.sum() else np.nan,
            "maria_likeshare": w[mar].sum() / w.sum(), "guest_likeshare": w[gst].sum() / w.sum(),
            "only_maria_share": (d["about"] == "Maria").mean(), "only_guest_share": (d["about"] == "Guest").mean(),
            "both_share": (d["about"] == "Both").mean(), "show_share_all": (d["about"] == "Show").mean(),
            "neither_share_all": (d["about"] == "Neither").mean(),
            **{f"share_{c.lower()}": (d["about"] == c).mean() for c in classify.CATEGORIES},
            "show_pos_share": ((d["about"] == "Show") & (d["compound"] > 0)).mean(),
            "show_neg_share": ((d["about"] == "Show") & (d["compound"] < 0)).mean(),
        })
    e = pd.DataFrame(rows)
    e["gap_all"] = e["maria_share_all"] - e["guest_share_all"]
    e["gap_likes"] = e["maria_likeshare"] - e["guest_likeshare"]
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
    for col, label in [("gap_all", "share of all comments"), ("gap_likes", "like-weighted share")]:
        x = r[col].dropna()
        n, k, p = sign_test(x)
        lo, hi = boot_ci(x)
        out.append(f"{label}: mean gap (Maria - guest) = {x.mean():+.3f} [95% CI {lo:+.3f}, {hi:+.3f}]; "
                   f"Maria ahead in {k}/{n} episodes; two-sided sign test p = {p:.4f}")
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
