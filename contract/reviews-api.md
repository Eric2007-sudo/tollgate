# `/api/reviews` 契约（第 10 步产出，两路并发都以这份为准）

这份文件是第 11 步两路的**唯一事实来源**。字段名、`null` 的含义、看板上那句话的措辞，
都写死在这里。不够用或者跟代码对不上，**停下来汇报**，不许各自将就。

## 1. 接口

```
GET http://127.0.0.1:<port>/api/reviews
```

只读。绑定 `127.0.0.1`，校验 `Host` 头（和 `/api/state` 同一套，403 就是非本地访问）。
数据源：`.plan/review-<步>.json`，**只读不写**，不校验、不裁决、不改计划状态。

## 2. 响应字段（改名字要升 `api`，加字段不用）

```json
{
  "api": 1,
  "cmd": "reviews",
  "ok": true,
  "reviews": [
    {
      "file": "review-9.json",
      "step": 9,
      "reviewer": "jev",
      "verdict": "unsure",
      "pass_prob": 0.81,
      "reviewed_at": "2026-10-26T12:00:00",
      "human_verified": true,
      "human_claims": { "confirmed": 5, "total": 5 }
    }
  ],
  "bad_files": [ { "file": "review-5.json", "error": "解析不了：..." } ],
  "summary": {
    "steps": 3,
    "bad": 0,
    "claims_confirmed": 13,
    "claims_total": 13,
    "all_confirmed": true,
    "without_claims": 0
  },
  "note": "人确认进度只读；verdict 只是概率建议，done 只认断言 exit code。"
}
```

逐字段口径：

| 字段 | 类型 | 口径 |
|---|---|---|
| `reviews[].step` | int | 取自文件内容，不是文件名（两者不一致时以内容为准，且不替人改） |
| `reviews[].verdict` | str | `pass` / `fail` / `unsure` / `skip`。**是概率结论，不是验收结论** |
| `reviews[].pass_prob` | number \| null | `probs.pass`；没有就给 `null`，**不许拿 `confidence` 顶替** |
| `reviews[].human_verified` | bool | 只有原文件写 `true` 才是 `true`，缺省 `false` |
| `reviews[].human_claims.confirmed` | int | 人确认了几条 claim |
| `reviews[].human_claims.total` | int \| null | **`null` = 这份 review 根本没有逐条数据**，跟「0 条确认」不是一回事，前端不许显示成 `0/0` |
| `bad_files[]` | array | 解析不了 / 缺 `step`/`reviewer`/`verdict` 的文件。**必须显示出来，不许静默跳过** |
| `summary.claims_total` | int \| null | 全部为 `null` 时就给 `null`，**不许报 0** |
| `summary.all_confirmed` | bool | `claims_total` 为 `null` 或 0 时必须是 `false`（「没有数据」不等于「全绿」） |
| `note` | str | 原样显示在面板上，别删别改 |

「jev 自己的汇总」那种行不是 claim，不算进 `total`。

## 3. 前端要显示的措辞（不许自创同义词）

| 情况 | 必须显示 |
|---|---|
| `total` 为 `null` | `没有逐条数据`（不是 `0/N`、不是 `未确认`） |
| `confirmed == total` 且 `total > 0` | `全部经人确认（N/N 条 claim）` |
| `0 < confirmed < total` | `人确认 N/M 条 claim`，并另起一句 `部分确认不算验证通过` |
| `confirmed == 0` 且有 `total` | `一条都没人确认（0/M）` |
| `bad_files` 非空 | 红字逐条列出文件名与原因 |

面板上不许出现「AI 评价通过」「验证通过」这类把两层合成一句话的写法。

## 4. 目录边界（第 11 步两路）

| 角色 | 只许动 | 产物 |
|---|---|---|
| `frontend` | `ui/` | 面板渲染（`app.js` / `style.css` / 必要时 `index.html`） |
| `tester` | `tests/` | `tests/smoke_serve.py` 里针对 `/api/reviews` 的真请求断言 |

`tg.py`、本文件、`.plan/`、`.orch/` 谁都不许动。前端拿不到字段就**停下来汇报**，不许自己在
`ui/` 里造一个替代数据源。

## 5. 「跑通」的定义（只认 exit code）

- 第 11 步的断言全绿：`tg gate 11` → exit 0
- `frontend`：`/api/reviews` 的 6 种情况在面板上按上面那张表显示（人眼看，截图或说明都行）
- `tester`：`python tests/smoke_serve.py` → exit 0，且新增用例覆盖「有逐条数据 / 无逐条数据 /
  坏文件」三种形状
- 两路都**不许**自己调 `tg done`，收工汇报，由大脑（`kernel`）合
