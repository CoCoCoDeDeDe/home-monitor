"""home-monitor 服务端：MQTT 消费 / 流转发 / 检测 / 告警 / Web。

功能规划见 docs/design.md。当前能力：
- contact-node 开门事件 → 冷却防抖 → Server酱
- 节点在线状态跟踪（LWT）+ 健康指标（rssi/uptime），GET /nodes 查看
- 事件落盘 SQLite（events.py），GET /api/events 查历史（重启不丢）
"""

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from alerter import Alerter
from discovery import discovery_messages
from events import EventStore
from mqtt_client import start_mqtt
from profiles import load_profiles

# 节点状态表：{node: {"type","status","state","rssi","uptime","ts"}}
nodes: dict[str, dict] = {}
# 物模型类型档案（lifespan 启动时加载）
PROFILES: dict[str, dict] = {}


def _ensure_node(ntype: str, node: str, discover) -> dict:
    """注册表建条目；新节点且类型有档案时触发 HA discovery 代发。"""
    if node not in nodes:
        entry = nodes.setdefault(node, {})
        entry["type"] = ntype
        discover(ntype, node)
        return entry
    return nodes[node]


def _on_state(ntype: str, node: str, state: str, cached: bool, retained: bool,
              alerter: Alerter, store: EventStore, discover) -> None:
    entry = _ensure_node(ntype, node, discover)
    entry["state"] = state
    entry["ts"] = time.time()
    if retained:
        return  # retain 重放只更新注册表，不记事件、不告警
    store.record(ntype, node, "state", {"state": state, "cached": cached})
    if state == "open":
        alerter.alert_open(node)


def _on_status(ntype: str, node: str, status: str, retained: bool,
               store: EventStore, discover) -> None:
    entry = _ensure_node(ntype, node, discover)
    entry["status"] = status
    entry["ts"] = time.time()
    if not retained:
        store.record(ntype, node, "status", {"status": status})


def _on_health(ntype: str, node: str, rssi, uptime, discover) -> None:
    entry = _ensure_node(ntype, node, discover)
    entry["rssi"] = rssi
    entry["uptime"] = uptime
    entry["ts"] = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    alerter = Alerter(
        sendkey=os.getenv("SCT_SENDKEY", ""),
        cooldown_seconds=int(os.getenv("ALERT_COOLDOWN_SECONDS", "60")),
    )
    store = EventStore(os.getenv("EVENTS_DB", "/data/events.db"))
    PROFILES.update(load_profiles(os.getenv("PROFILES_DIR", "/srv/nodetypes")))

    def discover(ntype: str, node: str) -> None:
        """新节点出现：按档案代发 HA discovery config（retain，幂等）。"""
        profile = PROFILES.get(ntype)
        if not profile:
            return
        for topic, payload in discovery_messages(ntype, node, profile):
            client.publish(topic, payload, retain=True)
        print(f"[discovery] {ntype}/{node} config 已代发", flush=True)

    client = start_mqtt(
        host=os.getenv("MQTT_HOST", "localhost"),
        port=int(os.getenv("MQTT_PORT", "1883")),
        user=os.getenv("MQTT_USER", ""),
        password=os.getenv("MQTT_PASS", ""),
        on_state=lambda t, n, s, c, r: _on_state(t, n, s, c, r, alerter, store, discover),
        on_status=lambda t, n, s, r: _on_status(t, n, s, r, store, discover),
        on_health=lambda t, n, r, u: _on_health(t, n, r, u, discover),
    )
    app.state.alerter = alerter
    app.state.mqtt = client
    app.state.events = store
    yield
    client.loop_stop()
    client.disconnect()
    store.close()


app = FastAPI(title="home-monitor", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mqtt_connected": app.state.mqtt.is_connected()}


@app.get("/nodes")
def list_nodes() -> dict:
    """节点状态表：type=物模型类型，status(LWT)=online/offline，state=open/closed，rssi/uptime=健康上报。"""
    return nodes


@app.get("/api/events")
def list_events(limit: int = 50, node: str | None = None) -> list[dict]:
    """事件时间线（SQLite 落盘，重启不丢）：按时间倒序，可按节点过滤。"""
    return app.state.events.query(limit=min(limit, 200), node=node)


@app.get("/api/profiles")
def list_profiles() -> dict:
    """物模型类型档案（看板按此渲染节点卡片）。"""
    return PROFILES
