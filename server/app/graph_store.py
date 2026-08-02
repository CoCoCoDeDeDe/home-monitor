"""图持久化：graphs 表（与 events.db 同文件）。每节点一张小图；node_id 空串 = 全局图（未来联动）。"""

import json
import sqlite3
import threading
import time

DDL = """
CREATE TABLE IF NOT EXISTS graphs (
  graph_id TEXT PRIMARY KEY,  -- 节点图 = node_id；全局图 = "global:<name>"（未来）
  node_id  TEXT NOT NULL DEFAULT '',
  json     TEXT NOT NULL,
  ts       REAL NOT NULL
);
"""


class GraphStore:
    """线程安全：API 在 FastAPI 线程，求值在 paho 线程。"""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(DDL)
        self._lock = threading.Lock()

    def save(self, node_id: str, spec: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO graphs (graph_id, node_id, json, ts) VALUES (?,?,?,?) "
                "ON CONFLICT(graph_id) DO UPDATE SET json=excluded.json, ts=excluded.ts",
                (node_id, node_id, json.dumps(spec, ensure_ascii=False), time.time()))
            self._db.commit()

    def load(self, node_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT json FROM graphs WHERE graph_id = ?", (node_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def load_all(self) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT graph_id, json FROM graphs").fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def delete(self, node_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM graphs WHERE graph_id = ?", (node_id,))
            self._db.commit()

    def close(self) -> None:
        self._db.close()
