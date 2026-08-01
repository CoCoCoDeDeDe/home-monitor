"""HA MQTT Discovery config 代发：按类型档案为节点生成 config 消息。

设计依据（design.md 物模型设计）：config 由服务端代发（retain）而非固件发——
HA 不关心来源，固件保持零改动；将来接入真 Home Assistant 时这些 retain
消息直接生效，无需任何改动。
"""

import json


def discovery_messages(ntype: str, node: str, profile: dict) -> list[tuple[str, str]]:
    """生成 [(topic, payload)]，调用方负责以 retain 发布。档案无 ha 段返回空。"""
    ha = profile.get("ha")
    if not ha:
        return []
    label = profile.get("label", ntype)
    base = {
        "avty_t": f"{ntype}/{node}/status",     # LWT 驱动的可用性
        "pl_avail": "online",
        "pl_not_avail": "offline",
        "dev": {"ids": [f"{ntype}-{node}"], "name": f"{ntype}-{node}", "mf": "DIY"},
    }
    msgs = []

    comp = ha.get("component", "binary_sensor")
    uid = f"{ntype}_{node}_{comp}"
    main = {
        **base,
        "name": label,
        "uniq_id": uid,
        "stat_t": f"{ntype}/{node}/{ha.get('state_topic', 'state')}",
    }
    if ha.get("device_class"):
        main["dev_cla"] = ha["device_class"]
    if ha.get("value_template"):
        main["val_tpl"] = ha["value_template"]
    if ha.get("payload_on"):
        main["pl_on"] = ha["payload_on"]
    if ha.get("payload_off"):
        main["pl_off"] = ha["payload_off"]
    msgs.append((f"homeassistant/{comp}/{uid}/config", json.dumps(main)))

    for s in ha.get("extra_sensors", []):
        uid_s = f"{ntype}_{node}_{s['suffix']}"
        cfg = {
            **base,
            "name": f"{label} {s['name']}",
            "uniq_id": uid_s,
            "stat_t": f"{ntype}/{node}/{s['state_topic']}",
            "val_tpl": s["value_template"],
        }
        if s.get("device_class"):
            cfg["dev_cla"] = s["device_class"]
        if s.get("unit"):
            cfg["unit_of_meas"] = s["unit"]
        msgs.append((f"homeassistant/sensor/{uid_s}/config", json.dumps(cfg)))
    return msgs
