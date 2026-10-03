# 课堂日志审批内核：撤签与局部重执行

纯 Python 内存实现，无外部依赖、无服务端口。模板为不可变 DAG，
节点为人工审批，边条件为 通过 / 拒绝 / 任意，支持多根与共享下游。

## 库操作

- `create(graph)`：依据模板（`Graph` 或 dict 规格）创建实例并达到初始稳定状态。
- `apply(instance, node, token, action)`：对节点执行 `Action.APPROVE` /
  `Action.REJECT` / `Action.WITHDRAW`；token 必须等于节点当前代次，
  过期提交返回 `False` 且不改任何节点。成功时目标节点及状态/入边值
  变化的节点各换代一次，并沿拓扑序传播失效。
- `inspect(instance)`：返回快照，含各节点状态、结果、代次（token）、
  签署证据与每条入边的取值（未决 / 关闭 / 开启+源代次）。

## 运行测试

```sh
python3 -m pytest tests/ -v
```

## 文件

- `approval_kernel.py`：内核实现（类型定义 + 三个库操作）。
- `tests/test_approval_kernel.py`：合流证据失效、菱形共享节点单次换代、
  撤回传播、跳过/重激活、过期 token 等场景测试。
