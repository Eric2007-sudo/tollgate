# 贡献指南

## 先跑起来

```bash
git clone https://github.com/Eric2007-sudo/tollgate.git
cd tollgate
python tg.py -V                 # 应当打印版本号
python tests/check_doc_counts.py   # 563 项，全绿才算能改
```

零依赖，Python 3.8+，只用标准库。不需要 pip install。

## 改动前后都跑这一条

```bash
python tests/check_doc_counts.py
```

它会真跑五个套件，并核对 README 里写的项目数与实测一致。
**改了测试数量就必须同步改 README**，否则这条会红——这是故意的：
README 曾经把 115 项写成 56 项，没人发现。

```
tests/test_tg.py      213 项   计划 / 门禁 / 预算口径 / 兼容 / BOM / 超时 / --json 接缝
tests/test_ctx.py      25 项   逐轮口径 / 增速 / 比价 / 无数据不编数
tests/test_sync.py    115 项   对账口径 / 幂等 / 增量 / 游标容错 / 归账边界
tests/test_orch.py    155 项   角色契约 / 四道刹车 / 开工七问 / 简报归属 / 分批
tests/smoke_serve.py   55 项   真起服务、真打请求、Host 头校验
```

全部离线可复现：构造同构 sqlite 库，一个模型都不调。

## 四条不破的约束

1. **零依赖。** 只用标准库。要加第三方包请先开 issue 说明为什么不能手写。
2. **单一真相。** `.plan/plan.json` 是唯一真源，`.plan/plan.md` 只是渲染视图。
3. **追加写。** `ledger.jsonl` / `ctx.jsonl` 只 append，永不改写，崩溃可恢复。
4. **确定性门禁。** 「完成」由断言 `exit code` 决定，不由模型自述决定。

## 提交信息怎么写

一条提交说清**修了什么、为什么、代价是什么**。参考已有提交：

```
orch 0.4.0 / tg 0.4.2：简报从来不发步骤正文，reset 还会把它清空
```

带版本号前缀（`tg x.y.z` / `orch x.y.z`），并在正文里写实测代价。
**不要写「优化代码」「修复问题」这种没有信息量的句子。**

## 提 issue 时带上这些

- `python tg.py -V` 与 `python orch.py --version` 的输出
- 复现命令（越短越好）
- 期望结果 vs 实际结果
- 如果和账本口径有关，附 `tg report --json`（**先删掉会话 id 与绝对路径**）

## 关于数字

这个项目的文档里所有数字都必须是**实测**的。做不到就写 `unknown`，不估算、不四舍五入成好看的数。
如果你在 PR 里写了一个数，请说明它是怎么跑出来的。
