# 编排层：多角色并发怎么不炸

`orch.py` 是 `tg.py` 旁边的第二层。它解决的问题不是「怎么让模型跑得更快」，而是
**「让多个 agent 同时干活时，账、边界、验收都不失真」**。

## 它是什么，不是什么

| 是 | 不是 |
|---|---|
| 把「谁负责哪个目录、谁来评」落盘成契约 | 进程调度器（不起线程、不管生命周期） |
| 派发前把四道刹车踩一遍 | agent runtime（不拼 prompt 给模型执行） |
| 把 `.plan/review-*.json` 和账本汇总成三层报告 | 模型调用方（一个 token 都不发） |
| 只经 `subprocess` 调 `tg` 的 `--json` 契约 | `import tg` 内部函数（内核换个实现它也不改） |

最后一条是硬约束：**orch 从不 import 内核**。它只认 `tg status --json` 的 `api` 版本号，
对不上就停，不去猜计划长什么样。这样内核可以单独升级，也可以被换成别的实现。

## 三层门控：生死只由第一层决定

| 层 | 谁说了算 | 输出什么 | 能不能否决 `done` |
|---|---|---|---|
| 1 断言 | `exit code` | 过 / 破 / **超时（没验完）** | 能，唯一能的那层 |
| 2 语义 | reviewer（jev / startlux / local-4b / none） | 概率 + 证据 | **不能**，只出建议 |
| 3 人 | 你 | 只在 1、2 打架时介入 | 能，且是最后一道 |

三条铁律：

1. **报告里不许出现「AI 评价通过」这种合体句。** 两层必须分列打印。合起来写，
   迟早有人拿语义 verdict 当验收依据。
2. **jev 的 `pass` 在人工抽查验证之前不得当作 `done` 依据。** 双轨期要求：每条
   jev 评价旁边记一句 `human_verified`，`orch rollup` 会把「评了几次 / 抽查了几次」
   摆出来，没抽查满就一直警告。
3. **没查就说没查。** `rollup` 不给 `--gate` 时不做打架检测（因为 `status=done`
   只说明「当时过了」，代码可能已经变了），并且明说「没查不等于没打架」——
   绝不输出一个看着像结论的「0 个打架」。

## `review-<step>.json` 格式

放在 `.plan/review-<步骤号>.json`。写它的是**主会话的 agent**（它调 jev 或别的 reviewer），
`orch` 只读不写、只汇总不裁决。

```json
{
  "schema": 1,
  "step": 2,
  "reviewer": "jev",
  "model": "jev-判定",
  "verdict": "pass",
  "confidence": 0.82,
  "probs": { "pass": 0.82, "fail": 0.18 },
  "checks": [
    { "name": "路由与契约一致", "ok": true, "note": "5 条路由全对" },
    { "name": "错误体格式", "ok": false, "note": "缺 code 字段" }
  ],
  "evidence": [
    { "kind": "file", "ref": "api/app.py", "note": "只看了这个文件" },
    { "kind": "diff", "ref": "git diff --stat", "note": "改动 3 个文件" }
  ],
  "reviewed_at": "2026-10-25T21:00:00",
  "reviewer_session": "ses_e8ce09f98ffeH9jKpjw8mxL1bE",
  "cost_usd": 0.0012,
  "human_verified": false,
  "notes": "没跑起来，只做了静态比对 → 概率给低一点"
}
```

字段口径：

| 字段 | 必填 | 口径 |
|---|---|---|
| `schema` | 是 | 现在是 `1`。加字段不升，改含义要升 |
| `step` | 是 | 步骤号，必须和文件名里的号一致 |
| `reviewer` | 是 | `jev` / `startlux` / `local-4b` / `none`，别的一律算坏文件 |
| `verdict` | 是 | `pass` / `fail` / `unsure` / `skip`。**这是概率结论，不是验收结论** |
| `probs` | 否 | 建议给。`{"pass": x, "fail": y}`；只给 verdict 不给概率时，人没法判断该多信 |
| `confidence` | 否 | 0–1。和 `probs.pass` 不是一回事：一个说「我多确定」，一个说「我觉得多可能」 |
| `checks` | 否 | 逐项检查，比一个大 verdict 有用得多 |
| `evidence` | 否 | 它到底看了什么。**只写了 diff 没跑起来** 这件事必须写在这儿 |
| `human_verified` | 否 | 人抽查过才写 `true`。缺省视为 `false` |
| `reviewer_session` | 否 | 评价这件事本身花了哪个会话的钱，能追到账 |
| `cost_usd` | 否 | 评价本身的成本。一次评价也是有代价的，别算进「免费的检查」 |

**坏文件必须报出来。** 缺字段、`reviewer` 不在允许列表、JSON 本身坏了 —— `orch rollup`
会逐条点名。把它当「没评过」静默跳过是最坏的处理：看起来一样，事后没人查得出来。

## 命令

```bash
# 角色 = 一个它独占的目录。目录一重叠就不是并发，是抢文件。
orch role add backend  --dir api/ --reviewer jev
orch role add frontend --dir web/ --reviewer jev
orch role list

# 派发前踩四道刹车，过了就一人一份自足式简报（不派发、不调模型）
orch fan 2                          # 默认全部已登记角色
orch fan 2 --roles backend,frontend --json
orch fan 2 --contract contract/CONTRACT.md

# 收工后三层汇总
orch rollup            # 只读计划状态，不做打架检测（并明说没查）
orch rollup --gate     # 真重跑断言，才谈得上「打架」
orch rollup --gate --cost --json
```

`fan` 生成的简报在 `.orch/briefs/<步骤>-<角色>.md`，每份都自带：只能碰的目录、
别人的禁区、唯一事实来源（契约文件）、验收断言原文、归账命令，以及
**「不要自己调 `tg done`」**。

## 派发前的四道刹车

| 刹车 | 默认 | 踩下去的样子 |
|---|---|---|
| 并发路数 | 2 | `要派 3 路，上限是 2 路` —— 并追问「这 3 路各自产出的东西谁看、谁合、合不上怎么办」 |
| 目录重叠 | 必查 | `角色 a 的 api 与 b 的 api/sub 重叠 → 这不是并发，是抢文件` |
| 依赖链深度 | 3 | `依赖链深度 5 > 上限 3` —— 链越长，前面一步判断错，后面全白跑 |
| 契约文件 | 必查 | 没契约只是警告（并说明「各自脑补 = 集成时返工」），显式指定了却不存在是硬拒 |

路数上限不是性能旋钮，是刹车。依据写在下面「凭什么这么设」里。

## 并发怎么真落地（诚实版）

`tg` 目前只允许**一个**步骤同时 `doing`（`tg start` 会拒绝第二个）。所以两路并发时：

1. 第一路正常 `tg start <n>`，拿到 `started_ms` 归账边界；
2. **其余各路不走 `start`**，收工后直接 `tg sync --step <n> --session <该 subagent 的会话 id>`。
   每个 subagent 是独立会话，游标也按会话分开存，所以归属仍然是干净的；
3. 绕过 `start` 的那一路没有 `started_ms`，归账边界退化成计划 `created`。
   对「专门为这一步开的 subagent 会话」没有实际副作用（那个会话里没有别的事）。

**这是绕过，不是支持。** 把 `doing` 放宽成受 `max_fanout` 约束的多值是内核待改项，
`orch fan` 每次都会把这段话原样打在 stderr 上，不让人以为它是官方姿势。

## 凭什么把路数卡在 2

不是拍的，是两个已公开的实测 + 我们自己的账本：

- **Anthropic 的多 agent 研究系统**：+90.2% 的质量，但花 **15×** token；而且同一篇
  明确说编码任务的可并行度低。
- **MAST**（1642 条多 agent 轨迹的失败模式调查）：头号失败模式是 **cost runaway**。
- **本仓 A/B 实测**（同一个待办 Web 应用，一份写死的契约）：2 路并发 18 轮 / 28.8k tok
  / $0.010047 / 墙钟 111.2s，1 路串行 46 轮 / 72.9k tok / $0.030831 / 518.8s。
  `cache_read` 200.2k vs 1649.7k（差 8.2 倍），质量打平（集成探针均 32/32）。

结论不是「并发更快」，而是：**并发的收益来自「强制先把契约写死 + 角色边界 = 目录边界」**，
钱是从「不重复读」和「少返回几轮」省下来的。所以 `orch` 卡的是路数，逼的是契约。

## 不变量（改代码时别破）

1. `orch` 不调模型、不起进程、不改 `plan.json`。
2. `orch` 的 `stdout` 是机器契约：**人类提示一律走 `stderr`**，警告同时进 payload 的
   `warnings`。破了这条，下游每读一个命令都得猜哪几行是 JSON。
3. `review-*.json` 永远不能改变步骤状态。`orch rollup` 跑完之后 `plan.json` 必须字节不变
   （测试里直接比对前后 status 表）。
4. `tg --json` 的 `api` 版本对不上就停，不降级、不猜。

## 局限

1. 只读 `.plan/plan.json` 的存在性，状态一律问 `tg status --json`；内核崩了 orch 就没事干。
2. 简报是模板生成的，不懂项目语义——它保证「边界和契约写清了」，不保证「任务描述得好」。
3. 依赖深度按 `depends` 图算，不看产物依赖；产物重叠由 `tg parallel` 另外判。
4. `rollup` 的打架检测只在 `--gate` 下做（其它情况一律报「没查」），所以默认模式**不能**
   拿来当验收证据。
5. 不做重试、不做超时、不做并发限流——它只做准备和汇总，执行由主会话掌控。
