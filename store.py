"""Storage layer: Turso (libSQL) over its HTTP API, with a local SQLite fallback.

Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN to use Turso. Without them, a local
file (pretty_tough.db) is used, which is handy for testing.
Using the HTTP API means no native libSQL package is needed in CI.
"""
import os
import sqlite3
import requests

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS episodes (
        video_id TEXT PRIMARY KEY, video_url TEXT, published TEXT, guest TEXT,
        guest_aliases TEXT, guest_tier INTEGER, status TEXT DEFAULT 'pending',
        n_comments INTEGER, snapshot_at TEXT, views INTEGER, video_likes INTEGER, video_comments INTEGER,
        views_at TEXT, authors INTEGER, keywords TEXT, requested TEXT, alias_suggestions TEXT)""",
    """CREATE TABLE IF NOT EXISTS comments (
        video_id TEXT, comment_key TEXT, text TEXT, likes INTEGER, is_reply INTEGER,
        compound REAL, about TEXT, fetched_at TEXT, rules_version TEXT, focus TEXT, work TEXT, topic TEXT,
        posted_at TEXT, script TEXT, words INTEGER, replies INTEGER, mode TEXT, PRIMARY KEY (video_id, comment_key))""",
    """CREATE TABLE IF NOT EXISTS weekly_metrics (
        run_date TEXT PRIMARY KEY, n_episodes INTEGER, mean_gap_all REAL, ci_lo REAL,
        ci_hi REAL, mean_gap_likes REAL, sign_p REAL, maria_ahead INTEGER)""",
]


class TursoHTTP:
    CHUNK = 200

    def __init__(self, url, token):
        self.base = url.replace("libsql://", "https://").rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    @staticmethod
    def _arg(v):
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "integer", "value": str(int(v))}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        return {"type": "text", "value": str(v)}

    @staticmethod
    def _val(cell):
        t, v = cell.get("type"), cell.get("value")
        if t == "null":
            return None
        if t == "integer":
            return int(v)
        if t == "float":
            return float(v)
        return v

    def _send(self, stmts):
        reqs = [{"type": "execute", "stmt": {"sql": s, "args": [self._arg(a) for a in args]}}
                for s, args in stmts]
        reqs.append({"type": "close"})
        r = requests.post(self.base + "/v2/pipeline", json={"requests": reqs},
                          headers=self.headers, timeout=60)
        r.raise_for_status()
        results = r.json()["results"][:-1]
        for res in results:
            if res["type"] == "error":
                raise RuntimeError(f"Turso error: {res['error']['message']}")
        return [res["response"]["result"] for res in results]

    def execute(self, sql, args=()):
        res = self._send([(sql, args)])[0]
        cols = [c["name"] for c in res["cols"]]
        return [dict(zip(cols, [self._val(c) for c in row])) for row in res["rows"]]

    def executemany(self, sql, rows):
        rows = list(rows)
        for i in range(0, len(rows), self.CHUNK):
            chunk = rows[i:i + self.CHUNK]
            stmts = [("BEGIN", ())] + [(sql, r) for r in chunk] + [("COMMIT", ())]
            self._send(stmts)


class LocalSQLite:
    def __init__(self, path="pretty_tough.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def execute(self, sql, args=()):
        cur = self.conn.execute(sql, tuple(args))
        rows = [dict(r) for r in cur.fetchall()]
        self.conn.commit()
        return rows

    def executemany(self, sql, rows):
        self.conn.executemany(sql, [tuple(r) for r in rows])
        self.conn.commit()


def connect():
    url, token = os.environ.get("TURSO_DATABASE_URL"), os.environ.get("TURSO_AUTH_TOKEN")
    if url and token:
        db, where = TursoHTTP(url, token), "Turso"
    else:
        db, where = LocalSQLite(os.environ.get("LOCAL_DB", "pretty_tough.db")), "local SQLite (no Turso env vars set)"
    for stmt in SCHEMA:
        db.execute(stmt)
    # Databases created before these columns existed: add them (old rows stay NULL).
    NEW = {"comments": [("rules_version", "TEXT"), ("focus", "TEXT"), ("work", "TEXT"), ("topic", "TEXT"), ("posted_at", "TEXT"),
                        ("script", "TEXT"), ("words", "INTEGER"), ("replies", "INTEGER"), ("mode", "TEXT")],
           "episodes": [("views", "INTEGER"), ("video_likes", "INTEGER"), ("video_comments", "INTEGER"),
                        ("views_at", "TEXT"), ("authors", "INTEGER"), ("keywords", "TEXT"), ("requested", "TEXT"), ("alias_suggestions", "TEXT")]}
    for table, cols in NEW.items():
        try:
            have = [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]
        except Exception:
            have = []   # PRAGMA not answered: try the ALTERs below, a duplicate-column error just means it exists
        for col, typ in cols:
            if col not in have:
                try:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    print(f"Added {table}.{col} column")
                except Exception as ex:
                    if "duplicate column" not in str(ex).lower():
                        raise
    print("Storage:", where)
    return db
