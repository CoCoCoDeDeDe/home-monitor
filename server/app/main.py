"""home-monitor 服务端：MQTT 消费 / 流转发 / 检测 / 告警 / Web。

功能规划见 docs/design.md。当前为骨架：仅健康检查。
"""

from fastapi import FastAPI

app = FastAPI(title="home-monitor")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
