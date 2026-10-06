import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time TEXT, kind TEXT, symbol TEXT, payload TEXT);
        """)
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, allow_nan=False)))
        self.db.commit()

    def log(self, kind, symbol="", **payload):
        self.db.execute("INSERT INTO events(time,kind,symbol,payload) VALUES (?,?,?,?)", (datetime.now(timezone.utc).isoformat(), kind, symbol, json.dumps(payload, allow_nan=False)))
        self.db.commit()

    def halt(self, reason):
        self.set("halt", reason)
        self.log("HALT", reason=reason)
