"""Steps 2-5: tag comments (Maria vs guest), test the claim, plot, export PNGs.

Claim under test: viewers engage more with Maria Sharapova than with the guest.

Usage:
    python analyze_youtube.py comments.csv guests.csv

Inputs
    comments.csv  columns: video_id, comment, likes, (is_reply, date, video_title)
    guests.csv    columns: video_id, guest, guest_aliases (pipe-separated, lowercase), (guest_tier)

Method
    - A comment is "Maria" if it names her (maria, sharapova, masha), "Guest" if it
      names the guest (any alias), "Both" if it names both, "Neither" otherwise.
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
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from nltk.sentiment.vader import SentimentIntensityAnalyzer

MARIA = re.compile(r"\b(maria|sharapova|masha)\b", re.I)
C_MARIA, C_GUEST, GRID = "#C8553D", "#2A6F97", "#E6E6E6"
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


def alias_regex(aliases):
    parts = [re.escape(a.strip()) for a in aliases.split("|") if a.strip()]
    parts = [p for p in parts if not MARIA.fullmatch(p)]  # a guest alias must not be Maria's name
    return re.compile(r"\b(" + "|".join(parts) + r")\b", re.I) if parts else None


def tag(df):
    rx = {vid: alias_regex(a) for vid, a in df.drop_duplicates("video_id")[["video_id", "guest_aliases"]].values}
    m = df["comment"].apply(lambda t: bool(MARIA.search(t)))
    gst = pd.Series([bool(rx[v].search(t)) if rx[v] else False for v, t in zip(df["video_id"], df["comment"])],
                    index=df.index)
    df["about"] = np.select([m & gst, m, gst], ["Both", "Maria", "Guest"], default="Neither")
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
    d = df[df["about"].isin(["Maria", "Guest"])]
    fig = go.Figure()
    for who, col in [("Maria", C_MARIA), ("Guest", C_GUEST)]:
        s = d[d["about"] == who]["compound"]
        fig.add_trace(go.Box(y=s, name=f"{who} (n={len(s)})", marker_color=col, boxmean=True))
    style(fig, "Tone of comments that name only Maria vs only the guest", 1000, 900)
    fig.update_yaxes(title_text="VADER compound (-1 to +1)", range=[-1, 1], gridcolor=GRID)
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
    df = score(tag(load(cpath, gpath)))
    e = per_episode(df)
    df.to_csv("tagged_comments.csv", index=False)
    e.to_csv("episode_summary.csv", index=False)
    print(f"{len(df)} comments, {df['video_id'].nunique()} episodes "
          f"({int(e['reliable'].sum())} with >=10 comments naming Maria or the guest)")
    print(df["about"].value_counts(normalize=True).round(3).to_string())
    print("\nTEST OF THE CLAIM")
    for line in verdict(e):
        print(" -", line)
    figs = [("who_gets_talked_about", chart_dumbbell(e)), ("tone_maria_vs_guest", chart_sentiment(df))]
    fame = chart_fame(e)
    if fame is not None:
        figs.append(("guest_fame_vs_gap", fame))
    for name, fig in figs:
        fig.write_image(f"{name}.png", scale=2)
        print("wrote", f"{name}.png")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
