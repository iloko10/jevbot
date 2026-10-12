"""SQLite log. Every tick stores (snapshot, answers, action). That triple is your calibration data."""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
  ts REAL, source TEXT, model TEXT, latency_ms REAL,
  mid REAL, action TEXT, reason TEXT, risk TEXT, risk_reasons TEXT,
  bid_px REAL, ask_px REAL, inventory REAL, equity REAL,
  snapshot TEXT, answers TEXT
);
CREATE INDEX IF NOT EXISTS ix_dec_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS fills (
  ts REAL, side TEXT, px REAL, sz REAL, fee REAL, maker INTEGER, mid REAL
);
CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, detail TEXT);
"""


class Store:
    def __init__(self, path, keep_days=3):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path, self.keep_days = path, keep_days
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._last_prune = 0.0

    def decision(self, row: dict):
        with self.lock:
            self.db.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["ts"], row["source"], row["model"], row["latency_ms"], row["mid"], row["action"],
                 row["reason"], row["risk"], json.dumps(row["risk_reasons"]), row["bid_px"], row["ask_px"],
                 row["inventory"], row["equity"],
                 json.dumps(row["snapshot"], separators=(",", ":")),
                 json.dumps(row["answers"], separators=(",", ":"))))
            self.db.commit()
        self._maybe_prune(row["ts"])

    def fill(self, f):
        with self.lock:
            self.db.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?)",
                            (f.ts, f.side, f.px, f.sz, f.fee, int(f.maker), f.mid_at_fill))
            self.db.commit()

    def event(self, kind, detail=""):
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?,?,?)", (time.time(), kind, str(detail)))
            self.db.commit()

    def _maybe_prune(self, now):
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        cut = now - self.keep_days * 86400
        with self.lock:
            self.db.execute("DELETE FROM decisions WHERE ts < ?", (cut,))
            self.db.commit()


def reader(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db
