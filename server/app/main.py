"""home-monitor 服务端：MQTT 消费 / 流转发 / 检测 / 告警 / Web。

功能规划见 docs/design.md。当前能力：
- contact-node 开门事件 → 冷却防抖 → Server酱
- 节点在线状态跟踪（LWT），GET /nodes 查看
"""

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from alerter import Alerter
from mqtt_client import start_mqtt

# 节点状态表：{node: {"status": online/offline, "state": open/closed, "ts": 时间戳}}
nodes: dict[str, dict] = {}


def _on_state(node: str, state: str, cached: bool, alerter: Alerter) -> None:
    node = node.removeprefix("contact-")  # payload 带前缀、topic 不带，统一键名
    entry = nodes.setdefault(node, {})
    entry["state"] = state
    entry["ts"] = time.time()
    if state == "open":
        alerter.alert_open(node)


def _on_status(node: str, status: str) -> None:
    node = node.removeprefix("contact-")
    entry = nodes.setdefault(node, {})
    entry["status"] = status
    entry["ts"] = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    alerter = Alerter(
        sendkey=os.getenv("SCT_SENDKEY", ""),
        cooldown_seconds=int(os.getenv("ALERT_COOLDOWN_SECONDS", "60")),
    )
    client = start_mqtt(
        host=os.getenv("MQTT_HOST", "localhost"),
        port=int(os.getenv("MQTT_PORT", "1883")),
        user=os.getenv("MQTT_USER", ""),
        password=os.getenv("MQTT_PASS", ""),
        on_state=lambda n, s, c: _on_state(n, s, c, alerter),
        on_status=_on_status,
    )
    app.state.alerter = alerter
    app.state.mqtt = client
    yield
    client.loop_stop()
    client.disconnect()


app = FastAPI(title="home-monitor", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "mqtt_connected": app.state.mqtt.is_connected()}


@app.get("/nodes")
def list_nodes() -> dict:
    """节点在线状态表（LWT 驱动）：status=online/offline，state=open/closed。"""
    return nodes
