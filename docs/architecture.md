# tollgate 架构

## 一句话

一个文件（`tg.py`）+ 一层静态页（`ui/`）。左边管计划，右边管上下文，两边共用一个数据底座。

```
                       ┌────────────────────────────────────────────┐
                       │                tg.py（单文件 CLI）           │
                       │                                            │
  .plan/plan.json ◀──▶ │  基础设施  find_root / paths / load / save  │
  .plan/ledger.jsonl◀─▶│  渲染     render / plan.md                  │
  .plan/ctx.jsonl  ◀──▶│  断言     run_check / run_gates             │
                       │  命令     cmd_*(25 个)                      │
                       └───────┬──────────────────────┬─────────────┘
                               │                      │
            闸一：plan 模型      │                      │  闸二：用量模型
            （磁盘上的契约）     │                      │  （宿主的账本）
                               ▼                      ▼
                    ┌──────────────────┐   ┌──────────────────────────┐
                    │ .plan/plan.json  │   │ opencode.db（只读 URI）   │
                    │ ├ steps[]        │   │ ├ session_v2     累计     │
                    │ │  out / accept  │   │ ├ session_message 逐轮    │
                    │ │  budget/rounds │   │ └ message       旧布局兜底 │
                    │ └ exclude[]      │   │                          │
                    └──────────────────┘   │ opencode.json（价目表）    │
                                           └──────────────────────────┘
```

## 三个数据面的关系

| 面 | 谁写 | 谁读 | 作用 |
|---|---|---|---|
| `.plan/plan.json` | `init` / `add` / `amend` | 全部命令、看板 | **唯一的计划真相** |
| `.plan/ledger.jsonl` | `spend` / `sync` / `gate` / `done` / `reset` | `report` / 看板 | 只追加的审计流水 |
| `.plan/sync.json` | `sync` | `sync` 自己、看板 | 对账游标：**每个会话各一份**已入账的消息 id（schema v2） |
| `opencode.db` | 宿主（OpenCode）自己 | `ctx` / `price` / `sync` / 看板 | 真实 token 与钱，**只读** |

关键约束：**tollgate 从不写宿主的库**。`opencode.db` 用 `file:<path>?mode=ro` 打开，避免和正在跑的会话抢锁。反过来，宿主也从不知道 tollgate 的存在——这是"能随时卸载"的前提。

## `tg.py` 的分区

文件按固定顺序分段，每段前面有 `# ---- 分隔注释`：

| 段 | 内容 |
|---|---|
| 头部 docstring | 版本、四个设计约束、向后兼容承诺 |
| 基础设施 | `find_root`（向上找 `.plan/`）、`paths`、`load`、`save`、`append_ledger`、`sh`、`git_drift` |
| 渲染 | `render` 把 plan.json 渲成 plan.md |
| 断言 | `parse_pair`、`run_check`、`run_gates`、`gate_report` |
| 命令 | 21 个闸一命令 |
| **价格与账本** | `opencode_config` / `load_prices` / `opencode_db` / `read_sessions` / `current_session` / `cost_usd` |
| **逐轮数据** | `read_turns`（解析消息里的 `tokens`）/ `growth_rate`（中位数增速） |
| **上下文报告** | `ctx_report`（唯一的口径聚合点）/ `cmd_ctx` / `cmd_price` |
| **两闸交界** | `load_cursors` / `save_cursors`（按会话分开的游标，v1 读时自动升级）/ `sync_boundary`（这一步从什么时候开始算）/ `cmd_sync`（把逐轮用量写进账本） |
| **工作台** | `state_payload`（把上面全部打包成 JSON）/ `_make_handler` / `cmd_serve` |
| 入口 | argparse，25 个子命令 |

## 一次 `tg ctx` 的数据流

```
current_session()          挑会话：--session > $OPENCODE_SESSION_ID > 最近更新
      │
      ├── read_turns(sid)  读 session_message + message，解析 tokens
      │        │
      │        ├── 最后一行 → 本轮上下文 = input + cache.read
      │        └── 近 8 行 → growth_rate() 中位差分 → 还有几轮撞 90% 窗口
      │
      ├── load_prices()    opencode.json 里各模型的 cost / limit.context
      │
      ├── cost_usd()       与宿主同口径：reasoning 按输出价
      │
      └── verdict[]        规则化判断，每条都可解释、都可回指到某个数字
```

`ctx` 的每条判断都必须能指回一个具体数字（窗口百分比、命中率、增速、轮数）。**指不回去的话就不说。**

## 三层兜底（为了保证不编数字）

| 情况 | 用什么 | 输出 |
|---|---|---|
| 宿主有逐轮 tokens | `read_turns` | 精确单轮值 + 增速预测 |
| 只有会话累计 | `.plan/ctx.jsonl` 两次采样差值 | 增量值，标注"两次采样以来" |
| 什么也没有 | 计划里的 `--baseline-context` | 估计值，标注来源 |
| 连估计也没有 | —— | `estimate.tokens = null`，明说"未知" |

绝不把累计值冒充单轮值。这是这个项目最容易犯、也最致命的错误（曾经真的算出过"占窗口 28810%"）。

## 两闸交界：`tg sync` 的数据流

闸一需要"这一步花了多少"，闸二手里有"每一轮真实花了多少"，而人肉把后者抄成前者是纯粹的浪费。

```
current_session()            挑会话（同 ctx）
      │
      ├── load_cursors()    读 .plan/sync.json：**这个会话**已入账的 id 集合
      │                     （别的会话的游标原样留着，不动）
      │
      ├── sync_boundary()   归账边界：这步的 started_ms → 计划 created → 不知道
      │
      ├── read_turns(sid)    最新 2000 轮，逐轮 tokens + cost
      │        │
      │        ├── 早于边界的轮次 → skipped[]（同样报出来，不进账本也不进游标）
      │        └── 边界之后的，过滤掉已在游标里的 → fresh[]
      │                 │
      │                 ├── in  = Σ input
      │                 ├── out = Σ (output + reasoning)
      │                 ├── reqs= len(fresh)
      │                 └── cost= Σ cost   （为 0 时按 load_prices 重算，标 cost_est）
      │
      ├── append_ledger()    写一笔 {step, in, out, reqs, cache_read, cost, src, session}
      └── save_cursors()     这个会话的游标前移（上限 2000 条），其它会话不动
```

五个设计决定：

1. **口径不新开**：`sync` 写的就是 `spend` 那个格式，`spent_for()` / `reqs_for()` 一行都不用改。
   缓存读单列存进记录但不参与求和——预算口径是"新 token"，缓存命中只是重读。
2. **游标窗口 = 读窗口**（都是 2000）。两者必须相等才能同时保证"读到的都认得"和
   "认得的都在读窗口里"，于是既不重复记账、也不会因为游标比窗口窄而反复重记。
3. **归账宁可拒绝**：没有 `doing` 的步骤时直接非 0 退出，而不是猜一个步骤记上去。
   记到错的地方比不记更坏——账本的价值全在可信。
4. **游标按会话分开**：一次 `sync` 只处理一个会话，但每个会话各留一份已入账 id。
   曾经只有一份扁平游标、换会话就清空，于是 A→B→A 会把 A 整段重记（实测 4 轮 /
   $0.0011 记成 7 轮 / $0.0014）。会话是几根并行的计数器，不是一根。
5. **归账要划边界**：读窗口（最近 2000 轮）比"这一步"大得多，整段算给当前步就是错的归属
   （真机实测：计划刚建立，第 1 步吃下整段对话 1102 轮 / $0.486120）。`tg start` 写
   `steps[].started_ms` 当边界，被挡的轮次报出来但不入账；边界未知时明说不知道。

## 工作台为什么是静态页

`ui/` 里只有三个文件：`index.html` / `app.js` / `style.css`，没有任何构建链。

- `GET /api/state` → `state_payload()` 打包好的 JSON（计划 + ctx + 价目 + 判断）
- `GET /` `/app.js` `/style.css` → 白名单静态文件
- `POST /api/sample` → 追一条采样

安全边界：只绑 `127.0.0.1`，每个请求校验 `Host` 头（防 DNS rebinding），静态路径是白名单而非字符串拼接（防目录穿越）。

页面每 3 秒轮询一次。不做 WebSocket，因为数据量小、延迟不重要，而 SSE/WS 会让"零依赖"这个约束破在部署上。

### `state_payload()` 的两个新增块

| 块 | 内容 | 诚实处理 |
|---|---|---|
| `money` | 账本里真实记过的 `cost` 求和、笔数、轮数、单轮均价、宿主累计、**同期别的会话花了多少**（`other_sessions` / `other_usd`）、**钱按会话分列**（`by_session`：`session` / `usd` / `rows` / `reqs`，没会话 id 的旧行 `session` 为空串） | 一笔 cost 都没有时 `ledger_usd = null`，前端显示「没有」并指路 `tg sync`；没有别的会话时 `other_usd = null`，卡片不出现 |
| `sync` | **正在看的那个会话**的游标状态（`synced_ids` / `same_session` / `updated`，`last_session` 是最后一次 sync 的会话）、账本里一共记过几个会话（`sessions`）、窗口内未入账轮数 | 未入账数只算**手上这 40 轮窗口**，而且只跟**这个会话**的游标比——拿别的会话的游标比会直接算错 |

`money` 是**快照**：账本记的是上次 `sync` 那一刻的数，宿主累计一直在涨。两者差值 ≈ 上次对账
之后又跑掉的钱，界面把"截至上次 sync"写在卡片上，避免被当成实时值。

`other_usd` 是**下限的另一半**：`sync` 一次只认一个会话，所以账本里的钱是花费下限。计划建立
之后仍在花钱的别的会话（子 agent、另一个窗口）单独列一张「同期在花」卡片——它不进账本，
但看板上不能假装它不存在。计划之前跑过的会话不计入，那跟这件事无关。要把它们补进账本，
逐个 `tg sync --session <id>` 就行：游标按会话分开存，来回切多少次都只记一次。

代价：`state_payload()` 每轮多调一次 `read_turns(sid, limit=CTX_WINDOW)`（40 行）来算
`pending_in_window`。数据量小，3 秒一次完全够；如果哪天宿主库变得很大，这里是第一个该加缓存的地方。

## 加一个宿主适配器要改哪

只改四个函数，其余不动：

1. `opencode_db()` —— 返回库路径（或读 `TOLLGATE_DB`）
2. `read_sessions()` —— 返回会话累计行，字段名照现有 dict
3. `read_turns()` —— 返回逐轮 `tokens`
4. `opencode_config()` / `load_prices()` —— 返回价目表

`ctx_report` 之后的一切都不感知宿主差异，因为它们只吃标准化后的 dict。

## 不变式

1. `plan.json` 是唯一真源；`plan.md`、看板、`status` 都是它的视图
2. `ledger.jsonl` / `ctx.jsonl` 只 append，不改写，不重排
3. 门禁结论只来自子进程 `exit code`
4. 宿主的库只读；不在别人的文件里留状态
5. 任何"算不出来"的地方输出 `null` + 原因，不填近似值
