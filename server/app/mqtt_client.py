"""MQTT 消费：订阅 contact/+/state，开门事件回调告警器。"""

import json

import paho.mqtt.client as mqtt


def start_mqtt(host: str, port: int, user: str, password: str, on_open) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if user:
        client.username_pw_set(user, password)
    # 断线自动重连：延迟 1s~60s 递增
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    def on_connect(c, _u, _f, rc, _p=None):
        print(f"[mqtt] connected rc={rc}", flush=True)
        c.subscribe("contact/+/state")

    def on_message(_c, _u, msg):
        try:
            data = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            print(f"[mqtt] 非法 payload: {msg.topic}", flush=True)
            return
        print(f"[mqtt] {msg.topic} {data}", flush=True)
        if data.get("state") == "open":
            on_open(data.get("node") or msg.topic.split("/")[1])

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=60)
    client.loop_start()  # 后台线程跑网络循环，FastAPI 主进程不被阻塞
    return client
