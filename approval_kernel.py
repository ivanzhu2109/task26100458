"""课堂日志审批内核：撤签与局部重执行。

模板为不可变 DAG，节点为人工审批，边条件为 通过/拒绝/任意。
单实例顺序调用，状态保存在内存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Tuple


class Result(Enum):
    """审批结果。"""

    APPROVE = "approve"
    REJECT = "reject"


class Action(Enum):
    """apply 支持的操作：通过、拒绝、撤回。"""

    APPROVE = "approve"
    REJECT = "reject"
    WITHDRAW = "withdraw"


class EdgeCond(Enum):
    """边条件：源节点结果为通过 / 拒绝 / 任意结果。"""

    ON_APPROVE = "approve"
    ON_REJECT = "reject"
    ANY = "any"


class EdgeKind(Enum):
    """边的取值：未决 / 关闭 / 开启。"""

    PENDING = "pending"
    CLOSED = "closed"
    OPEN = "open"


class NodeStatus(Enum):
    """节点状态。"""

    WAITING = "waiting"  # 有未决入边
    SKIPPED = "skipped"  # 入边全部关闭
    PENDING = "pending"  # 待签署
    SIGNED = "signed"    # 已签署


@dataclass(frozen=True)
class EdgeValue:
    """边的当前取值；仅 OPEN 携带源节点代次。"""

    kind: EdgeKind
    gen: Optional[int] = None


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    cond: EdgeCond


@dataclass(frozen=True)
class Graph:
    """不可变 DAG 模板。nodes 为节点名集合，edges 为边列表。"""

    nodes: Tuple[str, ...]
    edges: Tuple[Edge, ...]


@dataclass
class NodeState:
    """单节点运行时状态。"""

    status: NodeStatus = NodeStatus.WAITING
    result: Optional[Result] = None
    gen: int = 0
    # 签署时生效的开启入边身份及代次集合：{(src, gen), ...}
    evidence: Optional[FrozenSet[Tuple[str, int]]] = None


@dataclass
class Instance:
    """运行实例：模板 + 各节点状态。"""

    graph: Graph
    nodes: Dict[str, NodeState] = field(default_factory=dict)
    topo: Tuple[str, ...] = ()
    in_edges: Dict[str, Tuple[Edge, ...]] = field(default_factory=dict)


def _normalize_graph(graph) -> Graph:
    """接受 Graph 或 dict 规格 {"nodes": [...], "edges": [(src, dst, cond), ...]}。"""
    if isinstance(graph, Graph):
        return graph
    nodes = tuple(graph["nodes"])
    edges = []
    for item in graph.get("edges", []):
        src, dst = item[0], item[1]
        cond = item[2] if len(item) > 2 else EdgeCond.ANY
        if not isinstance(cond, EdgeCond):
            cond = EdgeCond(cond)
        edges.append(Edge(src, dst, cond))
    return Graph(nodes, tuple(edges))


def _topo_sort(graph: Graph) -> Tuple[str, ...]:
    indeg = {n: 0 for n in graph.nodes}
    for e in graph.edges:
        indeg[e.dst] += 1
    queue = [n for n in graph.nodes if indeg[n] == 0]
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for e in graph.edges:
            if e.src == n:
                indeg[e.dst] -= 1
                if indeg[e.dst] == 0:
                    queue.append(e.dst)
    if len(order) != len(graph.nodes):
        raise ValueError("graph must be a DAG (cycle detected)")
    return tuple(order)


def _edge_value(inst: Instance, edge: Edge) -> EdgeValue:
    src = inst.nodes[edge.src]
    if src.status in (NodeStatus.WAITING, NodeStatus.PENDING):
        return EdgeValue(EdgeKind.PENDING)
    if src.status is NodeStatus.SKIPPED:
        return EdgeValue(EdgeKind.CLOSED)
    matched = (
        edge.cond is EdgeCond.ANY
        or (edge.cond is EdgeCond.ON_APPROVE and src.result is Result.APPROVE)
        or (edge.cond is EdgeCond.ON_REJECT and src.result is Result.REJECT)
    )
    if matched:
        return EdgeValue(EdgeKind.OPEN, src.gen)
    return EdgeValue(EdgeKind.CLOSED)


def _in_values(inst: Instance, node: str) -> Tuple[EdgeValue, ...]:
    return tuple(_edge_value(inst, e) for e in inst.in_edges[node])


def _unsigned_status(in_values: Tuple[EdgeValue, ...]) -> NodeStatus:
    if not in_values:  # 根节点：固定开启输入
        return NodeStatus.PENDING
    if any(v.kind is EdgeKind.PENDING for v in in_values):
        return NodeStatus.WAITING
    if all(v.kind is EdgeKind.CLOSED for v in in_values):
        return NodeStatus.SKIPPED
    return NodeStatus.PENDING


def _signed_valid(inst: Instance, node: str,
                  in_values: Tuple[EdgeValue, ...]) -> bool:
    """已签署节点保留结果的条件：仍可签署且开启入边身份及代次集合不变。"""
    st = inst.nodes[node]
    if _unsigned_status(in_values) is not NodeStatus.PENDING:
        return False
    current = frozenset(
        (e.src, v.gen)
        for e, v in zip(inst.in_edges[node], in_values)
        if v.kind is EdgeKind.OPEN
    )
    return st.evidence == current


def create(graph: Graph) -> Instance:
    """依据模板创建实例并计算初始稳定状态。"""
    g = _normalize_graph(graph)
    names = set(g.nodes)
    for e in g.edges:
        if e.src not in names or e.dst not in names:
            raise ValueError(f"edge endpoint not in nodes: {e}")
        if e.src == e.dst:
            raise ValueError(f"self loop not allowed: {e}")
    inst = Instance(graph=g)
    inst.in_edges = {
        n: tuple(e for e in g.edges if e.dst == n) for n in g.nodes
    }
    inst.topo = _topo_sort(g)
    inst.nodes = {n: NodeState() for n in g.nodes}
    for name in inst.topo:
        inst.nodes[name].status = _unsigned_status(_in_values(inst, name))
    return inst


def _snapshot(inst: Instance) -> Dict[str, tuple]:
    return {
        name: (st.status, st.result, _in_values(inst, name))
        for name, st in inst.nodes.items()
    }


def apply(instance: Instance, node: str, token: int, action: Action) -> bool:
    """对节点执行 通过/拒绝/撤回；token 过期则拒绝且不改任何状态。"""
    if not isinstance(action, Action):
        action = Action(action)
    if node not in instance.nodes:
        raise ValueError(f"unknown node: {node}")
    st = instance.nodes[node]
    if token != st.gen:
        return False  # 过期提交：不改变任何节点
    if action is Action.WITHDRAW:
        if st.status is not NodeStatus.SIGNED:
            return False
    elif st.status not in (NodeStatus.PENDING, NodeStatus.SIGNED):
        return False  # 仅待签署可签署、已签署可改签

    pre = _snapshot(instance)

    # 变更目标节点并立即换代
    if action is Action.WITHDRAW:
        st.result = None
        st.evidence = None
        st.status = _unsigned_status(_in_values(instance, node))
    else:
        st.result = Result.APPROVE if action is Action.APPROVE else Result.REJECT
        in_vals = _in_values(instance, node)
        st.evidence = frozenset(
            (e.src, v.gen)
            for e, v in zip(instance.in_edges[node], in_vals)
            if v.kind is EdgeKind.OPEN
        )
        st.status = NodeStatus.SIGNED
    st.gen += 1

    # 传播：按拓扑序重算其余节点，状态或完整入边值变化者换代一次
    for name in instance.topo:
        if name == node:
            continue
        nst = instance.nodes[name]
        in_vals = _in_values(instance, name)
        if nst.status is NodeStatus.SIGNED and _signed_valid(
            instance, name, in_vals
        ):
            new_status, new_result, new_evidence = (
                NodeStatus.SIGNED, nst.result, nst.evidence,
            )
        else:
            new_status = _unsigned_status(in_vals)
            new_result, new_evidence = None, None
        pre_status, pre_result, pre_in = pre[name]
        if (new_status, new_result) != (pre_status, pre_result) or \
                in_vals != pre_in:
            nst.gen += 1
        nst.status, nst.result, nst.evidence = (
            new_status, new_result, new_evidence,
        )
    return True


def inspect(instance: Instance) -> dict:
    """返回实例当前快照：各节点状态、结果、代次与入边取值。"""
    nodes = {}
    for name in instance.topo:
        st = instance.nodes[name]
        in_vals = _in_values(instance, name)
        nodes[name] = {
            "status": st.status.value,
            "result": st.result.value if st.result is not None else None,
            "gen": st.gen,
            "token": st.gen,
            "evidence": (
                sorted(st.evidence) if st.evidence is not None else None
            ),
            "in_edges": [
                {
                    "src": e.src,
                    "cond": e.cond.value,
                    "kind": v.kind.value,
                    "gen": v.gen,
                }
                for e, v in zip(instance.in_edges[name], in_vals)
            ],
        }
    return {"nodes": nodes}
