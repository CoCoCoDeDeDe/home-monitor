"""事件落盘：SQLite 单文件存储，供看板历史事件查询。

选型依据（design.md）：事件量小（每天数百条）+ 单写者 + 结构化数据，
SQLite 零服务零依赖；标准 SQL 保留向 PostgreSQL 平迁的路径。
二进制（将来事件快照/录像）不进库，只存文件路径。
"""

import json
import sqlite3
import threading
import time

DDL = """
CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      REAL NOT NULL,
  type    TEXT NOT NULL,   -- 物模型节点类型（contact/camera...）
  node    TEXT NOT NULL,   -- 节点 id（不带类型前缀）
  kind    TEXT NOT NULL,   -- state / status / detection...
  payload TEXT NOT NULL    -- JSON
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_node ON events(node, ts);
"""


class EventStore:
    """线程安全：MQTT 回调在 paho 线程、查询在 FastAPI 线程。"""

    def __init__(self, path: str):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(DDL)
        self._lock = threading.Lock()

    def record(self, ntype: str, node: str, kind: str, payload: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts, type, node, kind, payload) VALUES (?,?,?,?,?)",
                (time.time(), ntype, node, kind,
                 json.dumps(payload, ensure_ascii=False)))
            self._db.commit()

    def query(self, limit: int = 50, node: str | None = None) -> list[dict]:
        sql = "SELECT id, ts, type, node, kind, payload FROM events"
        args: list = []
        if node:
            sql += " WHERE node = ?"
            args.append(node)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [{"id": r[0], "ts": r[1], "type": r[2], "node": r[3],
                 "kind": r[4], "payload": json.loads(r[5])} for r in rows]

    def close(self) -> None:
        self._db.close()
