"""节点类型档案（Node Type Profile）：类型知识数据驱动。

一个 <type>.json 同时驱动：看板渲染（dashboard 段）、HA discovery 代发（ha 段）、
将来 payload 校验与告警级别。新增节点类型 = 丢一个档案文件，代码零改动。
"""

import json
import os


def load_profiles(directory: str) -> dict[str, dict]:
    """加载目录下全部档案，按 type 索引；目录不存在返回空。"""
    profiles: dict[str, dict] = {}
    if not os.path.isdir(directory):
        return profiles
    for fn in sorted(os.listdir(directory)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, fn), encoding="utf-8") as f:
                p = json.load(f)
        except (OSError, ValueError) as e:
            print(f"[profiles] 档案 {fn} 加载失败: {e}", flush=True)
            continue
        if p.get("type"):
            profiles[p["type"]] = p
    print(f"[profiles] 已加载 {len(profiles)} 个类型档案: {list(profiles)}", flush=True)
    return profiles
