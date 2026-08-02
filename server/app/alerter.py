"""Server酱 Turbo 发送（告警块的发送通道；冷却逻辑已搬入 blocks.AlertBlock）。"""

import requests


def send_sct(sendkey: str, title: str, desp: str) -> None:
    """无 key（开发/测试）打日志代替推送；发送失败打日志不重试（告警不阻塞数据流）。"""
    if not sendkey:
        print(f"[alert] (no sendkey) {title}: {desp}", flush=True)
        return
    try:
        resp = requests.post(
            f"https://sctapi.ftqq.com/{sendkey}.send",
            data={"title": title, "desp": desp},
            timeout=10,
        )
        resp.raise_for_status()
        print(f"[alert] 已推送 Server酱: {title}", flush=True)
    except Exception as e:
        print(f"[alert] 推送失败 {title}: {e}", flush=True)
