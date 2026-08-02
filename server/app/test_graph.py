"""图引擎测试：零依赖，python test_graph.py 直接跑。requests stub 在 blocks import 前。"""

import sys, types, tempfile, os

for _n in ["requests"]:
    sys.modules[_n] = types.ModuleType(_n)

FAILED = []
def check(name, fn):
    try:
        fn()
        print(f"PASS {name}")
    except AssertionError as e:
        FAILED.append(name)
        print(f"FAIL {name}: {e}")

def test_store_roundtrip():
    from graph_store import GraphStore
    db = tempfile.mktemp(suffix=".db")
    s = GraphStore(db)
    spec = {"blocks": [{"id": "in1", "kind": "io_in", "params": {}}], "wires": []}
    s.save("6750f8", spec)
    assert s.load("6750f8") == spec, "save 后 load 应原样返回"
    assert s.load_all() == {"6750f8": spec}
    s.save("6750f8", {"blocks": [], "wires": []})  # upsert 覆盖
    assert s.load("6750f8") == {"blocks": [], "wires": []}
    s.delete("6750f8")
    assert s.load("6750f8") is None
    s.close(); os.unlink(db)

check("store_roundtrip", test_store_roundtrip)

def test_translate_block():
    from blocks import BLOCKS
    b = BLOCKS["translate"]
    params = {"map": {"triggered": {"text": "门窗打开", "level": "warn"},
                      "released": {"text": "门窗关闭", "level": "info"}}}
    st = {}
    r1 = b.evaluate(params, {"in": {"raw": "triggered", "ts": 1.0}}, st, 1.0, {})
    assert r1["out"]["text"] == "门窗打开" and r1["out"]["level"] == "warn"
    assert "event" not in r1, "首个值不应产生 event（防开机/retain 误告警）"
    r2 = b.evaluate(params, {"in": {"raw": "released", "ts": 2.0}}, st, 2.0, {})
    assert r2["event"]["text"] == "门窗关闭", "raw 变化应产生 event"
    r3 = b.evaluate(params, {"in": {"raw": "weird", "ts": 3.0}}, st, 3.0, {})
    assert r3["out"]["text"] == "weird" and r3["out"]["level"] == "info", "未知原始值透传"

def test_alert_block_cooldown():
    from blocks import BLOCKS
    sent = []
    import alerter
    orig = alerter.send_sct
    alerter.send_sct = lambda k, t, d: sent.append(t)
    try:
        import blocks
        blocks.send_sct = alerter.send_sct  # monkeypatch 到 blocks 命名空间
        b = BLOCKS["alert"]
        params = {"cooldown": 60, "channel": "sct"}
        st = {}
        ctx = {"sendkey": "", "node": "6750f8", "alias": "平台窗户"}
        warn = {"raw": "triggered", "text": "门窗打开", "level": "warn", "ts": 0}
        b.evaluate(params, {"trigger": warn}, st, 100.0, ctx)
        b.evaluate(params, {"trigger": warn}, st, 120.0, ctx)  # 冷却内
        b.evaluate(params, {"trigger": warn}, st, 200.0, ctx)  # 冷却外
        assert sent == ["门窗打开告警", "门窗打开告警"], f"冷却期只发一次: {sent}"
        info = {"raw": "released", "text": "门窗关闭", "level": "info", "ts": 0}
        b.evaluate(params, {"trigger": info}, st, 300.0, ctx)
        assert len(sent) == 2, "非 warn 不告警"
    finally:
        alerter.send_sct = orig

def test_display_block():
    from blocks import BLOCKS
    b = BLOCKS["display"]
    st = {}
    v = {"raw": "triggered", "text": "门窗打开", "level": "warn", "ts": 1.0}
    r1 = b.evaluate({"alias": "平台窗户"}, {"state": v}, st, 1.0, {})
    assert r1["view"] == {"text": "门窗打开", "level": "warn", "alias": "平台窗户"}
    r2 = b.evaluate({"alias": "平台窗户"}, {"state": v}, st, 2.0, {})
    assert r2 is None, "view 无变化返回 None"

check("translate_block", test_translate_block)
check("alert_block_cooldown", test_alert_block_cooldown)
check("display_block", test_display_block)

def _spec(cycle=False, bad_kind=False, bad_port=False):
    kind2 = "nope" if bad_kind else "translate"
    to_port = "nope" if bad_port else "in"
    wires = [{"from": "in1.state", "to": f"sem1.{to_port}"}]
    if cycle:
        wires.append({"from": "sem1.out", "to": "in1.state"})
    return {"blocks": [
        {"id": "in1", "kind": "io_in", "params": {"topic": "collision/6750f8/state"}},
        {"id": "sem1", "kind": kind2, "params": {"map": {"triggered": {"text": "开", "level": "warn"}}}},
    ], "wires": wires}

def test_graph_validation():
    from graph_engine import Graph, GraphError
    Graph(_spec())  # 合法图不抛
    for bad, why in [(_spec(bad_kind=True), "未知 kind"),
                     (_spec(bad_port=True), "端口不存在"),
                     (_spec(cycle=True), "环")]:
        try:
            Graph(bad)
            raise AssertionError(f"{why} 应抛 GraphError 但没抛")
        except GraphError:
            pass

def test_graph_feed_incremental():
    from graph_engine import Graph
    spec = {"blocks": [
        {"id": "in1", "kind": "io_in", "params": {"topic": "t"}},
        {"id": "sem1", "kind": "translate", "params": {"map": {"triggered": {"text": "开", "level": "warn"},
                                                              "released": {"text": "关", "level": "info"}}}},
        {"id": "disp1", "kind": "display", "params": {"alias": "平台窗户"}},
    ], "wires": [
        {"from": "in1.state", "to": "sem1.in"},
        {"from": "sem1.out", "to": "disp1.state"},
    ]}
    g = Graph(spec)
    produced = g.feed("in1", {"state": {"raw": "triggered", "ts": 1.0}}, 1.0, {})
    assert produced == {"in1", "sem1", "disp1"}, f"首次全链路传播: {produced}"
    assert g.output("disp1", "view") == {"text": "开", "level": "warn", "alias": "平台窗户"}
    produced = g.feed("in1", {"state": {"raw": "triggered", "ts": 2.0}}, 2.0, {})
    assert produced == {"in1", "sem1"}, f"display 输入没变应截断: {produced}"
    produced = g.feed("in1", {"state": {"raw": "released", "ts": 3.0}}, 3.0, {})
    assert "disp1" in produced and g.output("disp1", "view")["text"] == "关"
    assert g.block_params("disp1") == {"alias": "平台窗户"}
    assert g.block_params("nope") is None

check("graph_validation", test_graph_validation)
check("graph_feed_incremental", test_graph_feed_incremental)

def _profiles():
    return {"collision": {"dashboard": {"events": {
        "triggered": {"text": "碰撞触发", "level": "warn"},
        "released": {"text": "碰撞释放", "level": "info"}}}}}

def test_service_default_and_form():
    import tempfile, os
    from graph_store import GraphStore
    from graph_service import GraphService
    db = tempfile.mktemp(suffix=".db")
    svc = GraphService(GraphStore(db), _profiles(), "", None)
    # 默认值策略：无图节点 feed 后自动生成默认图，文案=collision 原始文案
    svc.feed_state("collision", "6750f8", "triggered", 1.0)
    assert svc.display_of("6750f8")["text"] == "碰撞触发"
    # 表单投影默认值
    proj = svc.projection("collision", "6750f8")
    assert proj["alias"] == "" and proj["map"]["triggered"]["text"] == "碰撞触发"
    # 表单写：自定义文案立即生效
    svc.apply_form("collision", "6750f8", "平台窗户",
                   {"triggered": {"text": "门窗打开", "level": "warn"},
                    "released": {"text": "门窗关闭", "level": "info"}}, 60)
    svc.feed_state("collision", "6750f8", "released", 2.0)
    d = svc.display_of("6750f8")
    assert d == {"text": "门窗关闭", "level": "info", "alias": "平台窗户"}, d
    assert svc.translate_map_of("6750f8")["triggered"]["text"] == "门窗打开"
    # 持久化：新实例（模拟重启）图还在
    svc2 = GraphService(GraphStore(db), _profiles(), "", None)
    svc2.feed_state("collision", "6750f8", "triggered", 3.0)
    assert svc2.display_of("6750f8")["text"] == "门窗打开", "重启后图应保留"
    assert svc2.projection("collision", "6750f8")["alias"] == "平台窗户"

def test_service_alert_link():
    import tempfile, os, time
    import blocks
    sent = []
    orig = blocks.send_sct
    blocks.send_sct = lambda k, t, d: sent.append((t, d))
    try:
        from graph_store import GraphStore
        from graph_service import GraphService
        db = tempfile.mktemp(suffix=".db")
        svc = GraphService(GraphStore(db), _profiles(), "", None)
        svc.apply_form("collision", "6750f8", "平台窗户",
                       {"triggered": {"text": "门窗打开", "level": "warn"},
                        "released": {"text": "门窗关闭", "level": "info"}}, 60)
        T0 = 1_700_000_000.0  # 用真实时间戳：冷却判定 now-last<cooldown，小时刻会落在初始冷却内
        svc.feed_state("collision", "6750f8", "triggered", T0)        # 首值无 event，不告警
        svc.feed_state("collision", "6750f8", "released", T0 + 1)     # info，不告警
        svc.feed_state("collision", "6750f8", "triggered", T0 + 2)    # 变化沿 + warn → 告警
        assert len(sent) == 1 and sent[0][0] == "门窗打开告警", sent
        assert "平台窗户" in sent[0][1]
    finally:
        blocks.send_sct = orig

check("service_default_and_form", test_service_default_and_form)
check("service_alert_link", test_service_alert_link)

def test_main_on_state_link():
    import tempfile, os
    for name in ["fastapi", "pydantic", "paho", "paho.mqtt", "paho.mqtt.client"]:
        sys.modules[name] = types.ModuleType(name)
    class _F:
        def __init__(self, *a, **k): pass
        def get(self, *a, **k): return lambda f: f
        def put(self, *a, **k): return lambda f: f
    sys.modules["fastapi"].FastAPI = _F
    sys.modules["fastapi"].HTTPException = Exception
    fr = types.ModuleType("fastapi.responses")
    fr.FileResponse = lambda *a, **k: None
    sys.modules["fastapi.responses"] = fr
    sys.modules["pydantic"].BaseModel = object
    sys.modules["paho.mqtt.client"].Client = _F
    sys.modules["paho.mqtt.client"].CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    import events
    from graph_store import GraphStore
    from graph_service import GraphService
    import main
    db = tempfile.mktemp(suffix=".db")
    store = events.EventStore(db)
    gs = GraphStore(db)
    gsvc = GraphService(gs, _profiles(), "", None)
    main._on_state("collision", "6750f8", "triggered", False, False,
                   gsvc, store, lambda t, n: None)
    assert main.nodes["6750f8"]["state"] == "triggered"
    assert gsvc.display_of("6750f8")["text"] == "碰撞触发", "图引擎应产出 display"
    ev = store.query(limit=1)[0]
    assert ev["payload"]["state"] == "triggered", "事件落原始值"
    # retained 重放：不记事件、不喂图
    main._on_state("collision", "6750f8", "released", False, True,
                   gsvc, store, lambda t, n: None)
    assert gsvc.display_of("6750f8")["text"] == "碰撞触发", "retained 不应喂图"
    store.close(); gs.close(); os.unlink(db)

check("main_on_state_link", test_main_on_state_link)

if __name__ == "__main__":
    sys.exit(1 if FAILED else 0)
