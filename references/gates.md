# 验收断言：把「做完了」变成可执行的判据

## 为什么需要这个

自动 agent 最典型的失败不是「不会做」，是**做了一半就说做完了**。
模型判断「完成」的方式是读自己刚写的代码，然后觉得没问题——这等于自己给自己批卷。

断言的价值在于把判据**从模型里搬到进程外**。`exit code` 不会自我感动。

---

## 六种断言

### 1. `file:exists:<路径>`

产物应该在。

```bash
--accept "file:exists:src/auth/jwt.ts"
--accept "file:exists:tests/auth/jwt.spec.ts"
```

**注意**：它只证明文件存在，不证明文件是对的。**永远配一条命令类断言。**

---

### 2. `file:absent:<路径>`

旧文件应该被删除。重构类任务必备——否则 agent 常常「新增了新的，忘了删旧的」，然后两套逻辑并存。

```bash
--accept "file:absent:src/auth/session.ts"
```

---

### 3. `grep:<正则>@<文件>`

文件里出现了预期内容。

```bash
--accept "grep:export function verifyJwt@src/auth/jwt.ts"
--accept "grep:from './jwt'@src/auth/index.ts"
```

**坑**：解析用的是 `rsplit("@", 1)`，即**按最后一个 `@` 分隔**。所以 pattern 里含 `@` 没问题，但**文件路径里不能有 `@`**。

---

### 4. `nogrep:<正则>@<文件>`

旧写法应该消失。这是**最被低估的断言**。

```bash
--accept "nogrep:getSessionServerSide@src/app/**/*.ts"
--accept "nogrep:TODO@src/auth/jwt.ts"
```

迁移类任务里，`nogrep` 比 `file:absent` 更有用——文件还在，但里面的旧调用必须清干净。

---

### 5. `cmd:<命令>`

命令成功即通过。**这是唯一真正有分量的断言。**

```bash
--accept "cmd:npx tsc --noEmit"
--accept "cmd:pytest -q tests/test_auth.py"
--accept "cmd:npm run build"
--accept "cmd:ruff check src/auth"
--accept "cmd:git diff --quiet --exit-code"
```

规则：
- 优先选**快**的命令（单文件测试 > 全量测试）。输出里会带每条命令的**耗时**，
  谁在吃时间一眼看见（真输出长这样：`PASS  cmd:python slow.py` → `exit=0  5.1s`）
- 优先选**有明确退出码**的工具（类型检查、lint、单测）
- 默认 600 秒/条上限，`--timeout N` 可改；**超时算「没验完」**，标 `TIMEOUT`，
  既不算通过也不算破了（见下节「gate 的四种结果」）
- 命令在项目根目录执行
- **不要在命令里嵌引号**。`--accept "cmd:python -c \"...\""` 在 PowerShell 下会被吃掉一层，
  写 `cmd:pytest -q tests/test_x.py` 这种无引号形式；真要带引号，把逻辑写成脚本再 `cmd:python scripts/check.py`

**别把「全仓检查」写进断言。** 断言回答的是「**这一步**做完了吗」，不是「这个仓库还干净吗」。
`cmd:pytest`、`cmd:npm test`、跑整套文档核对的脚本都属于后者：它们会在每次 `tg done`
和每次 `tg reconcile` 里被重跑一遍。狗粮实测——一次两分半起步，`reconcile` 六个步骤十分钟。
全仓检查放提交前手动跑，或者单独做一个收尾步骤（那时它才算「这一步的产物」）。
真需要留一条慢断言，就给 `--timeout` 放宽，别删（删了就是不验）。

---

### 6. `ack:<说明>`

无法自动判定的项。**必须显式确认才算通过**：

```bash
--accept "ack:向用户确认旧 session 表可以下线"
```

```bash
tg ack "向用户确认旧 session 表可以下线"
```

用它兜住「必须有人拍板」的环节。**别滥用**——每条 `ack` 都是门禁上的一个洞。

---

## 按任务类型选的断言配方

| 任务类型 | 推荐组合 |
|---|---|
| 新增功能 | `file:exists` + `cmd:<类型检查>` + `cmd:<相关单测>` |
| 重构 / 迁移 | `nogrep:<旧调用>` + `cmd:<相关单测>` + `cmd:<构建>`（**全量单测放收尾步骤**） |
| 修 bug | `cmd:<复现该 bug 的测试>`（**先写失败测试，再修**） |
| 删代码 | `file:absent` + `cmd:<构建>` + `nogrep:<被删符号>` |
| 写文档 | `file:exists` + `grep:<必须出现的关键词>` |
| 纯分析 | `file:exists:notes/NN-x.md` + `grep:<结论关键词>@同文件` |
| 性能优化 | `cmd:<benchmark 脚本>`（让脚本自己判定阈值，别靠肉眼看） |

---

## 反例：这些断言等于没写

| 写法 | 为什么无效 |
|---|---|
| `ack:代码质量良好` | 无法判定，且没人会说不 |
| `grep:function@src/x.ts` | 任何文件都有 `function`，恒真 |
| `cmd:echo ok` | 永远 exit 0 |
| `cmd:true` | 同上 |
| `file:exists:.` | 目录总在 |
| `file:exists:dist/` | 构建产物目录可能早就在，不证明这次构建成功 |

**自检方法**：写下断言后问自己——「如果这一步我什么都没做，这条断言会不会通过？」
会通过 → 这条断言是废的。

---

## 断言写不出来的情况怎么办

如果一步的成果完全无法断言，说明**这一步定义得不对**，通常是两个原因：

1. **粒度太粗**（"优化代码"）→ 拆成"降低 X 函数的复杂度到 Y 以下"+"基准脚本跑出来 < Z ms"
2. **目标本身模糊**（"让它更好"）→ 回头找用户要一个可判定的判据

宁可多花 2 分钟把断言写具体，也别让门禁变成橡皮章。

---

## gate 的四种结果

```bash
tg gate 3              # 每条 cmd: 断言默认 600 秒上限
tg gate 3 --timeout 30 # 收紧/放宽单条断言的时限（0 = 不限时）
```

- **全 PASS**：可以 `tg done 3`
- **有 FAIL**：`done` 会拒绝，输出失败项
- **有 TIMEOUT**：这条**没验完**——`done` 同样拒绝（没验完就别算做完），但 `reconcile`
  **不判它回归、也不以非 0 退出**，只报 `?? 有 N 条没验完（超时）：不知道还算不算通过`。
  超时当 FAIL 会冤枉一步好代码，当 PASS 就是自欺，所以它单独算
- **无断言**：直接 PASS，但会提示「这步没有验证价值」——这是警告，不是错误

强制通过（只在断言本身写错时用）：

```bash
tg done 3 --force    # 会在 ledger.jsonl 里留一条 "(forced)" 记录
```
