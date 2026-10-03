"""审批内核测试（骨架，后续填充）。"""
"""课堂日志审批内核测试。

运行：python3 -m pytest tests/ -v
"""

import pytest

from approval_kernel import (
    Action,
    Edge,
    EdgeCond,
    Graph,
    apply,
    create,
    inspect,
)


def make_join():
    """两个根审批 s1/s2 汇入合流点 j，边条件均为“通过”。"""
    return create({
        "nodes": ["s1", "s2", "j"],
        "edges": [("s1", "j", "approve"), ("s2", "j", "approve")],
    })


def token_of(inst, name):
    return inspect(inst)["nodes"][name]["token"]


def node(inst, name):
    return inspect(inst)["nodes"][name]


def sign_joined(inst):
    """s1 通过、s2 拒绝（s2->j 关闭），随后签署 j，返回 j 的 token。"""
    assert apply(inst, "s1", 0, Action.APPROVE)
    assert apply(inst, "s2", 0, Action.REJECT)
    tok = token_of(inst, "j")
    assert node(inst, "j")["status"] == "pending"
    assert apply(inst, "j", tok, Action.APPROVE)
    return token_of(inst, "j")


class TestJoinEvidence:
    """已签署合流点同时有开启和关闭入边的场景。"""

    def test_resign_closed_source_keeps_join(self):
        inst = make_join()
        tok_j = sign_joined(inst)
        # 重签关闭边的源节点 s2，结果不变（边仍关闭）
        assert apply(inst, "s2", 1, Action.REJECT)
        j = node(inst, "j")
        assert j["status"] == "signed"
        assert j["result"] == "approve"
        assert j["token"] == tok_j  # 合流审批及 token 保留

    def test_resign_open_source_invalidates_join(self):
        inst = make_join()
        sign_joined(inst)
        # 重签开启边的源节点 s1，即使结果相同
        assert apply(inst, "s1", 1, Action.APPROVE)
        j = node(inst, "j")
        assert j["status"] == "pending"
        assert j["result"] is None

    def test_withdraw_and_resign_then_old_token_rejected(self):
        inst = make_join()
        tok_j = sign_joined(inst)
        # 撤回再重签
        assert apply(inst, "j", tok_j, Action.WITHDRAW)
        assert node(inst, "j")["status"] == "pending"
        assert node(inst, "j")["result"] is None
        tok_after_withdraw = token_of(inst, "j")
        assert apply(inst, "j", tok_after_withdraw, Action.APPROVE)
        # 旧 token（撤回前与重签前）都不能再提交
        stale_snapshot = inspect(inst)
        assert not apply(inst, "j", tok_j, Action.REJECT)
        assert not apply(inst, "j", tok_after_withdraw, Action.WITHDRAW)
        assert inspect(inst) == stale_snapshot  # 过期提交不改变任何节点

    def test_resign_same_result_still_bumps_target_token(self):
        inst = make_join()
        sign_joined(inst)
        assert apply(inst, "s1", 1, Action.APPROVE)  # 改签为相同结果
        assert token_of(inst, "s1") == 2
        assert not apply(inst, "s1", 1, Action.APPROVE)  # 旧 token 失效


class TestDiamond:
    """菱形图：a->{b,c}->d，共享节点 d 被多条路径触及。"""

    def make(self):
        return create({
            "nodes": ["a", "b", "c", "d"],
            "edges": [
                ("a", "b", "any"), ("a", "c", "any"),
                ("b", "d", "any"), ("c", "d", "any"),
            ],
        })

    def sign_all(self, inst):
        apply(inst, "a", token_of(inst, "a"), Action.APPROVE)
        apply(inst, "b", token_of(inst, "b"), Action.APPROVE)
        apply(inst, "c", token_of(inst, "c"), Action.APPROVE)
        apply(inst, "d", token_of(inst, "d"), Action.APPROVE)

    def test_single_apply_bumps_shared_node_once(self):
        inst = self.make()
        self.sign_all(inst)
        gen_before = {n: token_of(inst, n) for n in "abcd"}
        # 重签 a：经 b、c 两条路径同时触及 d
        assert apply(inst, "a", gen_before["a"], Action.APPROVE)
        assert token_of(inst, "d") == gen_before["d"] + 1  # 只换代一次
        # b/c/d 的审批均因证据失效被清除，需重新收集
        for n in "bc":
            assert node(inst, n)["status"] == "pending"
            assert node(inst, n)["result"] is None
        assert node(inst, "d")["status"] == "waiting"  # b/c 未决
        assert node(inst, "d")["result"] is None

    def test_withdraw_propagates_through_both_paths_once(self):
        inst = self.make()
        self.sign_all(inst)
        gen_before = {n: token_of(inst, n) for n in "abcd"}
        assert apply(inst, "a", gen_before["a"], Action.WITHDRAW)
        assert token_of(inst, "d") == gen_before["d"] + 1
        assert node(inst, "a")["status"] == "pending"
        for n in "bcd":
            assert node(inst, n)["result"] is None


class TestBasics:
    def test_initial_state(self):
        inst = make_join()
        snap = inspect(inst)["nodes"]
        assert snap["s1"]["status"] == "pending"  # 根：固定开启输入
        assert snap["s2"]["status"] == "pending"
        assert snap["j"]["status"] == "waiting"   # 有未决入边
        assert all(snap[n]["token"] == 0 for n in snap)

    def test_stale_token_changes_nothing(self):
        inst = make_join()
        apply(inst, "s1", 0, Action.APPROVE)
        before = inspect(inst)
        assert not apply(inst, "s1", 0, Action.APPROVE)  # token 已过期
        assert not apply(inst, "s2", 99, Action.APPROVE)
        assert inspect(inst) == before

    def test_illegal_actions_rejected(self):
        inst = make_join()
        assert not apply(inst, "j", 0, Action.APPROVE)   # 等待中不可签
        assert not apply(inst, "s1", 0, Action.WITHDRAW)  # 未签署不可撤
        apply(inst, "s1", 0, Action.REJECT)
        apply(inst, "s2", 0, Action.REJECT)
        j = node(inst, "j")
        assert j["status"] == "skipped"  # 入边全关闭
        assert not apply(inst, "j", j["token"], Action.APPROVE)  # 跳过不可签

    def test_reactivated_branch_needs_fresh_approval(self):
        inst = make_join()
        # s1 通过、s2 拒绝 -> j 可签；签署后 s1 撤回 -> j 失效
        tok_j = sign_joined(inst)
        assert apply(inst, "s1", 1, Action.WITHDRAW)
        j = node(inst, "j")
        assert j["status"] == "waiting"  # s1 未决
        assert j["result"] is None
        # s1 重新签署后 j 重新激活，但旧审批不得复活
        assert apply(inst, "s1", 2, Action.APPROVE)
        j = node(inst, "j")
        assert j["status"] == "pending"
        assert j["result"] is None
        assert j["token"] != tok_j

    def test_skip_then_reactivate_clears_old_result(self):
        # k -> m（条件：通过），先让 k 通过并签 m，再改签 k 为拒绝使 m 跳过
        inst = create({
            "nodes": ["k", "m"],
            "edges": [("k", "m", "approve")],
        })
        apply(inst, "k", 0, Action.APPROVE)
        apply(inst, "m", token_of(inst, "m"), Action.REJECT)
        assert node(inst, "m")["status"] == "signed"
        # 改签 k 为拒绝：m 入边全关闭 -> 跳过，旧审批清除
        apply(inst, "k", 1, Action.REJECT)
        assert node(inst, "m")["status"] == "skipped"
        assert node(inst, "m")["result"] is None
        # 再改签回通过：m 重新待签署，旧审批不得复活
        apply(inst, "k", 2, Action.APPROVE)
        m = node(inst, "m")
        assert m["status"] == "pending"
        assert m["result"] is None

    def test_open_edge_carries_source_gen(self):
        inst = make_join()
        apply(inst, "s1", 0, Action.APPROVE)
        edges = {e["src"]: e for e in node(inst, "j")["in_edges"]}
        assert edges["s1"]["kind"] == "open"
        assert edges["s1"]["gen"] == 1
        assert edges["s2"]["kind"] == "pending"
        assert edges["s2"]["gen"] is None

    def test_multi_root_and_shared_downstream(self):
        # 两个根 r1/r2 共享下游 t；r1 签署不影响 r2 的待签署状态
        inst = create({
            "nodes": ["r1", "r2", "t"],
            "edges": [("r1", "t", "any"), ("r2", "t", "any")],
        })
        apply(inst, "r1", 0, Action.APPROVE)
        assert node(inst, "r2")["status"] == "pending"
        assert node(inst, "t")["status"] == "waiting"

    def test_graph_object_and_cycle_rejected(self):
        g = Graph(("x",), ())
        inst = create(g)
        assert node(inst, "x")["status"] == "pending"
        with pytest.raises(ValueError):
            create(Graph(("x", "y"),
                         (Edge("x", "y", EdgeCond.ANY),
                          Edge("y", "x", EdgeCond.ANY))))

def test_skeleton():
    assert True
