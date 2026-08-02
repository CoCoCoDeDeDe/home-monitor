"""图服务（Issue #27）：store+engine+blocks 的装配层，main.py 只与它对。

- 默认值策略：无图节点首次 feed 时按固件类型档案生成默认图（原始文案）
- 表单投影：projection/apply_form 读写节点标准子图（固定块 id in1/sem1/disp1/alm1）
- 图是唯一真身：注册表/语义档案概念不存在于本层
"""

import time

from graph_engine import Graph, GraphError
from graph_store import GraphStore


def default_map(profiles: dict, ntype: str) -> dict:
    """默认翻译表 = 固件类型档案的 dashboard.events（硬件档案），
    无 dashboard 时退到 template.map（模板文件，兼容过渡期的 contact 固件词表）。"""
    p = profiles.get(ntype) or {}
    return dict(((p.get("dashboard") or {}).get("events"))
                or ((p.get("template") or {}).get("map")) or {})


def standard_graph(ntype: str, node: str, alias: str, map_: dict, cooldown: int) -> dict:
    """节点标准子图：io_in → translate → display / alert（固定块 id，表单投影约定）。"""
    return {"blocks": [
        {"id": "in1", "kind": "io_in",
         "params": {"topic": f"{ntype}/{node}/state"}},
        {"id": "sem1", "kind": "translate", "params": {"map": map_}},
        {"id": "disp1", "kind": "display", "params": {"alias": alias}},
        {"id": "alm1", "kind": "alert",
         "params": {"cooldown": int(cooldown), "channel": "sct"}},
    ], "wires": [
        {"from": "in1.state", "to": "sem1.in"},
        {"from": "sem1.out", "to": "disp1.state"},
        {"from": "sem1.event", "to": "alm1.trigger"},
    ]}


class GraphService:
    def __init__(self, store: GraphStore, profiles: dict, sendkey: str, publish):
        self._store = store
        self._profiles = profiles
        self._sendkey = sendkey
        self._publish = publish
        self._graphs: dict[str, Graph] = {}
        self._specs: dict[str, dict] = {}  # 重建用（Graph 内部结构不暴露 spec）
        self._ntypes: dict[str, str] = {}  # node → 固件类型（合并默认翻译表用）
        for nid, spec in store.load_all().items():
            try:
                self._graphs[nid] = Graph(spec)
                self._specs[nid] = spec
            except GraphError as e:  # 坏图跳过，节点退化兜底显示，不拖垮服务
                print(f"[graph] 节点 {nid} 图加载失败跳过: {e}", flush=True)

    def _ensure(self, ntype: str, node: str) -> Graph:
        self._ntypes[node] = ntype
        if node not in self._graphs:
            spec = standard_graph(ntype, node, "", default_map(self._profiles, ntype), 60)
            self._graphs[node] = Graph(spec)
            self._specs[node] = spec  # 默认图不落库：用户保存表单时才持久化
            print(f"[graph] {node}: 生成默认图（{ntype}）", flush=True)
        return self._graphs[node]

    def _ctx(self, node: str) -> dict:
        alias = (self._graphs[node].block_params("disp1") or {}).get("alias", "")
        return {"sendkey": self._sendkey, "node": node, "alias": alias,
                "publish": self._publish}

    def feed_state(self, ntype: str, node: str, raw: str, now: float) -> None:
        g = self._ensure(ntype, node)
        g.feed("in1", {"state": {"raw": raw, "ts": now}}, now, self._ctx(node))

    def display_of(self, node: str) -> dict:
        g = self._graphs.get(node)
        return (g.output("disp1", "view") or {}) if g else {}

    def translate_map_of(self, node: str) -> dict:
        """节点生效翻译表：默认表为底 + 图内 map 覆盖（看板事件列表翻译历史原始值用）。"""
        g = self._graphs.get(node)
        saved = ((g.block_params("sem1") or {}).get("map") or {}) if g else {}
        return {**default_map(self._profiles, self._ntypes.get(node, "")), **saved}

    def projection(self, ntype: str, node: str) -> dict:
        """表单读：从标准块 params 提取；无图按默认值投影（不落库）。
        map 合并默认表为底：固件词表里的键（如过渡期 contact 的 open/closed）
        不会因用户只保存了部分键而丢失翻译。"""
        self._ensure(ntype, node)
        g = self._graphs[node]
        saved = (g.block_params("sem1") or {}).get("map") or {}
        return {
            "alias": (g.block_params("disp1") or {}).get("alias", ""),
            "map": {**default_map(self._profiles, ntype), **saved},
            "cooldown": (g.block_params("alm1") or {}).get("cooldown", 60),
        }

    def apply_form(self, ntype: str, node: str, alias: str,
                   map_: dict, cooldown: int) -> dict:
        """表单写：重建标准子图 + 落库 + 热加载。GraphError 由 API 层转 400。
        map 合并默认表为底：用户只编辑部分键时，固件词表其余键翻译不丢。"""
        spec = standard_graph(ntype, node, alias,
                              {**default_map(self._profiles, ntype), **map_}, cooldown)
        g = Graph(spec)  # 先校验，非法图不入库
        self._store.save(node, spec)
        self._graphs[node] = g
        self._specs[node] = spec
        self._ntypes[node] = ntype
        print(f"[graph] {node}: 表单已应用（alias={alias!r}）", flush=True)
        return self.projection(ntype, node)
