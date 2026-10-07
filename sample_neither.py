"""Write a random sample of stored 'Neither' comments to a CSV so you can read them and decide on new categories.

Usage:  python sample_neither.py [--n 150] [--out neither_sample.csv]

Only comments whose text is still stored (under 28 days old) can be sampled. The file holds raw comment text
(no usernames), so it is written to a file and never printed to the log; in GitHub Actions it is a short-lived
artifact. Delete it after reading.
"""
import argparse
import csv

import store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--out", default="neither_sample.csv")
    args = ap.parse_args()
    db = store.connect()
    rows = db.execute("""SELECT c.video_id, e.guest, c.likes, c.compound, c.text
                         FROM comments c JOIN episodes e USING (video_id)
                         WHERE c.about='Neither' AND c.text IS NOT NULL
                         ORDER BY random() LIMIT ?""", (args.n,))
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["video_id", "guest", "likes", "compound", "text", "your_label"])
        for r in rows:
            w.writerow([r["video_id"], r["guest"], r["likes"], round(r["compound"], 3), r["text"], ""])
    print(f"Wrote {len(rows)} sampled Neither comments to {args.out} (text not shown in the log)")


if __name__ == "__main__":
    main()
