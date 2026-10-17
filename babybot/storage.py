from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

PREFERENCES = ("恐龙", "小动物", "森林", "太空", "海洋", "汽车", "音乐", "画画", "兔子", "猫咪")


class Store:
    def __init__(self, settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(settings.data_dir, 0o700)
        path = settings.data_dir / "family.sqlite3"
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS auth (token TEXT PRIMARY KEY, expires REAL NOT NULL, parent_until REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, seconds REAL NOT NULL DEFAULT 0);
        """)

    def get(self, key, default):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def config(self):
        return self.get("config", {"session_minutes": self.settings.session_minutes, "daily_minutes": self.settings.daily_minutes, "vad_silence_ms": 1400, "allowed_start_hour": 0, "allowed_end_hour": 24})

    def memory(self):
        return self.get("memory", {"enabled": False, "preferences": []})

    def day(self):
        return datetime.now(ZoneInfo(self.settings.timezone)).date().isoformat()

    def used(self):
        row = self.db.execute("SELECT seconds FROM usage WHERE day=?", (self.day(),)).fetchone()
        return row[0] if row else 0.0

    def charge(self, seconds):
        self.db.execute("INSERT INTO usage(day,seconds) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET seconds=seconds+excluded.seconds", (self.day(), max(0, seconds)))
        self.db.commit()

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def login(self):
        token = secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM auth WHERE expires<?", (time.time(),))
        self.db.execute("INSERT INTO auth VALUES(?,?,0)", (self.digest(token), time.time() + 86400))
        self.db.commit()
        return token

    def auth(self, token):
        if not token:
            return None
        row = self.db.execute("SELECT expires,parent_until FROM auth WHERE token=?", (self.digest(token),)).fetchone()
        if not row or row[0] <= time.time():
            return None
        return "parent" if row[1] > time.time() else "child"

    def elevate(self, token):
        self.db.execute("UPDATE auth SET parent_until=? WHERE token=?", (time.time() + 600, self.digest(token)))
        self.db.commit()

    def logout(self, token):
        self.db.execute("DELETE FROM auth WHERE token=?", (self.digest(token),))
        self.db.commit()

    def revoke_others(self, token):
        self.db.execute("DELETE FROM auth WHERE token<>?", (self.digest(token),))
        self.db.commit()

    def close(self):
        self.db.close()
