# 计划文件格式

## 五个位置，一份真相

```
.plan/
  plan.json      ← 唯一真相。CLI 读写，不要手改
  plan.md        ← 渲染视图。人看、模型按需读。每次改动自动重写
  ledger.jsonl   ← 追加写。一行一笔 token 消耗，永不改写
  gates.log      ← 每次 gate 的历史
  notes/         ← 每步的产物（纯分析步骤的交付物）
  brief/         ← handoff 生成的、给便宜模型的自足简报
```

**规则**：要改计划，用 `tg` 改；不要手写 `plan.json`。
`plan.md` 是生成物，手改会在下次渲染时被覆盖。

---

## plan.json 结构（schema 2）

```json
{
  "schema": 2,
  "id": "807595",
  "goal": "把 auth 模块从 session 迁到 JWT",
  "why": "旧 session 表要下线",
  "created": "2026-10-25T11:29:10",
  "updated": "2026-10-25T11:29:34",
  "git_head": "cacdcb8",
  "max_rounds": 3,
  "baseline_context": 150000,
  "exclude": ["node_modules/", "dist/", "*.lock"],
  "acks": ["ack:向用户确认旧 session 表可以下线"],
  "steps": [
    {
      "n": 1,
      "title": "侦察：找出现有 session 的所有落点",
      "tier": "cheap",
      "budget": 6000,
      "out": "notes/01-recon.md",
      "read": ["src/auth/index.ts"],
      "reuse": "existing",
      "depends": [],
      "stop": ["grep 不到任何调用点就停下汇报"],
      "accept": ["file:exists:notes/01-recon.md"],
      "status": "done",
      "rounds": 1,
      "budget_rounds": 2,
      "note": ""
    },
    {
      "n": 2,
      "title": "写签发与校验函数",
      "tier": "std",
      "budget": 14000,
      "out": "src/auth/jwt.ts",
      "read": ["notes/01-recon.md", "src/auth/session.ts"],
      "reuse": "line",
      "depends": [1],
      "stop": ["session.ts 的实际形状和侦察结果对不上"],
      "accept": ["file:exists:src/auth/jwt.ts", "cmd:npx tsc --noEmit"],
      "status": "todo",
      "rounds": 0,
      "budget_rounds": 4,
      "note": ""
    }
  ]
}
```

| 字段 | 含义 | 谁写 |
|---|---|---|
| `schema` | 目前是 2。v1 计划文件读进来会自动补默认值 | CLI |
| `id` | 目标文本的 sha1 前 6 位。目标是同一个，id 就稳定 | `init` |
| `goal` | 一句话目标。**不超过 46 字**，超过会在 status 里被截断 | `init` |
| `why` | 这个任务为什么该存在。空的会在 `drift` 里被点出来 | `init --why` |
| `git_head` | 建计划时的 `git rev-parse --short HEAD`。用于漂移检查 | `init` |
| `max_rounds` | 单步重试上限。`start` 到顶会强制 block，防死循环烧钱 | `init --max-rounds` |
| `baseline_context` | 本会话一次请求的典型上下文 token 数。有它 `report` 才能把轮数折算成钱；0 = 未设 | `init --baseline-context` / `amend 0` |
| `exclude` | 禁读清单 | `init --exclude` / `exclude` |
| `acks` | 已确认的 `ack:` 断言 | `ack` |
| `steps[].n` | 序号，从 1 开始，自动分配 | `add` |
| `steps[].title` | 一步一个动作，动词开头 | `add` |
| `steps[].tier` | `cheap` / `std` / `deep` | `add --tier` |
| `steps[].budget` | 这一步的 token 预算（**易失真**：一次真实重构实测差 74 倍） | `add --budget` |
| `steps[].budget_rounds` | 这一步允许花几轮请求。0 = 未设。**推荐用它定预算** | `add --rounds` |
| `steps[].out` | **必填**。产物路径。同时用于并行冲突检测 | `add --out` |
| `steps[].read` | 计划内要读的文件。空的含义是「先 grep 再决定」 | `add --read` |
| `steps[].reuse` | 复用梯子档位 `skip`→`new`，见 SKILL.md 闸 0 | `add --reuse` |
| `steps[].depends` | 依赖的步骤号数组。决定 `ready` / `parallel` / `graph` | `add --depends` |
| `steps[].stop` | 停止条件。命中就该 `block` 而不是即兴发挥 | `add --stop` |
| `steps[].accept` | 验收断言数组 | `add --accept` |
| `steps[].status` | `todo` / `doing` / `done` / `blocked` | CLI |
| `steps[].rounds` | 已开始的重试轮次（`start` 累加）。因状态回滚而保留，不随 `reset` 清零 | CLI |
| `steps[].note` | 备注；`block` 时存卡点原因 | `block` / `amend --note` |

`rounds` 和 `budget_rounds` 是两件事：前者是**已经花掉**的轮次，后者是**允许花**的轮次。
`report` 拿两者相比，超了才提示。

### 改计划：`amend` 而不是手改 JSON

`amend` 只动 `plan.json`，不碰产物；每次改动写一行 ledger（新旧值都在里面）。

```bash
tg amend 3 --add-accept "cmd:npx tsc --noEmit"   # 补一条漏掉的验收
tg amend 3 --rm-accept "ack:代码质量良好"          # 删掉不可判定的断言
tg amend 3 --rounds 6 --reason "测试要跑三遍"      # 改轮数预算
tg amend 3 --depends 1,2                          # 整体替换依赖；空串 = 清空
tg amend 0 --baseline-context 150000              # 0 = 计划级设置
tg amend 0 --add-exclude "data/*.json" --max-rounds 2
```

护栏：改 `done` 步骤默认被拒（`exit 2`）。真需要改就 `--force`，ledger 里那行会带
`"forced": true`，改完必须跑 `tg reconcile`——否则这个计划里「曾经通过」的记录就不再可信。


### 为什么 `out` 兼管并行安全

`tg parallel` 不需要你额外声明「哪些步骤会改同一个文件」——它直接用 `out` 判断：
路径相等、或一方是另一方的父路径，就算冲突，这两步不能进同一批次。

所以**把 `out` 写准就是安全声明**。写 `src/auth/` 这种目录会让它跟所有同目录步骤冲突，
这是有意的保守设计：宁可串行，也不要两个子 agent 同时改一个文件。

---

## ledger.jsonl 结构

一行一个 JSON 对象，只追加：

```jsonl
{"ts": "2026-10-25T11:29:18", "step": 1, "in": 4200, "out": 900, "reqs": 1, "note": ""}
{"ts": "2026-10-25T11:29:21", "step": 1, "in": 0, "out": 0, "note": "DONE"}
{"ts": "2026-10-25T11:31:02", "step": 2, "in": 8400, "out": 1200, "reqs": 2, "note": "第一版 tsc 报错"}
{"ts": "2026-10-25T11:33:40", "step": 2, "in": 0, "out": 0, "amend": {"accept": [["file:exists:src/auth/jwt.ts"], ["file:exists:src/auth/jwt.ts", "cmd:npx tsc --noEmit"]]}, "forced": false, "note": "忘了类型检查"}
```

三类行，同一张表：

| 行型 | 必有的键 | 含义 |
|---|---|---|
| 记账 | `in` / `out` | 一笔 token 消耗。`reqs` 可选但强烈建议（几轮请求） |
| 完成 | `note: "DONE"` | 这一步收尾了。`(forced)` 后缀表示绕过了门禁 |
| 改动 | `amend` / `forced` | 计划被改：字段 → [旧值, 新值]。`forced: true` = 动了 done 步骤 |

`amend` 行不记 token（`in`/`out` 都是 0），它只是留痕：谁在什么时候把哪条断言从什么改成了什么，
以及为什么（`note`）。这样「预算超了之后偷偷改小预算」和「断言写错了补一条」在日志里长得不一样。

为什么追加写而不是改一个总数：**崩溃安全**。进程被杀在写一半时，最多丢最后一行，前面全部有效。
这也让 `ledger.jsonl` 可以被 `git diff` 看清（每行独立）。

---

## 好计划的形状

### 1. 步骤数 3~8

少于 3 步 → 不需要用这个技能，直接做。
多于 8 步 → 前半段一定会被上下文压缩掉。拆成两个 plan（第二个 plan 的 `--read` 指向第一个的产物）。

### 2. 一步一个可独立验证的产物

「一步的产物能独立验证」是唯一的粒度标准。

```
错：  #2  实现 JWT（产物：src/auth/）        ← 目录，无法验证
对：  #2  写签发与校验函数（产物：src/auth/jwt.ts）
      #3  接到中间件（产物：src/middleware/auth.ts）
      #4  删掉旧 session 逻辑（产物：无 → file:absent 断言）
```

### 3. 标题用动词开头

```
错：  "JWT 迁移"
对：  "写 JWT 签发与校验函数"
```

动词开头的标题天然就是可验证的，因为动词暗示了一个产物。

### 4. `read` 只写「确定要读」的

`read` 不是白名单，是**预告**。列了 5 个文件说明这一步本来就不该一次做完——拆。

```json
"read": ["src/auth/index.ts", "src/auth/session.ts"]
```

留空是有意义的写法：意思是「我不知道要读什么，所以先 grep」。

### 5. 第一个步骤几乎总是侦察

```bash
tg add --title "侦察：X 在哪里被使用" --tier cheap --budget 6000 \
     --out notes/01-recon.md --accept "file:exists:notes/01-recon.md"
```

侦察步骤的产物是一份 notes，后面的步骤用它当输入。
**不要跳过这一步。** 跳过它的代价是后面的步骤全部超预算。

---

## 完整例子

```bash
PL init "把 auth 从 session 迁到 JWT" --exclude "node_modules/" "dist/" "*.lock"

PL add --title "侦察：session 的所有落点" --tier cheap --budget 6000 \
   --out notes/01-recon.md \
   --accept "file:exists:notes/01-recon.md"

PL add --title "写签发与校验函数" --tier std --budget 14000 \
   --read "src/auth/session.ts" \
   --out src/auth/jwt.ts \
   --accept "file:exists:src/auth/jwt.ts" \
   --accept "cmd:npx tsc --noEmit"

PL add --title "设计迁移方案（读 notes 后决策）" --tier deep --budget 10000 \
   --read "notes/01-recon.md" \
   --out notes/03-plan.md \
   --accept "file:exists:notes/03-plan.md"

PL add --title "接中间件并删旧逻辑" --tier std --budget 16000 \
   --out src/middleware/auth.ts \
   --accept "nogrep:getSessionServerSide@src/middleware/auth.ts" \
   --accept "file:absent:src/auth/session.ts" \
   --accept "cmd:npm test"

PL status
```

对应状态摘要：

```
[PLAN 807595] 目标: 把 auth 从 session 迁到 JWT
进度: 0/4 · 预算 0/46k (0%) · 卡点 0
当前: #1 侦察：session 的所有落点 (tier=cheap, 预算 6k)
产物: notes/01-recon.md
禁读: node_modules/、dist/、*.lock
提示: tg show 看全文 · tg gate 验收 · 完成必须 gate 通过
```

---

## 计划的寿命

| 情况 | 处理 |
|---|---|
| 全部 done | `tg drift` 做收尾检查，然后留着当记录（或归档到 `notes/`） |
| 目标变了 | `tg amend 0 --goal "新目标"`；整个推倒重来才用 `init "新目标" --force`（会清空 ledger） |
| 只想改一个步骤 | `tg amend N ...`（留痕）。手改 `plan.json` 也行，但改动不进 ledger |
| 中途发现步骤拆错了 | `tg reset N` 退回 todo，`tg amend N` 改契约，`tg add` 补新步骤 |
| 计划废弃 | 删 `.plan/` 整个目录 |

**同一时间只有一个活跃计划。** 并行任务请在各自的项目根目录下各自 `init`，
`tg` 会从 cwd 向上找到最近的 `.plan/`，所以多个项目互不干扰。
