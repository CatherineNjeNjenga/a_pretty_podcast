"""Write a random sample of stored comments from one category to a CSV so you can read and label them.

Usage:  python sample_neither.py [--about Neither] [--n 150] [--out comment_sample.csv]
(--about is any tag: Both, Maria, Guest, Show, Request, Pair, Unnamed, Reaction, Topic, Noise, Neither)

Only comments whose text is still stored (under 28 days old) can be sampled. The file holds raw comment text
(no usernames), so it is written to a file and never printed to the log; in GitHub Actions it is a short-lived
artifact. Delete it after reading.

For Maria, Guest and Both samples there is an extra column, your_mode. Fill your_label with who the comment is really about
(or "ok" if the tag is right) and your_mode with "address" (talks TO her: thanks, praise, "you") or "discussion" (talks ABOUT her).
"""
import argparse
import csv

import store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--about", default="Neither")
    ap.add_argument("--out", default="comment_sample.csv")
    args = ap.parse_args()
    db = store.connect()
    rows = db.execute("""SELECT c.video_id, e.guest, c.likes, c.compound, c.text
                         FROM comments c JOIN episodes e USING (video_id)
                         WHERE c.about=? AND c.text IS NOT NULL
                         ORDER BY random() LIMIT ?""", (args.about, args.n))
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        extra = ["your_mode"] if args.about in ("Maria", "Guest", "Both") else []   # address / discussion, to check the rule
        w.writerow(["video_id", "guest", "likes", "compound", "text", "your_label"] + extra)
        for r in rows:
            w.writerow([r["video_id"], r["guest"], r["likes"], round(r["compound"], 3), r["text"], ""] + [""] * len(extra))
    print(f"Wrote {len(rows)} sampled {args.about} comments to {args.out} (text not shown in the log)")


if __name__ == "__main__":
    main()
