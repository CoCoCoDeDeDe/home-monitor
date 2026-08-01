"""告警器：冷却防抖 + Server酱 Turbo 推送。"""

import time

import requests


class Alerter:
    def __init__(self, sendkey: str, cooldown_seconds: int = 60):
        self.sendkey = sendkey
        self.cooldown = cooldown_seconds
        self._last_sent: dict[str, float] = {}

    def alert_open(self, node: str) -> bool:
        """门窗打开事件。冷却期内同一节点只报一次。返回是否真的触发了推送。"""
        now = time.time()
        if now - self._last_sent.get(node, 0) < self.cooldown:
            print(f"[alert] 冷却期内跳过: {node}", flush=True)
            return False
        self._last_sent[node] = now

        title = "门窗打开告警"
        desp = f"传感器 {node} 检测到打开，时间 {time.strftime('%H:%M:%S')}"
        if not self.sendkey:
            # 未配置 SendKey（开发/测试）：打日志代替推送
            print(f"[alert] (no sendkey) {title}: {desp}", flush=True)
            return True
        resp = requests.post(
            f"https://sctapi.ftqq.com/{self.sendkey}.send",
            data={"title": title, "desp": desp},
            timeout=10,
        )
        resp.raise_for_status()
        print(f"[alert] 已推送 Server酱: {node}", flush=True)
        return True
