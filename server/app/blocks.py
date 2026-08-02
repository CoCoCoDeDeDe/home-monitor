"""图块实现（Issue #27 图引擎）：5 种块 + BLOCKS 注册表。

块接口：类属性 kind/inputs/outputs + 静态 evaluate(params, ins, state, now, ctx)。
state 是引擎持有的块私有 dict（跨求值存活，如冷却时间、上一 raw）。
ctx 由 GraphService 注入：sendkey/node/alias/publish。
值统一 {"raw","text","level","ts"}；io_in 入口只带 raw/ts（engine.feed 构造）。
"""

import json
import time

from alerter import send_sct


class IoInBlock:
    """IO 输入点：MQTT 状态由此进图。不参与 evaluate——引擎 feed 时直接设置其输出。"""

    kind = "io_in"
    inputs = ()
    outputs = ("state",)

    @staticmethod
    def evaluate(params, ins, state, now, ctx):
        return None


class IoOutBlock:
    """IO 输出点：汇，把收到的值 publish 到 params.topic（设备互控地基；固件订阅端未来接）。"""

    kind = "io_out"
    inputs = ("cmd",)
    outputs = ()

    @staticmethod
    def evaluate(params, ins, state, now, ctx):
        v = ins.get("cmd")
        if v is None:
            return None
        publish = ctx.get("publish")
        if publish:
            publish(params["topic"], json.dumps(v, ensure_ascii=False))
        return None


class TranslateBlock:
    """语义翻译块：原始值 → 用户配置文案+级别。out=当前显示值，event=raw 变化沿。"""

    kind = "translate"
    inputs = ("in",)
    outputs = ("out", "event")

    @staticmethod
    def evaluate(params, ins, state, now, ctx):
        v = ins.get("in")
        if v is None:
            return None
        raw = v.get("raw", "")
        m = (params.get("map") or {}).get(raw)
        if m is None:  # 未知原始值透传：永远能显示、不告警
            m = {"text": raw, "level": "info"}
        out = {"raw": raw, "text": m.get("text", raw),
               "level": m.get("level", "info"), "ts": v.get("ts", now)}
        prev = state.get("raw")
        state["raw"] = raw
        result = {"out": out}
        if prev is not None and prev != raw:
            result["event"] = out
        return result


class DisplayBlock:
    """显示点：看板卡片数据源。view 变化才输出（驱动增量传播截断）。"""

    kind = "display"
    inputs = ("state",)
    outputs = ("view",)

    @staticmethod
    def evaluate(params, ins, state, now, ctx):
        v = ins.get("state")
        if v is None:
            return None
        view = {"text": v.get("text"), "level": v.get("level"),
                "alias": params.get("alias", "")}
        if state.get("view") == view:
            return None
        state["view"] = view
        return {"view": view}


class AlertBlock:
    """告警点：warn 级 event → 冷却防抖 → Server酱。首值无 event（翻译块保证）。"""

    kind = "alert"
    inputs = ("trigger",)
    outputs = ()

    @staticmethod
    def evaluate(params, ins, state, now, ctx):
        v = ins.get("trigger")
        if v is None or v.get("level") != "warn":
            return None
        if now - state.get("last", 0) < int(params.get("cooldown", 60)):
            print(f"[alert] 冷却期内跳过: {ctx.get('node')}", flush=True)
            return None
        state["last"] = now
        name = ctx.get("alias") or ctx.get("node", "")
        send_sct(ctx.get("sendkey", ""), f"{v['text']}告警",
                 f"{name}（{ctx.get('node','')}）{v['text']}，"
                 f"时间 {time.strftime('%H:%M:%S')}")
        return None


BLOCKS = {b.kind: b for b in (IoInBlock, IoOutBlock, TranslateBlock, DisplayBlock, AlertBlock)}
