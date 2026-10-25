# 计划 · 把 pipeline.py 拆成可测模块

> 为什么它该存在：现在改一处要跑全量，改动无法验证
> id `440ecb` ｜ 步骤 3（完成 1）｜ 总预算 75k tok
> 创建 2026-10-25T13:25:33 ｜ 最近变更 2026-10-25T13:25:36

---

## 步骤总览

| # | 标题 | 档位 | 预算 | 依赖 | 复用 | 产物 | 状态 | 实耗 |
|---|---|---|---|---|---|---|---|---|
| 1 | 抽出 io 层 | `cheap` | 30k | - | stdlib | `src/io.py` | OK | 12.9k |
| 2 | 把 fetch 拆成纯函数 | `cheap` | 25k | 1 | new | `src/fetch.py` | · | - |
| 3 | 补回归测试 | `std` | 20k | 2 | existing | `tests/test_fetch.py` | · | - |

### #1 抽出 io 层  [done]

- 复用梯子：3 标准库能做
- 计划内读取：`scripts/pipeline.py`
- 产物：`src/io.py`
- 请求轮数上限：3 轮（超了就 block，别硬撑）
- 验收断言：
    - `file:exists:src/io.py`
    - `cmd:python -m py_compile src/io.py`
- 停止条件（命中就停并汇报，别即兴发挥）：
    - pipeline.py 超过 800 行就停，先汇报

### #2 把 fetch 拆成纯函数  [todo]

- 依赖：#1
- 复用梯子：7 只能新写
- 计划内读取：（未声明 → 先 grep 定位，再决定读不读）
- 产物：`src/fetch.py`
- 请求轮数上限：4 轮（超了就 block，别硬撑）
- 验收断言：
    - `file:exists:src/fetch.py`
    - `nogrep:requests.get@src/fetch.py`
- 停止条件（命中就停并汇报，别即兴发挥）：
    - 测试里出现真实网络请求就停
- 备注：仓库里没有可复用的 fetch 层，前六级都不成立

### #3 补回归测试  [todo]

- 依赖：#2
- 复用梯子：2 仓库里已有
- 计划内读取：（未声明 → 先 grep 定位，再决定读不读）
- 产物：`tests/test_fetch.py`
- 请求轮数上限：3 轮（超了就 block，别硬撑）
- 验收断言：
    - `file:exists:tests/test_fetch.py`
    - `file:absent:scripts/old_fetch.py`
- 停止条件（命中就停并汇报，别即兴发挥）：
    - 断言与现有测试冲突就停，先汇报

