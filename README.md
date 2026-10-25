# tollgate · 过闸

> 给 agent 的每一轮请求装两道闸：**这轮该不该发**，和**这轮塞多少**。
> 零依赖、单文件 CLI + 本地只读工作台。Python 3.8+，只用标准库。

```
                        ┌──────────────────────────────┐
    用户任务 ──────────▶ │  闸一 · 计划预算              │
                        │  tg init / add / start        │
                        │  gate / done / handoff        │
                        │  「该不该做、谁来做、几轮」    │
                        └──────────────┬───────────────┘
                                       │ 放行
                                       ▼
                        ┌──────────────────────────────┐
                        │  闸二 · 上下文预算            │
                        │  tg ctx / price               │
                        │  「多大、多少钱、还撑几轮」    │
                        └──────────────┬───────────────┘
                                       │ 快满了
                                       ▼
                              tg handoff N  →  换干净会话
```

---

## 为什么要有第二个闸

关于省 token，市面上主流答案是"压缩提示词"。但把这个账算清楚会发现问题不在那儿：

| 手段 | 省的是什么 | 省多少 |
|---|---|---|
| 压缩输入 | 未命中的输入 token | 一次请求的 ~2/3 输入费，**每轮都要重付一次压缩调用** |
| 少写一半代码 | 账单的**分母** | 分子再省也只有 0~13% |
| 换一个干净会话 | 输入侧几乎全部 | 一次性打到 ~600 tok 的简报 |
| 干脆不做这件事 | 100% | —— |

所以顺序必须是：**该不该做 → 谁来做 → 怎么压**。前两件事没做，压缩最多救 13%。

tollgate 就是把这个顺序做成工具：闸一管前两件事，闸二管第三件事，并且**用同一条价格口径把"压缩"和"换会话"放在一起比价**。

---

## 它糅合了谁的想法

这个项目的每一块机制都不是原创，来源写在下面。**改动**一列是我们实际改掉的部分——不改就只是抄袭，改错了就是自嗨。

| tollgate 里的机制 | 来自 | 改动 |
|---|---|---|
| 计划落盘成文件 + 完成由断言判定 | `planning-with-files` | 断言必须是 shell 真命令，`exit code` 说话；模型自述不算 |
| 依赖图 / 并行批次 / 产物冲突检测 | `ccpm` | 加了"两个步骤写同一个产出物"的冲突检测 |
| 自足式简报（给最弱执行者） | `shadcn/improve` | 简报 ≤600 tok，含禁区、验收命令、停止条件 |
| 同一步重试到上限强制熔断 | `LazyCodex $ulw-loop` | 熔断后必须 `reset n --rounds` 显式重开 |
| 复用七级梯子（最省钱是不做） | `Ponytail` | 梯子档位进计划文件，`report` 统计复用率 |
| 输出侧压缩 | `caveman` | **没抄压缩器**，抄的是"先问这笔压缩值多少钱" |
| 上下文压缩 | `headroom` | 改成与 handoff 的比价：结论是换手恒优 1.5× |
| 按需检索而非全量读 | `codebase-memory-mcp` | 落成计划里的禁读清单 `--exclude` + 产出物声明 |
| 本地只读看板 | `claude-mem` 的 work-view | 用标准库 `http.server` 重写，零依赖、只绑 127.0.0.1、校验 Host 头 |
| 模型档位标签 / 路由 | `claude-code-router` / `laya` | **只保留 `--tier` 标签**，不接管路由——实际路由靠会话内切模型，少一层可坏的中间件 |
| usage 记账与 `cost` | OpenCode 自身 | 不另建账本，直接读宿主的 `session_v2` / `session_message` |
| 轮数口径而不是 token 口径 | 本项目实测踩坑 | 实测 token 预算差 74 倍，于是 `--rounds` 定预算、`--budget` 只当探测器 |

---

## 快速开始

```bash
git clone <this repo> && cd tollgate
python tg.py price            # 先看看宿主的价目对不对得上
python tg.py ctx              # 这一轮多大、多少钱、还能撑几轮
python tg.py init "把 pipeline.py 拆成可测模块" --why "现在改一处要跑全量"
python tg.py add --title "抽出 io 层" --out "src/io.py" --budget 30000 --rounds 3 \
                 --accept "pytest::test_io"
python tg.py serve            # 打开 http://127.0.0.1:7717/
```

装成 skill：

```powershell
.\install.ps1                              # → ~/.config/opencode/skills/tollgate
.\install.ps1 -Target project -ProjectPath "D:\proj"
```

`.plan/` 目录与 `schema 2` 与上游 `plan-ledger` **完全兼容**：老计划用 `tg` 直接读，`tg` 建的计划 `pl.py` 也读得动。

---

## 子命令（24 个）

**闸一 · 计划与预算**

```
init  add  amend  exclude  ready  parallel  graph
start  reset  block  spend  gate  done  handoff
reconcile  report  ack  status  show  resume  drift
```

**闸二 · 上下文与钱**

```
ctx [--json] [--sample] [--session ID]   本轮上下文 / 花费 / 增速预测 / 比价
price                                    列出配置里各模型的价目
serve [--port 7717] [--no-open]          本地只读工作台
```

---

## 数据从哪来，钱怎么算

| 数据 | 来源 | 口径 |
|---|---|---|
| 逐轮上下文 | `session_message.data` 的 `tokens` | `input + cache.read` = 这一轮真正送进模型的量 |
| 逐轮花费 | 同一行的 `cost` | 宿主自己算的，优先采信 |
| 会话累计 | `session_v2` | 只用于"会话级命中率"，**绝不冒充单轮值** |
| 价格表 | `opencode.json` 各 provider 的 `cost` | USD / 百万 token |
| 窗口 | 模型 `limit.context` | 中转若虚标就对不上，会在 `ctx` 里显形 |

计费公式与 OpenCode 一致（实测逐位对账过）：

```
usd = (input×p_in + (output + reasoning)×p_out + cache_read×p_cr + cache_write×p_cw) / 1e6
```

**推理 token 按输出价计**——漏掉这一项就对不上账。

---

## 本地工作台

`tg serve` 起一个只绑 `127.0.0.1` 的只读看板（校验 Host 头，防 DNS rebinding），一屏看到：

- 计划进度条、每步预算 vs 已用、轮数是否超预算
- 本轮上下文占窗口的比例（过半变黄，八成变红）
- **按近 8 轮中位增速，还有几轮撞到 90% 窗口**
- 最近 16 轮的上下文柱状趋势（悬停显示每轮花费与命中率）
- 判断清单：这一轮该 handoff、该修前缀、还是该继续

它是一个静态页 + 一个 JSON 接口，没有构建步骤，没有前端依赖。

---

## 实测样本

在真实长会话上跑出来的样子（40 轮、128k 窗口、中转 `deepseek-v4.1-flash`）：

```
第 40 轮上下文 74.7k tok（窗口 128k 的 58%）
构成 input 227 + cache_read 74.5k，本轮命中率 99.7%
近 8 轮中位增速 +711 tok/轮 → 距窗口 90% 还有 56 轮
这一轮 $0.000316（¥0.0021）（消息自带 cost）
```

含义很直接：**还没到要换手的时候，但按当前增速五十几轮后会到。** 这种"提前几十轮就知道"的信息，是压缩提示词给不了的。

---

## 局限（诚实清单）

1. 只认 OpenCode 的 sqlite 布局；其它宿主需用 `TOLLGATE_DB` 提供同构库。
2. 逐轮数据依赖宿主把 `tokens` 写进消息行；没有就退回采样差值，再没有就报"未知"——**不编数**。
3. 窗口大小取自配置的 `limit.context`，中转虚标就会算错占比。
4. `cost` 值是**上界**：中转的分组倍率通常还会打折。
5. 压缩比固定按 3:1 估，不实测（要用实测得先有压缩器，本项目不做压缩器）。
6. 增速用中位数，遇到"这轮突然读了个大文件"会滞后一轮才反映。
7. 不接管模型路由：`--tier` 只是标签，真路由靠会话内切模型。
8. 看板只读 + 采样，**不能从 UI 改计划**——写操作只走 CLI，才有 append-only 留痕。
9. `.plan/` 与 schema 2 与上游 `plan-ledger` 共享，改格式会同时影响两者。

---

## 测试

```bash
python tests/test_tg.py        # 53 项：计划 / 门禁 / 预算口径 / 兼容
python tests/test_ctx.py       # 25 项：逐轮口径 / 增速 / 比价 / 无数据不编数
python tests/smoke_serve.py    # 14 项：真起服务、真打请求、Host 头校验
```

全部离线可复现：`test_ctx.py` 用构造出来的同构 sqlite 库，不碰真实宿主数据。

---

## 设计约束（四条，不破）

1. **零依赖** 只用标准库，Windows / macOS / Linux 同一份代码
2. **单一真相** `.plan/plan.json` 是唯一真源，`.plan/plan.md` 只是渲染视图
3. **追加写** `ledger.jsonl` / `ctx.jsonl` 只 append 永不改写，崩溃可恢复
4. **确定性门禁** 「完成」由断言 `exit code` 决定，不由模型自述决定

---

## License

MIT
