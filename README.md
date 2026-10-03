# task26100458

课堂日志审批内核:撤签与局部重执行。纯 Python 标准库实现,无第三方依赖,
状态保存在内存,单实例顺序调用。

## 库操作

```python
from approval_kernel import create, apply, inspect

# 模板为不可变 DAG:节点为人工审批,边条件为 approve / reject / any
# 允许多根与共享下游;节点可省略,由边推断
inst = create({
    "edges": [
        ("A", "M", "approve"),          # 元组形式
        {"from": "B", "to": "M", "condition": "reject"},  # 字典形式
    ]
})

apply(inst, "A", 0, "approve")          # {"ok": True, "token": 1}
apply(inst, "B", 0, "approve")
apply(inst, "M", 2, "approve")          # 必须携带目标节点当前代次
apply(inst, "M", 3, "withdraw")         # 撤回到待签署,向下游传播
apply(inst, "M", 3, "approve")          # 过期 token:{"ok": False, ...},不改变任何节点

snapshot = inspect(inst)                # 纯数据快照:token / status / result / 入边值
```

## 核心语义

- 边值:源节点等待/待签署时为 `pending`(未决),跳过时为 `closed`(关闭),
  已签署时按边条件为 `closed` 或 `["open", 代次]`(开启并携带源节点当前代次)。
- 根节点视为有一个固定开启输入;其余节点有未决入边则等待,全关闭则跳过,
  否则待签署。
- 已签署节点仅在仍可签署、且开启入边的 (身份, 代次) 集合与签署时完全相同时
  保留结果;否则清除旧结果,不得自动复活。
- 动作:`approve` / `reject`(待签署可签署,已签署可改签)/ `withdraw`
  (仅已签署)。撤回与改签沿 DAG 传播,重新激活的分支必须重新收集审批。
- 代次(token)单调递增。每次成功 `apply` 后,操作目标以及状态或完整入边值
  相对操作前发生变化的其他节点各换代一次;同一节点被多条路径触及只换代一次;
  下游开启边引用更新后的代次。过期 token 提交为无操作。

## 运行测试

```bash
python3 -m unittest tests.test_kernel -v
```

覆盖场景:合流点(开启+关闭入边)下重签关闭边源保留审批、重签开启边源使审批
失效、撤回再重签后旧 token 失效;菱形图多路径触及共享节点只换代一次;跳过级联、
非法动作、过期提交无操作、多根共享下游、环检测、模板不可变等。
