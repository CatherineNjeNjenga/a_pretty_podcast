"""Steps 2-5: VADER sentiment -> Pandas monthly aggregation -> Plotly charts -> PNG export.

Usage:
    python analyze.py reviews.csv
Input CSV needs columns: text, date  (optional: title, votes, rating)
Outputs: monthly_summary.csv, scored_reviews.csv,
         sentiment_engagement_timeline.png, sentiment_quadrant.png
"""
import sys
import nltk
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from nltk.sentiment.vader import SentimentIntensityAnalyzer

SHOW = "Pretty Tough with Maria Sharapova"
NEG, POS = "#C8553D", "#2A6F97"   # sentiment line / engagement line
GRID = "#E6E6E6"


def load(path):
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True).dt.tz_localize(None)
    df["text"] = df["text"].fillna("").astype(str)
    if "title" in df.columns:
        df["full_text"] = (df["title"].fillna("").astype(str) + ". " + df["text"]).str.strip(". ")
    else:
        df["full_text"] = df["text"]
    df["votes"] = pd.to_numeric(df.get("votes", 0), errors="coerce").fillna(0)
    df = df.dropna(subset=["date"])
    df = df[df["full_text"].str.len() > 0]
    return df.drop_duplicates(subset=["full_text", "date"]).reset_index(drop=True)


def score(df):
    nltk.download("vader_lexicon", quiet=True)
    sia = SentimentIntensityAnalyzer()
    df["compound"] = df["full_text"].apply(lambda t: sia.polarity_scores(t)["compound"])
    return df


def aggregate(df):
    df["month"] = df["date"].dt.to_period("M").dt.to_timestamp()
    m = df.groupby("month").agg(
        avg_sentiment=("compound", "mean"),
        review_count=("compound", "size"),
        total_votes=("votes", "sum"),
    ).reset_index()
    # Engagement = each review counts as 1 interaction + its helpful votes
    m["engagement"] = m["review_count"] + m["total_votes"]
    return m


def base_layout(fig, title, w, h):
    fig.update_layout(
        title=dict(text=title, x=0.02, font=dict(size=22)),
        template="simple_white", width=w, height=h,
        font=dict(family="Helvetica, Arial, sans-serif", size=14),
        legend=dict(orientation="h", y=-0.15, x=0.02),
        margin=dict(l=70, r=70, t=90, b=80),
    )


def timeline(m):
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=m["month"], y=m["avg_sentiment"], name="Avg sentiment (VADER)",
                             mode="lines+markers", line=dict(color=NEG, width=3)), secondary_y=False)
    fig.add_trace(go.Scatter(x=m["month"], y=m["engagement"], name="Engagement (reviews + votes)",
                             mode="lines+markers", line=dict(color=POS, width=3, dash="dot")), secondary_y=True)
    fig.add_hline(y=0, line=dict(color="#999", width=1, dash="dash"), secondary_y=False)
    base_layout(fig, f"{SHOW}: monthly sentiment vs engagement", 1600, 900)
    fig.update_yaxes(title_text="Avg compound score (-1 to +1)", range=[-1, 1],
                     gridcolor=GRID, secondary_y=False)
    fig.update_yaxes(title_text="Total engagement", showgrid=False, secondary_y=True)
    fig.update_xaxes(gridcolor=GRID)
    return fig


def quadrant(m):
    x_mid, y_mid = m["engagement"].median(), m["avg_sentiment"].median()
    fig = go.Figure(go.Scatter(
        x=m["engagement"], y=m["avg_sentiment"], mode="markers+text",
        text=m["month"].dt.strftime("%b %y"), textposition="top center",
        marker=dict(size=14, color=m["avg_sentiment"], colorscale="RdBu", cmin=-1, cmax=1,
                    line=dict(color="white", width=1)),
    ))
    fig.add_vline(x=x_mid, line=dict(color="#999", dash="dash"))
    fig.add_hline(y=y_mid, line=dict(color="#999", dash="dash"))
    xmax, xmin = m["engagement"].max(), m["engagement"].min()
    ymax, ymin = m["avg_sentiment"].max(), m["avg_sentiment"].min()
    labels = [(xmax, ymax, "Loved & loud", "right"), (xmin, ymax, "Loved, quiet", "left"),
              (xmax, ymin, "Divisive & loud", "right"), (xmin, ymin, "Cool & quiet", "left")]
    for x, y, t, anchor in labels:
        fig.add_annotation(x=x, y=y, text=f"<b>{t}</b>", showarrow=False,
                           xanchor=anchor, font=dict(size=15, color="#555"))
    base_layout(fig, f"{SHOW}: engagement vs sentiment by month (lines = medians)", 1400, 1000)
    fig.update_xaxes(title_text="Monthly engagement (reviews + votes)", gridcolor=GRID)
    fig.update_yaxes(title_text="Avg compound score", gridcolor=GRID)
    return fig


def main(path):
    df = score(load(path))
    m = aggregate(df)
    df.drop(columns=["month"]).to_csv("scored_reviews.csv", index=False)
    m.to_csv("monthly_summary.csv", index=False)
    print(f"{len(df)} reviews across {len(m)} months; overall mean compound = {df['compound'].mean():.3f}")
    for name, fig in [("sentiment_engagement_timeline", timeline(m)), ("sentiment_quadrant", quadrant(m))]:
        fig.write_image(f"{name}.png", scale=2)   # 2x for crisp Substack rendering
        print("wrote", f"{name}.png")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "reviews.csv")
