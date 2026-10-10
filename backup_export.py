"""Export everything in the database to CSV files, as a backup you keep yourself.

Usage:  python backup_export.py [--out backup]

Writes <out>/pretty_tough_backup_<date>/ with episodes.csv, comments.csv, weekly_metrics.csv and manifest.json.
What is NOT in it: raw comment text (never exported) and the YouTube comment ids (replaced by a one-way hash, so rows stay
unique and can be restored but cannot be traced back to a comment). Everything else, tags, scores, likes, keywords and
requested-guest tallies, is exported. Restore into an empty database with restore_backup.py.
"""
import argparse
import hashlib
import json
import os
from datetime import date

import pandas as pd

import store

PAGE = 5000


def hashed(video_id, key):
    return hashlib.sha256(f"{video_id}|{key}".encode()).hexdigest()[:24]


def export(out):
    db = store.connect()
    folder = os.path.join(out, f"pretty_tough_backup_{date.today().isoformat()}")
    os.makedirs(folder, exist_ok=True)
    counts = {}
    ep = pd.DataFrame(db.execute("SELECT * FROM episodes ORDER BY published, video_id"))
    ep.to_csv(os.path.join(folder, "episodes.csv"), index=False)
    counts["episodes"] = len(ep)
    wm = pd.DataFrame(db.execute("SELECT * FROM weekly_metrics ORDER BY run_date"))
    wm.to_csv(os.path.join(folder, "weekly_metrics.csv"), index=False)
    counts["weekly_metrics"] = len(wm)
    cols = [r["name"] for r in db.execute("PRAGMA table_info(comments)") if r["name"] != "text"]
    rows, offset = [], 0
    while True:
        page = db.execute(f"SELECT {', '.join(cols)} FROM comments ORDER BY video_id, comment_key LIMIT ? OFFSET ?", (PAGE, offset))
        rows += page
        if len(page) < PAGE:
            break
        offset += PAGE
    cm = pd.DataFrame(rows, columns=cols)
    cm["comment_key"] = [hashed(v, k) for v, k in zip(cm["video_id"], cm["comment_key"])]
    cm.to_csv(os.path.join(folder, "comments.csv"), index=False)
    counts["comments"] = len(cm)
    manifest = {"exported": date.today().isoformat(), "counts": counts, "comment_columns": cols,
                "rules_versions": cm["rules_version"].fillna("pre-versioning").value_counts().to_dict() if len(cm) else {}}
    json.dump(manifest, open(os.path.join(folder, "manifest.json"), "w"), indent=1)
    print(f"Backup written to {folder}: {counts}")
    return folder


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="backup")
    export(ap.parse_args().out)
