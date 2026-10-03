"""审批内核测试:撤签、局部重执行、代次(token)语义。"""
import unittest

from approval_kernel import create, apply, inspect


def node(inst, name):
    return inspect(inst)["nodes"][name]


def token(inst, name):
    return node(inst, name)["token"]


class ConfluenceTest(unittest.TestCase):
    """合流点:一条开启入边 + 一条关闭入边。"""

    def setUp(self):
        # A --approve--> M <--reject-- B (A、B 为根)
        self.inst = create({"edges": [("A", "M", "approve"), ("B", "M", "reject")]})
        apply(self.inst, "A", 0, "approve")   # A->M 开启
        apply(self.inst, "B", 0, "approve")   # B->M 关闭(条件是 reject)
        m = node(self.inst, "M")
        self.assertEqual(m["status"], "pending")
        self.assertEqual(m["incoming"], {"A": ["open", 1], "B": "closed"})
        r = apply(self.inst, "M", m["token"], "approve")
        self.assertTrue(r["ok"])
        self.m_token = token(self.inst, "M")

    def test_resign_closed_edge_source_preserves_confluence(self):
        # 重签关闭边源节点 B,结果不变,边仍关闭:合流审批与 token 必须保留
        before = node(self.inst, "M")
        r = apply(self.inst, "B", 1, "approve")
        self.assertTrue(r["ok"])
        self.assertEqual(token(self.inst, "B"), 2)        # 操作目标换代
        after = node(self.inst, "M")
        self.assertEqual(after, before)                    # M 完全不变
        self.assertEqual(after["status"], "signed")
        self.assertEqual(after["result"], "approve")
        self.assertEqual(after["token"], self.m_token)

    def test_resign_open_edge_source_invalidates_confluence(self):
        # 重签开启边源节点 A,即使结果相同,合流审批也必须失效
        r = apply(self.inst, "A", 1, "approve")
        self.assertTrue(r["ok"])
        self.assertEqual(token(self.inst, "A"), 2)
        m = node(self.inst, "M")
        self.assertEqual(m["status"], "pending")           # 需重新收集审批
        self.assertIsNone(m["result"])
        self.assertEqual(m["token"], self.m_token + 1)     # 入边值变化,换代一次
        self.assertEqual(m["incoming"]["A"], ["open", 2])  # 开启边引用新代次

    def test_change_result_on_closed_edge_source_opens_edge(self):
        # B 改签订 reject:关闭边变开启边,合流旧审批失效
        r = apply(self.inst, "B", 1, "reject")
        self.assertTrue(r["ok"])
        m = node(self.inst, "M")
        self.assertEqual(m["status"], "pending")
        self.assertIsNone(m["result"])
        self.assertEqual(m["incoming"]["B"], ["open", 2])

    def test_withdraw_and_resign_stale_token_rejected(self):
        # 撤回再重签后,旧 token 不能提交
        t1 = token(self.inst, "M")
        r = apply(self.inst, "M", t1, "withdraw")
        self.assertTrue(r["ok"])
        self.assertEqual(node(self.inst, "M")["status"], "pending")
        t2 = token(self.inst, "M")
        self.assertGreater(t2, t1)
        r = apply(self.inst, "M", t2, "approve")
        self.assertTrue(r["ok"])
        # 旧 token 全部失效,且不得改变任何节点
        snapshot = inspect(self.inst)
        for stale in (t1, t2):
            r = apply(self.inst, "M", stale, "withdraw")
            self.assertFalse(r["ok"])
            self.assertEqual(r["reason"], "stale_token")
        self.assertEqual(inspect(self.inst), snapshot)

    def test_withdraw_propagates_and_requires_recollection(self):
        # 撤回 A:开启边变未决,合流审批失效;重签 A 后旧审批不得复活
        r = apply(self.inst, "A", 1, "withdraw")
        self.assertTrue(r["ok"])
        m = node(self.inst, "M")
        self.assertEqual(m["status"], "waiting")
        self.assertIsNone(m["result"])
        apply(self.inst, "A", 2, "approve")
        m = node(self.inst, "M")
        self.assertEqual(m["status"], "pending")
        self.assertIsNone(m["result"])                     # 不自动复活
        r = apply(self.inst, "M", m["token"], "approve")   # 重新收集审批
        self.assertTrue(r["ok"])
        self.assertEqual(node(self.inst, "M")["result"], "approve")


class DiamondTest(unittest.TestCase):
    """菱形图:多条路径同时影响共享节点,一次操作只换代一次。"""

    def setUp(self):
        # A -> B -> D, A -> C -> D
        self.inst = create({
            "edges": [
                ("A", "B", "any"), ("A", "C", "any"),
                ("B", "D", "any"), ("C", "D", "any"),
            ]
        })
        apply(self.inst, "A", 0, "approve")
        apply(self.inst, "B", token(self.inst, "B"), "approve")
        apply(self.inst, "C", token(self.inst, "C"), "approve")
        apply(self.inst, "D", token(self.inst, "D"), "approve")
        for n in "ABCD":
            self.assertEqual(node(self.inst, n)["status"], "signed")

    def test_single_apply_bumps_shared_node_once(self):
        tokens_before = {n: token(self.inst, n) for n in "ABCD"}
        r = apply(self.inst, "A", tokens_before["A"], "withdraw")
        self.assertTrue(r["ok"])
        # D 被 B、C 两条路径同时触及,但只换代一次
        self.assertEqual(token(self.inst, "D"), tokens_before["D"] + 1)
        self.assertEqual(token(self.inst, "B"), tokens_before["B"] + 1)
        self.assertEqual(token(self.inst, "C"), tokens_before["C"] + 1)
        self.assertEqual(token(self.inst, "A"), tokens_before["A"] + 1)
        # 下游全部失效,需重新收集
        for n in "BCD":
            self.assertEqual(node(self.inst, n)["status"], "waiting")
            self.assertIsNone(node(self.inst, n)["result"])

    def test_reactivated_branch_requires_fresh_approvals(self):
        apply(self.inst, "A", 1, "withdraw")
        apply(self.inst, "A", 2, "approve")                # 分支重新激活
        for n in "BC":
            self.assertEqual(node(self.inst, n)["status"], "pending")
            self.assertIsNone(node(self.inst, n)["result"])
        self.assertEqual(node(self.inst, "D")["status"], "waiting")
        apply(self.inst, "B", token(self.inst, "B"), "approve")
        apply(self.inst, "C", token(self.inst, "C"), "approve")
        self.assertEqual(node(self.inst, "D")["status"], "pending")
        apply(self.inst, "D", token(self.inst, "D"), "reject")
        self.assertEqual(node(self.inst, "D")["result"], "reject")


class SemanticsTest(unittest.TestCase):
    """基础语义:跳过、非法动作、过期提交、图校验。"""

    def test_skip_on_all_closed_and_cascade(self):
        inst = create({"edges": [("A", "X", "approve"), ("X", "Y", "any")]})
        apply(inst, "A", 0, "reject")                      # 条件是通过,边关闭
        self.assertEqual(node(inst, "X")["status"], "skipped")
        self.assertEqual(node(inst, "Y")["status"], "skipped")
        self.assertEqual(node(inst, "Y")["incoming"], {"X": "closed"})
        # 跳过的节点不能签署
        r = apply(inst, "X", token(inst, "X"), "approve")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "illegal_action")

    def test_waiting_node_cannot_sign(self):
        inst = create({"edges": [("A", "B", "any")]})
        r = apply(inst, "B", 0, "approve")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "illegal_action")

    def test_stale_token_changes_nothing(self):
        inst = create({"edges": [("A", "B", "any")]})
        apply(inst, "A", 0, "approve")
        snapshot = inspect(inst)
        r = apply(inst, "A", 0, "reject")                  # A 的 token 已是 1
        self.assertFalse(r["ok"])
        self.assertEqual(inspect(inst), snapshot)

    def test_resign_same_result_keeps_downstream_only_if_edges_unchanged(self):
        # 根 A 重签相同结果:A 换代,开启边代次变,下游 B 失效
        inst = create({"edges": [("A", "B", "any")]})
        apply(inst, "A", 0, "approve")
        apply(inst, "B", token(inst, "B"), "approve")
        apply(inst, "A", 1, "approve")
        b = node(inst, "B")
        self.assertEqual(b["status"], "pending")
        self.assertIsNone(b["result"])

    def test_multi_root_and_shared_downstream(self):
        # 两个根共享下游;任意结果条件
        inst = create({"edges": [("R1", "S", "any"), ("R2", "S", "any")]})
        apply(inst, "R1", 0, "reject")
        self.assertEqual(node(inst, "S")["status"], "waiting")
        apply(inst, "R2", 0, "approve")
        self.assertEqual(node(inst, "S")["status"], "pending")
        apply(inst, "S", token(inst, "S"), "approve")
        # 撤回 R2 不影响 R1 的审批
        apply(inst, "R2", 1, "withdraw")
        self.assertEqual(node(inst, "R1")["result"], "reject")
        self.assertEqual(node(inst, "S")["status"], "waiting")

    def test_unknown_node_and_action(self):
        inst = create({"nodes": ["A"]})
        with self.assertRaises(KeyError):
            apply(inst, "ZZ", 0, "approve")
        with self.assertRaises(ValueError):
            apply(inst, "A", 0, "maybe")

    def test_cycle_rejected(self):
        with self.assertRaises(ValueError):
            create({"edges": [("A", "B", "any"), ("B", "A", "any")]})

    def test_template_immutable_by_instance(self):
        graph = {"edges": [("A", "B", "any")]}
        inst = create(graph)
        apply(inst, "A", 0, "approve")
        self.assertEqual(graph, {"edges": [("A", "B", "any")]})


if __name__ == "__main__":
    unittest.main()
