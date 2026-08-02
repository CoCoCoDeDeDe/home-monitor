"""MQTT 消费：订阅 <type>/<id>/{state,status,health}（物模型通用）与 +/syncreq，事件回调。"""

import json

import paho.mqtt.client as mqtt


def start_mqtt(host: str, port: int, user: str, password: str,
               on_state, on_status, on_health) -> mqtt.Client:
    """回调均带物模型类型前缀：
    on_state(ntype, node, state, cached, retained)；on_status(ntype, node, status, retained)；
    on_health(ntype, node, rssi, uptime)。node 为 topic 中的 id（不带前缀）。
    retained=True 表示 broker retain 重放（本端重连时的旧消息），不应记为新事件。"""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if user:
        client.username_pw_set(user, password)
    # 断线自动重连：延迟 1s~60s 递增
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    def on_connect(c, _u, _f, rc, _p=None):
        print(f"[mqtt] connected rc={rc}", flush=True)
        # 物模型通用化：type 段用通配符，任何新节点类型自动被消费；
        # 补发握手同理（<type>/syncreq → 回 <type>/sync）
        c.subscribe([("+/+/state", 0), ("+/+/status", 0),
                     ("+/+/health", 0), ("+/syncreq", 0)])
        # 广播就绪：有缓存事件的节点收到后才补发（避免本端未订阅时补发被丢）。
        # 已知类型各发一份（节点类型少；新增固件类型时在此补充）
        for ntype in ("contact", "collision"):
            c.publish(f"{ntype}/sync", "1")

    def on_message(c, _u, msg):
        parts = msg.topic.split("/")
        if len(parts) == 2 and parts[1] == "syncreq":  # 节点请求补发握手
            print(f"[mqtt] syncreq from {msg.payload.decode(errors='replace')}", flush=True)
            c.publish(f"{parts[0]}/sync", "1")
            return
        if len(parts) != 3:  # <type>/<node>/<kind>
            return
        ntype, node, kind = parts
        if kind == "status":
            status = msg.payload.decode(errors="replace")
            print(f"[mqtt] {msg.topic} {status}", flush=True)
            on_status(ntype, node, status, bool(msg.retain))
            return
        try:
            data = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            print(f"[mqtt] 非法 payload: {msg.topic}", flush=True)
            return
        if kind == "health":
            on_health(ntype, node, data.get("rssi"), data.get("uptime"))
            return
        print(f"[mqtt] {msg.topic} {data}", flush=True)
        on_state(ntype, node,
                 data.get("state"),  # 布尔固件词表：1/0 原样传递（旧固件为字符串词）
                 bool(data.get("cached")),
                 bool(msg.retain))

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=60)
    client.loop_start()  # 后台线程跑网络循环，FastAPI 主进程不被阻塞
    return client
