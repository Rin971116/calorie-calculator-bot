"""
對話狀態管理（SQLite 版本）。

用途：記住使用者當前的多輪狀態，例如：
  - "confirm"：正在確認某張食物照片的待答問題
  - "weight" ：剛按下「上傳體重」，正在等待使用者輸入純數字體重

改用 SQLite 的好處（相較記憶體版）：
  伺服器休眠喚醒、當機重啟時，半途對話不會遺失。
  注意：Render 免費方案的磁碟是臨時的，重新「部署（deploy）」時
  SQLite 檔案會被清空——但對話暫存本來就是短期資料，這是可接受的取捨。

設計成可替換：欄位與方法與原記憶體版一致，app.py 不需大改。
逾時或找不到狀態時一律回傳 None，交由 app.py 做「請重新上傳照片」的例外處理。
"""
import os
import json
import time
import sqlite3
import threading

SESSION_TTL = 15 * 60  # 15 分鐘
DB_PATH = os.environ.get("SESSION_DB_PATH", "sessions.db")


class SessionStore:
    def __init__(self, db_path=DB_PATH, ttl=SESSION_TTL):
        self._ttl = ttl
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self):
        # 每次操作開一條連線，避免多執行緒共用同一連線的問題
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self):
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id     TEXT PRIMARY KEY,
                    data        TEXT NOT NULL,
                    updated_at  REAL NOT NULL
                )
                """
            )

    def set(self, user_id, data):
        with self._lock, self._conn() as conn:
            conn.execute(
                "REPLACE INTO sessions (user_id, data, updated_at) VALUES (?, ?, ?)",
                (user_id, json.dumps(data, ensure_ascii=False), time.time()),
            )

    def get(self, user_id):
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT data, updated_at FROM sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if not row:
                return None
            data_str, updated_at = row
            if time.time() - updated_at > self._ttl:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                return None
            return json.loads(data_str)

    def clear(self, user_id):
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def cleanup(self):
        cutoff = time.time() - self._ttl
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))


# 全域單例
session_store = SessionStore()
