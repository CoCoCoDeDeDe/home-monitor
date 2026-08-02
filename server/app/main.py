"""home-monitor 服务端：MQTT 消费 / 流转发 / 检测 / 告警 / Web。

功能规划见 docs/design.md。当前能力：
- 节点状态事件 → 事件落盘 + 图引擎求值（翻译/显示/告警是图输出，Issue #27）
- 节点在线状态跟踪（LWT）+ 健康指标（rssi/uptime），GET /nodes 查看
- 事件落盘 SQLite（events.py），GET /api/events 查历史（重启不丢）
- 看板配置表单 = 节点标准子图的投影（GET/PUT /api/nodes/{id}/config）
"""

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from discovery import discovery_messages
from events import EventStore
from graph_engine import GraphError
from graph_service import GraphService
from graph_store import GraphStore
from mqtt_client import start_mqtt
from profiles import load_profiles

# 节点状态表：{node: {"type","status","state","rssi","uptime","ts"}}（引擎外元信息）
nodes: dict[str, dict] = {}
# 物模型类型档案（lifespan 启动时加载；模板文件也在其中）
PROFILES: dict[str, dict] = {}


def _ensure_node(ntype: str, node: str, discover) -> dict:
    """状态表建条目；新节点且类型有档案时触发 HA discovery 代发。"""
    if node not in nodes:
        entry = nodes.setdefault(node, {})
        entry["type"] = ntype
        discover(ntype, node)
        return entry
    return nodes[node]


def _on_state(ntype: str, node: str, state, cached: bool, retained: bool,
              gsvc: GraphService, store: EventStore, discover) -> None:
    entry = _ensure_node(ntype, node, discover)
    entry["state"] = state
    entry["ts"] = time.time()
    if retained:
        return  # retain 重放只更新状态表，不记事件、不喂图、不告警
    store.record(ntype, node, "state", {"state": state, "cached": cached})
    gsvc.feed_state(ntype, node, state, entry["ts"])  # 翻译/显示/告警全在图里


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
    db_path = os.getenv("EVENTS_DB", "/data/events.db")
    store = EventStore(db_path)
    PROFILES.update(load_profiles(os.getenv("PROFILES_DIR", "/srv/nodetypes")))

    def discover(ntype: str, node: str) -> None:
        """新节点出现：按档案代发 HA discovery config（retain，幂等）。"""
        profile = PROFILES.get(ntype)
        if not profile:
            return
        for topic, payload in discovery_messages(ntype, node, profile):
            client.publish(topic, payload, retain=True)
        print(f"[discovery] {ntype}/{node} config 已代发", flush=True)

    # publish 闭包延迟绑定 client（io_out 块用；client 在下方 start_mqtt 才赋值）
    def publish(topic: str, payload: str):
        client.publish(topic, payload)

    gsvc = GraphService(GraphStore(db_path), PROFILES,
                        os.getenv("SCT_SENDKEY", ""), publish)

    client = start_mqtt(
        host=os.getenv("MQTT_HOST", "localhost"),
        port=int(os.getenv("MQTT_PORT", "1883")),
        user=os.getenv("MQTT_USER", ""),
        password=os.getenv("MQTT_PASS", ""),
        on_state=lambda t, n, s, c, r: _on_state(t, n, s, c, r, gsvc, store, discover),
        on_status=lambda t, n, s, r: _on_status(t, n, s, r, store, discover),
        on_health=lambda t, n, r, u: _on_health(t, n, r, u, discover),
    )
    app.state.mqtt = client
    app.state.events = store
    app.state.gsvc = gsvc
    yield
    client.loop_stop()
    client.disconnect()
    store.close()


app = FastAPI(title="home-monitor", lifespan=lifespan)

_STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.get("/")
def dashboard():
    """监控看板：图输出驱动的单页（无框架，5s 轮询 /nodes + /api/events）。"""
    return FileResponse(os.path.join(_STATIC, "index.html"))


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mqtt_connected": app.state.mqtt.is_connected()}


@app.get("/nodes")
def list_nodes() -> dict:
    """节点状态表 + 图输出：display=显示点输出（{text,level,alias}），
    map=翻译表（看板事件列表把历史原始事件翻译成当前语义文案）。"""
    return {nid: {**entry,
                  "display": app.state.gsvc.display_of(nid),
                  "map": app.state.gsvc.translate_map_of(nid)}
            for nid, entry in nodes.items()}


class NodeForm(BaseModel):
    alias: str = ""
    map: dict = {}
    cooldown: int = 60


def _ntype_of(node: str) -> str:
    """节点固件类型：状态表里有就用；未知节点按 collision（当前唯一固件类型）。"""
    return nodes.get(node, {}).get("type", "collision")


@app.get("/api/nodes/{node}/config")
def get_node_config(node: str) -> dict:
    """表单投影读（Issue #27）：alias/map/cooldown，无图节点按默认值投影。"""
    return app.state.gsvc.projection(_ntype_of(node), node)


@app.put("/api/nodes/{node}/config")
def set_node_config(node: str, form: NodeForm) -> dict:
    """表单投影写：重建节点标准子图，热加载生效，无需改固件。"""
    try:
        return app.state.gsvc.apply_form(_ntype_of(node), node,
                                         form.alias.strip(), form.map, form.cooldown)
    except GraphError as e:
        raise HTTPException(400, f"非法图: {e}")


@app.get("/api/events")
def list_events(limit: int = 50, node: str | None = None) -> list[dict]:
    """事件时间线（SQLite 落盘，重启不丢）：按时间倒序，可按节点过滤。"""
    return app.state.events.query(limit=min(limit, 200), node=node)


@app.get("/api/profiles")
def list_profiles() -> dict:
    """类型档案与模板（collision=IO 词汇+HA discovery；contact/presence=表单预填模板）。"""
    return PROFILES
