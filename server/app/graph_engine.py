"""图引擎（Issue #27）：图校验 + 拓扑排序 + 事件驱动增量求值。

Graph 只认 spec（{"blocks":[...], "wires":[...]}），不关心持久化与 MQTT。
feed(block_id, outputs) 从某块注入输出并沿连线传播：只有上游产出新输出的块
才重算；块 evaluate 返回 None = 输出无变化 = 截断传播。
"""

from blocks import BLOCKS


class GraphError(ValueError):
    """非法图：未知 kind / 端口不存在 / 引用缺失 / 成环 / 缺字段。"""


def _parse_port(ref: str, what: str) -> tuple[str, str]:
    if "." not in ref:
        raise GraphError(f"{what} 端口引用缺 '.': {ref!r}")
    return tuple(ref.split(".", 1))


class Graph:
    def __init__(self, spec: dict):
        self._blocks = {b["id"]: b for b in spec.get("blocks", [])}
        if len(self._blocks) != len(spec.get("blocks", [])):
            raise GraphError("块 id 重复")
        for bid, b in self._blocks.items():
            kind = b.get("kind")
            if kind not in BLOCKS:
                raise GraphError(f"块 {bid}: 未知 kind {kind!r}")
            if "params" not in b:
                b["params"] = {}
        self._in_wires: dict[str, list[tuple[str, str, str]]] = {
            bid: [] for bid in self._blocks}
        for w in spec.get("wires", []):
            fb, fp = _parse_port(w.get("from", ""), "from")
            tb, tp = _parse_port(w.get("to", ""), "to")
            if fb not in self._blocks or tb not in self._blocks:
                raise GraphError(f"连线引用未知块: {w}")
            if fp not in BLOCKS[self._blocks[fb]["kind"]].outputs:
                raise GraphError(f"块 {fb} 无输出端口 {fp!r}")
            if tp not in BLOCKS[self._blocks[tb]["kind"]].inputs:
                raise GraphError(f"块 {tb} 无输入端口 {tp!r}")
            self._in_wires[tb].append((fb, fp, tp))
        self._topo = self._topo_sort()
        self._outputs: dict[str, dict] = {bid: {} for bid in self._blocks}
        self._state: dict[str, dict] = {bid: {} for bid in self._blocks}

    def _topo_sort(self) -> list[str]:
        """Kahn；有环抛 GraphError。"""
        indeg = {bid: len(ws) for bid, ws in self._in_wires.items()}
        queue = [bid for bid, d in indeg.items() if d == 0]
        order = []
        while queue:
            bid = queue.pop()
            order.append(bid)
            for tid, ws in self._in_wires.items():
                if any(fb == bid for fb, _, _ in ws):
                    indeg[tid] -= 1
                    if indeg[tid] == 0:
                        queue.append(tid)
        if len(order) != len(self._blocks):
            raise GraphError("连线成环")
        return order

    def feed(self, block_id: str, outputs: dict, now: float, ctx: dict) -> set[str]:
        """从 block_id 注入输出并传播。返回本轮产出新输出的块 id 集。"""
        if block_id not in self._blocks:
            raise GraphError(f"feed 未知块 {block_id!r}")
        self._outputs[block_id].update(outputs)
        produced = {block_id}
        for bid in self._topo:
            if bid == block_id or bid in produced:
                continue
            ws = self._in_wires[bid]
            if not any(fb in produced for fb, _, _ in ws):
                continue
            block = BLOCKS[self._blocks[bid]["kind"]]
            ins = {tp: self._outputs[fb].get(fp) for fb, fp, tp in ws}
            try:
                result = block.evaluate(self._blocks[bid]["params"], ins,
                                        self._state[bid], now, ctx)
            except Exception as e:  # 块异常：日志 + 本次传播终止，不扩散
                print(f"[graph] 块 {bid} 求值异常: {e}", flush=True)
                continue
            if result:
                self._outputs[bid].update(result)
                produced.add(bid)
        return produced

    def output(self, block_id: str, port: str):
        return self._outputs.get(block_id, {}).get(port)

    def block_params(self, block_id: str) -> dict | None:
        b = self._blocks.get(block_id)
        return b["params"] if b else None
