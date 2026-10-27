# tollgate

**给 agent 的每一轮请求装两道闸：这一轮该不该发，以及这一轮要塞多少上下文。**

零依赖（Python 3.8+ 标准库）、单文件 CLI、本地只读看板、一个并发编排层。
「这一步做完了吗」只认 shell 断言的 `exit code`，不认模型自述。

```
                    用户任务
                        │
┌────────────────────────────────────────────────┐
│ 闸一 · 计划与预算                              │
│ init · add · start · gate · done               │
│ 问：该不该做 / 谁做 / 几轮 / 最多花多少        │
└───────────────────────┬────────────────────────┘
                        │ 放行：断言与预算都过
                        ▼
┌────────────────────────────────────────────────┐
│ 闸二 · 上下文预算                              │
│ ctx · price · handoff                          │
│ 问：这一轮多大 / 多少钱 / 按增速还撑几轮       │
└───────────────────────┬────────────────────────┘
          ┌─────────────┴─────────────┐
          │ 还撑得住                  │ 快满（窗口 90%）
          ▼                           ▼
     继续这一轮             换干净会话（handoff）
```

产物始终是同一本账：`.plan/plan.json`（计划）· `.plan/ledger.jsonl`（账本）· `.plan/gates.log`（门禁记录）。

---

## 1. 实现了什么

### 1.1 一页能力表

| 能力 | 命令 | 产出 |
|---|---|---|
| 计划落盘、依赖图、并行批次、产物冲突检测 | `init` `add` `amend` `ready` `parallel` `graph` | `.plan/plan.json` |
| 确定性门禁：断言 `exit code` 决定完成与否 | `gate` `done` `reconcile` | `.plan/gates.log` |
| 账本：从宿主 sqlite 读逐轮 tokens/cost，按会话分游标增量记账 | `sync` `report` `spend` | `.plan/ledger.jsonl` |
| 花费刹车（美元，累计口径，无 `--force` 后门） | `init --max-cost` / `amend 0` | 到顶拒绝 `start` |
| 上下文预算：当前占比、增速预测、压缩 vs 换手比价 | `ctx` `price` | stdout / `--json` |
| 本地只读工作台（标准库 `http.server`，只绑 127.0.0.1） | `serve` | `http://127.0.0.1:7717/` |
| 多角色并发编排：四道刹车 + 开工七问 + 按会话 id 点名归账 | `orch.py role/fan/dispatch/rollup` | `.orch/` + 每路一份简报 |
| 机器可读出口：stdout 恰好一个 JSON，人话全走 stderr | `--json` | `"api": 1` |

### 1.2 三个关键设计

**① 完成由断言决定，不由模型决定。**
每个步骤的 `accept` 是 shell 断言（`file:exists` / `file:absent` / `grep` / `nogrep` / `cmd` / `ack`）。
`tg gate N` 真跑一遍，全过 exit 0，有一条破 exit 1，超时单独记 `TIMEOUT`（没验完 ≠ 破了）。
`tg done N` 只认这个 exit code。模型说「做好了」不构成任何证据。

**② 账本从宿主读，不手记。**
`tg sync` 读 `~/.local/share/opencode/opencode.db` 的 `session_message`，把「上次 sync 之后的新轮次」写进账本：

| 账本字段 | 取什么 |
|---|---|
| `in` | Σ `tokens.input`（缓存命中不计入） |
| `out` | Σ(`output` + `reasoning`)，推理 token 按输出价计费 |
| `reqs` | 带 tokens 的 assistant 消息条数 |
| `cost` | Σ 每轮真实 `cost`；宿主没记则按配置单价重算并标 `cost_est` |
| `cache_read` / `cache_write` | 单列存，**不参与预算求和** |

游标按会话分开存（`.plan/sync.json` v2），所以同一轮不会被重复记账，多跑几次 `sync` 是安全的。
归账边界由 `tg start` 写入 `steps[].started_ms`，更早的轮次不算这一步的。

**③ 三层门控，生死只看第一层。**
断言（`exit code`，唯一能否决 `done` 的）／语义评审（只出概率，永不否决 `done`）／人（只在两层打架时介入）。
`orch rollup` 三层分列打印，不写「AI 评价通过」这种合体句。

### 1.3 不是什么

- **不是 agent runtime**：不排队、不重试到成功、不接管会话生命周期。`orch dispatch` 只把已过检的简报交给 `opencode run`，收 exit code 与会话 id。
- **不是模型网关**：不改路由、不代理请求、不存你的 key。`--tier` 只是标签。
- **不是公开多人平台**：单机自用。账本口径是「这台机器上这个任务花了多少」，多人共享会把归属算错。
- **不做主观打分**：不输出「完成度 87%」这类加权总分。

---

## 2. 如何使用

### 2.1 安装

```powershell
git clone <repo> && cd tollgate
.\install.ps1                                  # → ~/.config/opencode/skills/tollgate
.\install.ps1 -Target project -ProjectPath "D:\proj"
```

不需要 pip。`tg.py` 与 `orch.py` 都是单文件，直接 `python tg.py <子命令>` 也能用。

### 2.2 三分钟上手

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'

python tg.py price                 # 先确认宿主的价目读得到
python tg.py ctx                   # 这一轮多大、多少钱、还能撑几轮

python tg.py init "把 pipeline.py 拆成可测模块" --why "改一处要跑全量" --max-cost 0.5
python tg.py add --title "抽出 io 层" --out "src/io.py" --rounds 3 `
                 --accept "pytest::test_io" --accept "file:exists:src/io.py"

python tg.py start 1               # 拿归账边界，开始
# ……干活……
python tg.py sync                  # 把这一段的真实用量写进账本
python tg.py gate 1                # exit code 说话
python tg.py done 1                # 过了才标记完成
python tg.py status                # 进度 / 预算 / 钱
python tg.py serve                 # 看板
```

断言写法（每种前缀都真跑）：

```
file:exists:src/io.py
file:absent:src/tmp.py
grep:def test_io@tests/test_io.py
nogrep:TODO@src/io.py
cmd:python -m pytest tests/test_io.py -q
ack:看板宽度
```

`grep:` / `nogrep:` 的 pattern 是**正则**，`@` 后面必须是**文件**路径（不支持目录）。

### 2.3 命令分组

**闸一 · 计划与预算**

```
init  add  amend  exclude  ready  parallel  graph
start  reset  block  spend  gate  done  handoff
reconcile  report  ack  status  show  resume  drift
```

**闸二 · 上下文与钱**

```
ctx [--json] [--sample] [--session ID]    本轮上下文 / 花费 / 增速 / 比价
price                                     列出配置里各模型的价目
serve [--port 7717] [--no-open]           只读工作台
```

**两闸交界**

```
sync [--step N] [--session ID] [--dry-run] [--json]
```

**编排层**

```
python orch.py role add <名字> --dir <目录> --task "<做什么>" [--lead]
python orch.py role list
python orch.py fan N                       只出简报，一个进程都不起
python orch.py dispatch N --roles a,b --concurrency 2 --max-usd 3.0
python orch.py rollup --gate --cost        三层汇总
```

### 2.4 几个必须知道的口径

| 事项 | 口径 | 为什么 |
|---|---|---|
| `--max-cost` / `--max-usd` | **账本累计**（含历史），不是「这一轮还能花多少」 | 到顶就拒绝 `start`／一条会话都不起 |
| 账本金额 | **下限**（同期别的会话、没 sync 的轮次不在里面） | 拿下限当上限只会提前拦，方向安全 |
| `--max-fanout` vs `--concurrency` | 前者 = 这批**总共**派几路；后者 = **同时**跑几路 | 合并成一个数会逼人「想省时就得一次全烧」 |
| `gate --json` 多步 `doing` 且未点名 | `ok:false`、`ambiguous:[...]`、exit 2 | 替你挑一个等于给错的步骤发通行证 |
| `status --json` | `doing` 是权威字段（列表）；`current` 只是「第一个在跑的」 | 并发时只看 `current` 会漏掉另一路的钱 |
| `sync` 一次 | 只认**一个**会话，按会话各存游标 | 换会话不会吃别处轮次，回来也不重记 |

机器可读出口统一契约：stdout 上是**一个** JSON 对象，带 `"api": 1`；提示与错误全走 stderr。
`orch.py` 只吃这条契约，不去抠人类可读输出里的数字。

### 2.5 本地区域

`tg serve` 起一个只绑 `127.0.0.1` 的只读页面，校验 Host 头（防 DNS rebinding），一屏看：
计划进度、每步预算 vs 已用、每步真实花费、账本快照、同期在花、对账状态、
当前上下文占比、按近 8 轮中位增速还有几轮撞到 90% 窗口、最近 16 轮趋势。

---

## 3. 实测结果

数字全部来自账本与产物文件。没有估的；拿不到的写 `unknown`。

### 3.1 用在两个真项目上

| 项目 | 规模 | 步数 | 花费 | token 预算 vs 实际 |
|---|---|---|---|---|
| `bench`（Agent 运行观测与评测台，v0.2 四维） | 单文件 CLI × 4 模块 + 交互驱动 | **12/12** | **$0.704686**（主账本 $0.694001 + 交互独立账本 $0.010685） | 978k → **2476.9k（253%）** |
| `tally`（对话式 Excel 数据分析） | 五数口径 + JOIN + 一致性核验 | **11/11** | **$1.518486**（上限 $1.80，84%） | 1198k → **6402.4k（534%）** |

`bench` 的 `tg reconcile` 重跑 12 步全绿；`tally` 的 11 步门禁当时全过（本轮未重跑对账）。
`bench` 的独立验证器合计 **121 项断言（0 FAIL / 0 SKIP）**，全部由上层另写、子会话不许改。

**token 预算一贯低估 2.5–5 倍**：这不是功能变贵，是预估按「一次成型」算轮数、没算返工。所以本项目用 `--rounds` 定预算，`--budget` 只当探测器。

### 3.2 门禁到底提高了什么：A/B 四格

同一份简报、同一批文件、同一模型，只差「给不给角色目录边界与验收断言」。
两个任务都是**规格写死**的（复刻 bench #4 判分器、复刻 bench #6 导出三件套）。

| 任务 | 臂 | 首次通过 | 打回 | 成本 USD | 可追溯 | 轮次 |
|---|---|---|---|---|---|---|
| t1 | A 裸跑 | 是 | 0 | 0.064563 | 3/3 | 49 |
| t1 | B tollgate | 是 | 0 | 0.060753 | 3/3 | 69 |
| t2 | A 裸跑 | 是 | 0 | 0.035481 | 3/3 | 39 |
| t2 | B tollgate | 是 | 0 | 0.050078 | 3/3 | 26 |

四格并列，**不给加权总分**。已跑的四格里，裸跑 **4/4 首次通过**。

三条结论，都有限定条件：

1. **在「规格已经写死」的任务上，门禁没有提高首次通过率。** 断言本身就等于规格，裸跑的模型照着做也能一次过。
2. **t2 是最有信息量的一格**：B 臂多花 **41%**，md 导出实质更差，**却 10 条断言全绿**。
   → 「过门禁」与「需求满足」是两件事。断言只覆盖它写了的部分。
3. **B 臂买到的是可追溯性与边界**（3/3 交付可追溯、越界写盘 0），不是更高的通过率。

未跑的 t3 两格（约 $0.15–0.20）已停：前四格没有出现「裸跑需要返工」，继续测的边际信息量不足以支撑成本。

### 3.3 对账抓到了门禁漏掉的回归

`bench` 第 12 步第一次交付时：**门禁 14 条全绿、独立验证器 56 条全绿**。
`tg reconcile` 重跑全部 12 步后立刻变红：

```
!! #7  view/ 看板 … 现在不通过了
       cmd:python ../_scratch/verify_bench7c.py  (exit=1)
       A：out/fx.json 与 run.json 逐字节相同（8165 vs 6037）
```

原因是第 12 步为了塞四维，把 `out/fx.json` 从「`run.json` 的逐字节副本」改成了带附加字段。
**单步门禁再严，也防不住「后一步改坏前一步」**——这一层只能靠 `reconcile` 重跑历史断言。
修法是把四维挪到 `out/dimensions.json`，保住原不变式。

### 3.4 工具自己被测出来的三个缺陷（已修）

狗粮期间，三个 bug 都伪装成了「任务完成」：

| 缺陷 | 表现 | 为什么难发现 |
|---|---|---|
| `tg reset` 无条件把 `note` 置空 | 「改简报 → 重跑」会静默清空简报正文 | 清空的字段当时没人读 |
| `orch` 简报模板**没有 `{note}` 占位符** | 步骤的具体要求**从未发给过子会话** | 子会话照兜底句办事，exit 0 |
| 两者叠加 | 门禁全绿、产物却与目标相反 | 只有人工读产物才能发现 |

修法：`reset` 不再碰 `note`；模板加 `{step_note}` 段；`prepare_fan` 对空 note 直接拒派。
配套破坏性验证 `_scratch/verify_orch_emptynote.py`：空 note → exit 2；正常 note → 简报 2434 → 5487 字节。

### 3.5 一个真实会话上的样子

128k 窗口、中转账目：

```
某一轮上下文 74.7k tok（窗口 128k 的 58%）
构成 input 227 + cache_read 74.5k，本轮命中率 99.7%
近 8 轮中位增速 +711 tok/轮 → 距窗口 90% 还有 56 轮
这一轮 $0.000316（¥0.0021）（消息自带 cost）
```

714 轮长会话的一次对账：

```
本次同步 714 轮
新输入 2384.8k + 输出 360.5k（其中推理 215k）= 2960.3k tok
缓存读 45495.4k / 写 0 —— 不计入预算
钱：$0.276054（¥1.8466） · 宿主记录
```

两个数值得注意：**缓存读是新输入的 19 倍**（省钱着力点是缩短每轮要重读的前缀，不是少写两行新代码）；
**推理 token 占输出 60%**（记账漏掉它，钱就对不上）。

### 3.6 测试

```powershell
python tests\check_doc_counts.py   # 真跑下面五个套件，并核对 README 写的项目数
```

| 套件 | 项数 | 覆盖 |
|---|---|---|
| `tests/test_tg.py` | 213 | 计划 / 门禁 / 预算口径 / 兼容 / BOM 容错 / 断言超时 / `--json` 接缝 / 并发上限 / 依赖成环 / 花费到顶拒绝 |
| `tests/test_ctx.py` | 25 | 逐轮口径 / 增速 / 比价 / 无数据不编数 |
| `tests/test_sync.py` | 115 | 对账口径 / 幂等 / 增量 / dry-run / 游标容错 / 归账边界 / 多路不猜归属 |
| `tests/test_orch.py` | 155 | 角色契约 / 目录重叠拒绝 / 四道刹车 / 开工七问 / 简报归属 / dispatch 分批与 dry-run 不花钱 |
| `tests/smoke_serve.py` | 55 | 真起服务、真打请求、Host 头校验、`/api/reviews` 人确认进度 |
| **合计** | **563** | 全部离线可复现：造同构 sqlite 库，一个模型都不调 |

### 3.7 已知边界

1. 只认 OpenCode 的 sqlite 布局；其它宿主需 `TOLLGATE_DB` 提供同构库。
2. 逐轮数据依赖宿主把 `tokens` 写进消息行；没有就报 `unknown`，**不编数**。
3. 窗口取自 `limit.context`，中转虚标就会算错占比。
4. `cost` 是**下限**（未 sync 的会话不在账本）；中转分组倍率还会再打折。
5. 压缩比固定按 3:1 估，不实测（本项目不做压缩器）。
6. 增速用中位数，「这轮突然读了个大文件」会滞后一轮。
7. `sync` 读窗口是最新 2000 轮；超长会话会明说，别把那次结果当成会话总花费。
8. `ctx` 只看最近 40 轮，所以它**不说「第 N 轮」**——那个 N 只是窗口大小。
9. 语义评审（`jev`）需云端 key，数据离开本机；想零外传用 `reviewer=none`。它只出概率，永不否决 `done`。
10. `reconcile` 重跑全部已完成步骤的门禁，没有缓存、没有增量——「上次验过」不算数，这正是不骗人的代价。

---

## 4. 参考资料

本项目没有原创机制，每一块都能指到来源。「改动」一列是我们实际改掉的部分。

### 4.1 机制来源

| 机制 | 来源 | 改动 |
|---|---|---|
| 计划落盘成文件 + 完成由断言判定 | `planning-with-files` | 断言必须是 shell 真命令，`exit code` 说话 |
| 依赖图 / 并行批次 / 产物冲突检测 | `ccpm` | 加「两个步骤写同一个产出物」的冲突检测 |
| 自足式简报（给最弱执行者） | `shadcn/improve` | 简报 ≤600 tok，含禁区、验收命令、停止条件 |
| 同一步重试到上限强制熔断 | `LazyCodex` 的 `ulw-loop` | 熔断后必须 `reset n --rounds` 显式重开 |
| 复用七级梯子（最省钱是不做） | `Ponytail` | 梯子档位进计划文件，`report` 统计复用率 |
| 输出侧压缩 | `caveman` | 不抄压缩器，只问「这笔压缩值多少钱」 |
| 上下文压缩 | `headroom` | 改成与 handoff 比价，结论是换手恒优约 1.5× |
| 按需检索而非全量读 | `codebase-memory-mcp` | 落成 `--exclude` 禁读清单 + 产出物声明 |
| 本地只读看板 | `claude-mem` 的 work-view | 用标准库 `http.server` 重写，零依赖、校验 Host 头 |
| 语义评审（只出概率） | TypeSafe Jev | **只留概率与证据**，`verdict` 不能否决 `done` |
| 模型档位标签 / 路由 | `claude-code-router` / `laya` | 只保留 `--tier` 标签，不接管路由 |
| usage 记账与 `cost` | OpenCode 自身 | 不另建账本，直接读宿主 `session_v2` / `session_message` |
| 轮数口径而非 token 口径 | 本项目实测 | 见 §3.1：token 预算低估 2.5–5 倍 |

### 4.2 外部依据

| 用途 | 出处 |
|---|---|
| 多 agent 的 token 代价与并行度限制 | Anthropic, *Building a multi-agent research system*（15× token 换 +90.2%，并明说编码任务并行度低） |
| 多 agent 失败模式分类（cost runaway 居首） | MAST, *Why Do Multi-Agent LLM Systems Fail?*（1642 条轨迹） |
| 多轮交互评测：LLM 扮演用户 vs 脚本化用户、pass^k | `sierra-research/tau-bench` |
| 多轮 + model-graded 评测框架 | `UKGovernmentBEIS/inspect_ai` |
| 指令 + 测试脚本 + oracle 的任务形式 | Terminal-Bench |
| 字段命名（只借名字，不引运行时） | OpenTelemetry GenAI semantic conventions（`gen_ai.invoke_agent.duration` / `gen_ai.server.time_to_first_token` 等） |
| 宿主数据来源 | OpenCode 本地 sqlite（`session_v2` / `session_message`） |
| 上游计划格式 | `plan-ledger`（`.plan/` 与 `schema 2` 双向兼容） |

### 4.3 深入文档

| 文件 | 内容 |
|---|---|
| `references/gates.md` | 两道闸的守卫点、断言语法、门禁为什么拒 |
| `references/orchestration.md` | 角色/目录边界、四道刹车 + 开工七问、`review-*.json` 字段格式 |
| `references/plan-format.md` | 计划 / 账本 / 游标字段级格式；删 `.plan/` 里哪个文件会出什么事 |
| `references/context-budget.md` | 计费公式、缓存命中、换手结论、预算口径 vs 钱 |
| `references/tiers.md` | 三档判据、模型 ID 映射、用账本真实单轮价验证档位 |
| `references/handoff.md` | 简报怎么写、三种交付方式、换手与「回来」各自的钱 |
| `references/anti-patterns.md` | 29 条真实失败模式 |
| `docs/architecture.md` | 模块划分与数据流 |
| `docs/dogfood.md` | 一次真实任务的战报，含真实账本数字 |
| `docs/jev-onboarding.md` | 语义层首次接入的验收台账与七个坑 |
| `bench/REPORT-v0.2.md` | 四维评测台的完整交付报告（分账、缺陷、复现） |
| `abtest-gate/METHOD.md` | A/B 的唯一变量表、四数定义、六条「不能支撑什么」 |

---

## 5. 设计约束（不破）

1. **零依赖**：只用标准库，Windows / macOS / Linux 同一份代码。
2. **单一真相**：`.plan/plan.json` 是唯一真源，`.plan/plan.md` 只是渲染视图。
3. **追加写**：`ledger.jsonl` / `ctx.jsonl` 只 append，永不改写，崩溃可恢复。
4. **确定性门禁**：「完成」由断言 `exit code` 决定，不由模型自述决定。

## License

MIT
