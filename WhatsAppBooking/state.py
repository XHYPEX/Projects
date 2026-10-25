"""Catat message ID yang sudah diproses. Meta kadang ngirim webhook yang sama
lebih dari sekali; tanpa ini satu pesan bisa jadi dua event di kalender."""
import os
import sqlite3
import threading


class ProcessedMessages:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS processed (message_id TEXT PRIMARY KEY, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def claim(self, message_id: str) -> bool:
        """True kalau message ini belum pernah diproses (dan sekarang ditandai)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO processed (message_id) VALUES (?)", (message_id,)
            )
            self._conn.commit()
            return cur.rowcount == 1
