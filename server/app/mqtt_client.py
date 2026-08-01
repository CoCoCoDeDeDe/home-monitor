"""MQTT 消费：订阅 contact/+/state 与 contact/+/status，事件回调。"""

import json

import paho.mqtt.client as mqtt


def start_mqtt(host: str, port: int, user: str, password: str,
               on_state, on_status) -> mqtt.Client:
    """on_state(node, state, cached)；on_status(node, status)"""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if user:
        client.username_pw_set(user, password)
    # 断线自动重连：延迟 1s~60s 递增
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    def on_connect(c, _u, _f, rc, _p=None):
        print(f"[mqtt] connected rc={rc}", flush=True)
        c.subscribe([("contact/+/state", 0), ("contact/+/status", 0),
                     ("contact/syncreq", 0)])
        # 广播就绪：有缓存事件的节点收到后才补发（避免本端未订阅时补发被丢）
        c.publish("contact/sync", "1")

    def on_message(c, _u, msg):
        if msg.topic == "contact/syncreq":  # 节点请求补发握手
            print(f"[mqtt] syncreq from {msg.payload.decode(errors='replace')}", flush=True)
            c.publish("contact/sync", "1")
            return
        parts = msg.topic.split("/")  # contact/<node>/<kind>
        if len(parts) != 3:
            return
        node, kind = parts[1], parts[2]
        if kind == "status":
            status = msg.payload.decode(errors="replace")
            print(f"[mqtt] {msg.topic} {status}", flush=True)
            on_status(node, status)
            return
        try:
            data = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            print(f"[mqtt] 非法 payload: {msg.topic}", flush=True)
            return
        print(f"[mqtt] {msg.topic} {data}", flush=True)
        on_state(data.get("node") or node,
                 data.get("state", ""),
                 bool(data.get("cached")))

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=60)
    client.loop_start()  # 后台线程跑网络循环，FastAPI 主进程不被阻塞
    return client
