"""Load a backup made by backup_export.py into an EMPTY database (Turso or local SQLite, whichever the environment points to).

Usage:  python restore_backup.py --from backup/pretty_tough_backup_2026-10-10 [--force]

Refuses to run if the database already holds episodes, unless --force (existing rows with the same key are then overwritten).
Restored comments have no text (it was never exported); their tags, scores and likes are all back. Episodes keep their
status, so finished episodes are not fetched again.
"""
import argparse
import os

import pandas as pd

import store


def load(db, path, table):
    df = pd.read_csv(os.path.join(path, f"{table}.csv"), dtype=object).astype(object)
    df = df.where(df.notna(), None)
    if df.empty:
        return 0
    cols = list(df.columns)
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    db.executemany(sql, [tuple(None if v is None else str(v) for v in r) for r in df.itertuples(index=False)])
    return len(df)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    db = store.connect()
    if db.execute("SELECT count(*) AS n FROM episodes")[0]["n"] and not a.force:
        raise SystemExit("The database already has episodes. Restore into an empty database, or pass --force to overwrite matching rows.")
    for table in ("episodes", "comments", "weekly_metrics"):
        print(f"{table}: restored {load(db, a.src, table)} row(s)")


if __name__ == "__main__":
    main()
