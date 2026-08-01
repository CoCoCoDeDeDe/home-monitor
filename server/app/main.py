"""home-monitor 服务端：MQTT 消费 / 流转发 / 检测 / 告警 / Web。

功能规划见 docs/design.md。当前能力：contact-node 开门事件 → 冷却防抖 → Server酱。
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from alerter import Alerter
from mqtt_client import start_mqtt


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
        on_open=alerter.alert_open,
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
