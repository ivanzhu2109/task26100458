"""课堂日志审批内核:撤签与局部重执行。

模板为不可变 DAG,节点为人工审批,边条件为通过/拒绝/任意结果。
实例状态保存在内存,单实例顺序调用。
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Union

# ---- 类型与常量 ----

Condition = str  # "approve" | "reject" | "any"
Action = str     # "approve" | "reject" | "withdraw"
NodeName = str

COND_APPROVE: Condition = "approve"
COND_REJECT: Condition = "reject"
COND_ANY: Condition = "any"
CONDITIONS = (COND_APPROVE, COND_REJECT, COND_ANY)

ACTION_APPROVE: Action = "approve"
ACTION_REJECT: Action = "reject"
ACTION_WITHDRAW: Action = "withdraw"
ACTIONS = (ACTION_APPROVE, ACTION_REJECT, ACTION_WITHDRAW)

# 节点状态
STATUS_WAITING = "waiting"    # 等待:存在未决入边
STATUS_PENDING = "pending"    # 待签署
STATUS_SIGNED = "signed"      # 已签署
STATUS_SKIPPED = "skipped"    # 跳过:入边全关闭

# 边值
EDGE_PENDING = "pending"                        # 未决
EDGE_CLOSED = "closed"                          # 关闭
EDGE_OPEN = "open"                              # 开启(携带源节点当前代次)
EdgeValue = Union[str, Tuple[str, int]]         # "pending" | "closed" | ("open", 代次)

# 根节点的固定开启输入
ROOT_INPUT: Tuple[str, int] = ("<root>", 0)

Instance = Dict[str, Any]


# ---- 内部:图与状态推导 ----

def _normalize_graph(graph: Dict[str, Any]) -> Tuple[List[NodeName], Dict[NodeName, List[Tuple[NodeName, Condition]]]]:
    """校验模板,返回 (节点名列表, 节点 -> [(前驱, 条件)])。"""
    if not isinstance(graph, dict):
        raise TypeError("graph must be a dict")
    edges_spec = graph.get("edges", [])
    edges: List[Tuple[NodeName, NodeName, Condition]] = []
    for entry in edges_spec:
        if isinstance(entry, dict):
            src, dst = entry["from"], entry["to"]
            cond = entry.get("condition", COND_ANY)
        else:
            src, dst, cond = entry
        if cond not in CONDITIONS:
            raise ValueError(f"unknown edge condition: {cond!r}")
        edges.append((src, dst, cond))
    names: List[NodeName] = list(dict.fromkeys(graph.get("nodes", [])))
    for src, dst, _ in edges:
        for name in (src, dst):
            if name not in names:
                names.append(name)
    if not names:
        raise ValueError("graph has no nodes")
    preds: Dict[NodeName, List[Tuple[NodeName, Condition]]] = {n: [] for n in names}
    for src, dst, cond in edges:
        preds[dst].append((src, cond))
    return names, preds


def _topo_sort(names: List[NodeName], preds: Dict[NodeName, List[Tuple[NodeName, Condition]]]) -> List[NodeName]:
    """Kahn 拓扑排序;有环则报错(模板必须是 DAG)。"""
    indegree = {n: len(preds[n]) for n in names}
    order = [n for n in names if indegree[n] == 0]
    result: List[NodeName] = []
    queue = list(order)
    while queue:
        node = queue.pop(0)
        result.append(node)
        for other in names:
            for src, _ in preds[other]:
                if src == node:
                    indegree[other] -= 1
                    if indegree[other] == 0:
                        queue.append(other)
    if len(result) != len(names):
        raise ValueError("graph must be a DAG (cycle detected)")
    return result


def _edge_value(instance: Instance, src: NodeName, cond: Condition) -> EdgeValue:
    """由源节点当前状态推导边值。"""
    st = instance["nodes"][src]
    status = st["status"]
    if status in (STATUS_WAITING, STATUS_PENDING):
        return EDGE_PENDING
    if status == STATUS_SKIPPED:
        return EDGE_CLOSED
    # 已签署:按条件关闭或开启,开启携带源节点当前代次
    if cond == COND_ANY or cond == st["result"]:
        return (EDGE_OPEN, st["token"])
    return EDGE_CLOSED


def _incoming(instance: Instance, node: NodeName) -> Tuple[Tuple[NodeName, EdgeValue], ...]:
    """节点的完整入边值(根节点含固定开启输入)。"""
    preds = instance["preds"][node]
    if not preds:
        return ((ROOT_INPUT[0], (EDGE_OPEN, ROOT_INPUT[1])),)
    return tuple((src, _edge_value(instance, src, cond)) for src, cond in preds)


def _open_inputs(instance: Instance, node: NodeName) -> FrozenSet[Tuple[NodeName, int]]:
    """开启入边的 (身份, 代次) 集合;根节点为固定开启输入。"""
    preds = instance["preds"][node]
    if not preds:
        return frozenset({ROOT_INPUT})
    opened = set()
    for src, cond in preds:
        value = _edge_value(instance, src, cond)
        if isinstance(value, tuple):
            opened.add((src, value[1]))
    return frozenset(opened)


def _compute_status(instance: Instance, node: NodeName) -> str:
    """未签署节点的状态推导。"""
    values = [value for _, value in _incoming(instance, node)]
    if any(value == EDGE_PENDING for value in values):
        return STATUS_WAITING
    if all(value == EDGE_CLOSED for value in values):
        return STATUS_SKIPPED
    return STATUS_PENDING


def _signed_still_valid(instance: Instance, node: NodeName) -> bool:
    """已签署节点是否仍满足保留结果的条件。"""
    st = instance["nodes"][node]
    if _compute_status(instance, node) != STATUS_PENDING:
        return False
    return st["signed_inputs"] == _open_inputs(instance, node)


def _node_snapshot(instance: Instance, node: NodeName) -> Tuple[Any, ...]:
    """用于变更检测:节点状态 + 完整入边值。"""
    st = instance["nodes"][node]
    return (st["status"], st["result"], _incoming(instance, node))


def _settle(instance: Instance, node: NodeName) -> None:
    """就地稳定单个节点:清除失效签署,重算未签署状态。

    调用前提:该节点的全部前驱已稳定(按拓扑序调用)。
    """
    st = instance["nodes"][node]
    if st["status"] == STATUS_SIGNED:
        if _signed_still_valid(instance, node):
            return
        # 旧审批不得自动复活:失效即清除,需重新签署
        st["result"] = None
        st["signed_inputs"] = None
    st["status"] = _compute_status(instance, node)


# ---- 库操作 ----

def create(graph: Dict[str, Any]) -> Instance:
    """根据不可变 DAG 模板创建实例。

    graph = {
        "nodes": ["A", "B", ...],                # 可省略,由边推断
        "edges": [("A", "B", "approve"), ...],   # 或 {"from","to","condition"}
    }
    """
    names, preds = _normalize_graph(graph)
    topo = _topo_sort(names, preds)
    instance: Instance = {
        "preds": preds,
        "topo": topo,
        "nodes": {
            name: {"token": 0, "status": None, "result": None, "signed_inputs": None}
            for name in names
        },
    }
    for name in topo:
        _settle(instance, name)
    return instance


def apply(instance: Instance, node: NodeName, token: int, action: Action) -> Dict[str, Any]:
    """对节点执行签署/改签/撤回,携带目标节点当前代次。

    返回 {"ok": True, "token": <目标新代次>} 或 {"ok": False, "reason": ...}。
    代次过期或动作非法时不改变任何节点。
    """
    if node not in instance["nodes"]:
        raise KeyError(f"unknown node: {node!r}")
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action!r}")
    st = instance["nodes"][node]
    if st["token"] != token:
        return {"ok": False, "reason": "stale_token"}
    if action == ACTION_WITHDRAW:
        if st["status"] != STATUS_SIGNED:
            return {"ok": False, "reason": "illegal_action"}
    elif st["status"] not in (STATUS_PENDING, STATUS_SIGNED):
        return {"ok": False, "reason": "illegal_action"}

    before = {name: _node_snapshot(instance, name) for name in instance["topo"]}

    # 变更目标节点;签署时绑定当前有效输入证据
    if action == ACTION_WITHDRAW:
        st["result"] = None
        st["signed_inputs"] = None
        st["status"] = _compute_status(instance, node)
    else:
        st["status"] = STATUS_SIGNED
        st["result"] = action
        st["signed_inputs"] = _open_inputs(instance, node)
    # 操作目标必换代一次;下游开启边引用更新后的代次
    st["token"] += 1

    # 按拓扑序传播:前驱的代次变更先于后继处理,同一节点只换代一次
    for name in instance["topo"]:
        if name == node:
            continue
        _settle(instance, name)
        if _node_snapshot(instance, name) != before[name]:
            instance["nodes"][name]["token"] += 1

    return {"ok": True, "token": st["token"]}


def inspect(instance: Instance) -> Dict[str, Any]:
    """返回实例当前状态的快照(纯数据)。"""
    snapshot: Dict[str, Any] = {"nodes": {}}
    for name in instance["topo"]:
        st = instance["nodes"][name]
        incoming = {}
        for src, value in _incoming(instance, name):
            incoming[src] = list(value) if isinstance(value, tuple) else value
        snapshot["nodes"][name] = {
            "token": st["token"],
            "status": st["status"],
            "result": st["result"],
            "incoming": incoming,
        }
    return snapshot
