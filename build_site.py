"""Build the public results page (GitHub Pages) from the pipeline's output folder.

Usage:  python build_site.py [--out out] [--site site]

Reads out/ (written by run_weekly.py) and writes site/index.html plus the chart images, interactive charts and a few CSVs.
Only derived, episode-level files are published: never comment text, never usernames, and not the per-comment CSV
(tagged_comments.csv) or the alias audit. The wording in the CAVEATS list below is yours to edit.
"""
import argparse
import html
import json
import os
import shutil
from datetime import date

import pandas as pd

TITLE = "Pretty Tough: who do viewers talk about, the host or the guest?"
INTRO = ("A weekly check of the YouTube comments on each Pretty Tough episode (first 7 days after upload): do viewers name "
         "Maria Sharapova more often than the guest? Each comment is sorted by simple rules; no one reads individual comments here.")
CAVEATS = [
    "<b>Named is not the same as interested.</b> Many Maria comments are thanks addressed to the host. The 'discussion only' line "
    "leaves those out.",
    "Only comments that name Maria, the guest, or both go into the gap. Requests for future guests, comments about the show, "
    "'she/her' comments with no name and everything else are counted separately.",
    "Tags come from keyword rules that were checked on small hand-labelled samples; they will be wrong some of the time. "
    "Guest nicknames and misspellings are added by hand, so a guest can be under-counted.",
    "These are the episodes in the playlist, not all podcasts. Guest fame and the guest's own audience change the picture.",
]
CHARTS = [
    ("who_gets_talked_about", "Maria vs guest, episode by episode"),
    ("what_comments_are_about", "What the comments are about"),
    ("comment_categories", "All comment categories"),
    ("cumulative_gap", "The gap as episodes accumulate"),
    ("comments_per_1k_views", "Comments naming Maria vs the guest, per 1,000 views"),
    ("tone_maria_vs_guest", "Tone of comments about Maria vs the guest"),
    ("guest_fame_vs_gap", "Does guest fame change the gap?"),
]
DOWNLOADS = [("episode_summary.csv", "Per-episode summary"), ("guest_requests_table.csv", "Requested guests, by episode"),
             ("guest_requests.csv", "Requested guests, running totals"), ("cumulative.csv", "Cumulative result"),
             ("verdict.txt", "Verdict text")]
COLS = [("guest", "Guest", "t"), ("published", "Published", "t"), ("n_comments", "Comments", "i"),
        ("maria_share_all", "Maria", "p"), ("guest_share_all", "Guest", "p"), ("both_share", "Both", "p"),
        ("gap_all", "Gap", "g"), ("gap_discussion", "Gap (discussion only)", "g"),
        ("comments_per_1k_views", "Comments / 1k views", "f"), ("keywords", "Keyword candidates", "k"),
        ("requested_top", "Most requested guests", "k")]
STYLE = """
:root{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#ddd;--card:#f6f6f6;--maria:#C8553D;--guest:#1B74B8}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mut:#aaa;--line:#333;--card:#202020;--maria:#E07A62;--guest:#5AA9E6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.7rem;line-height:1.25;margin:.2em 0}h2{margin-top:2.2em;font-size:1.25rem}
.mut{color:var(--mut);font-size:.9rem}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px}
.card ul{margin:.3em 0;padding-left:1.2em}figure{margin:1.2em 0}img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:6px;background:#fff}
figcaption{font-size:.9rem;color:var(--mut)}a{color:var(--guest)}.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.88rem}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}
th:first-child,td:first-child,td.k,th.k{text-align:left}th{position:sticky;top:0;background:var(--bg)}
td.n{white-space:nowrap}.pos{color:var(--maria);font-weight:600}.neg{color:var(--guest);font-weight:600}
"""


def _read(out, name):
    p = os.path.join(out, name)
    return pd.read_csv(p) if os.path.exists(p) else None


def _fmt(v, kind):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if kind == "p":
        return f"{v:.1%}"
    if kind == "g":
        cls = "pos" if v > 0 else "neg" if v < 0 else ""
        return f'<span class="{cls}">{v * 100:+.1f} pts</span>'
    if kind == "f":
        return f"{v:.2f}"
    if kind == "i":
        return f"{int(v):,}"
    return html.escape(str(v))


def episode_table(e):
    e = e.sort_values("published") if "published" in e else e
    cols = [c for c in COLS if c[0] in e.columns]
    head = "".join(f'<th class="{"k" if k == "k" else ""}">{html.escape(n)}</th>' for _, n, k in cols)
    rows = []
    for r in e.to_dict("records"):
        rows.append("<tr>" + "".join(f'<td class="{"k" if k == "k" else "n"}">{_fmt(r.get(c), k)}</td>' for c, _, k in cols) + "</tr>")
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def requests_table(w, top=15):
    w = w.head(top)
    first, rest = w.columns[0], list(w.columns[1:])
    head = "<th>Requested guest</th>" + "".join(f"<th>{html.escape('All episodes' if c == 'total' else str(c))}</th>" for c in rest)
    body = "".join("<tr><td>" + html.escape(str(r[first])) + "</td>" + "".join(f"<td>{int(r[c])}</td>" for c in rest) + "</tr>"
                   for r in w.to_dict("records"))
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def build(out, site):
    os.makedirs(site, exist_ok=True)
    info = {}
    if os.path.exists(os.path.join(out, "run_info.json")):
        info = json.load(open(os.path.join(out, "run_info.json"), encoding="utf-8"))
    updated = info.get("run_date") or date.today().isoformat()
    parts = [f"<h1>{html.escape(TITLE)}</h1>", f'<p>{html.escape(INTRO)}</p>', f'<p class="mut">Last updated {html.escape(updated)}.'
             + (f" Episodes included: {info['n_episodes']}." if info.get("n_episodes") else "") + "</p>"]
    vp = os.path.join(out, "verdict.txt")
    if os.path.exists(vp):
        lines = [l.strip() for l in open(vp, encoding="utf-8").read().splitlines() if l.strip()]
        parts.append("<h2>The test so far</h2><div class='card'><ul>" + "".join(f"<li>{html.escape(l)}</li>" for l in lines) + "</ul></div>")
    if info.get("rules_versions") and len(info["rules_versions"]) > 1:
        parts.append("<p class='mut'>Note: some episodes were tagged under older rules (" + html.escape(", ".join(info["rules_versions"])) +
                     "), so compare them with care.</p>")
    parts.append("<h2>How to read this</h2><div class='card'><ul>" + "".join(f"<li>{c}</li>" for c in CAVEATS) + "</ul></div>")
    shown = 0
    for name, title in CHARTS:
        png = os.path.join(out, name + ".png")
        if not os.path.exists(png):
            continue
        shutil.copy(png, os.path.join(site, name + ".png"))
        link = ""
        if os.path.exists(os.path.join(out, name + ".html")):
            shutil.copy(os.path.join(out, name + ".html"), os.path.join(site, name + ".html"))
            link = f' <a href="{name}.html">Open the interactive version</a>.'
        if not shown:
            parts.append("<h2>Charts</h2>")
        shown += 1
        parts.append(f'<figure><img src="{name}.png" alt="{html.escape(title)}" loading="lazy"><figcaption>{html.escape(title)}.{link}</figcaption></figure>')
    e = _read(out, "episode_summary.csv")
    if e is not None and len(e):
        parts.append("<h2>Episode by episode</h2>" + episode_table(e) +
                     "<p class='mut'>Gap = share of all comments naming Maria minus the guest (percentage points); positive means Maria ahead. "
                     "Keyword candidates and requested guests come from the comment text while it was available.</p>")
    w = _read(out, "guest_requests_table.csv")
    if w is not None and len(w):
        parts.append("<h2>Guests viewers ask for</h2>" + requests_table(w) +
                     "<p class='mut'>Names are typed by viewers and read by simple rules, so treat the counts as approximate.</p>")
    links = [(f, d) for f, d in DOWNLOADS if os.path.exists(os.path.join(out, f))]
    for f, _ in links:
        shutil.copy(os.path.join(out, f), os.path.join(site, f))
    if links:
        parts.append("<h2>Data</h2><ul>" + "".join(f'<li><a href="{f}">{html.escape(d)}</a> <span class="mut">({f})</span></li>' for f, d in links) + "</ul>")
    parts.append("<p class='mut'>No comment text or usernames are published, only counts and shares.</p>")
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Pretty Tough comments</title><style>{STYLE}</style></head><body><main>{''.join(parts)}</main></body></html>")
    open(os.path.join(site, "index.html"), "w", encoding="utf-8").write(page)
    open(os.path.join(site, ".nojekyll"), "w").close()
    print(f"Built {site}/index.html ({shown} chart(s), {0 if e is None else len(e)} episode row(s))")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--site", default="site")
    a = ap.parse_args()
    build(a.out, a.site)
