#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orch —— 多角色编排层。零依赖，只经 subprocess 调 tg 的 CLI，只吃它的 --json 契约。

它**不是**进程调度器，**不是** agent runtime，**不调模型**：

    role add / role list   把「谁负责哪个目录、谁来评」落盘成一份可以吵的契约
    fan <step>             派发前先查一遍：并发路数、目录重叠、依赖深度、契约在不在；
                           全过了才给每个角色生成一份自足式简报，并打印该跑的 tg 命令
    rollup                 收工后把「断言层 / 语义层 / 人」三层分开汇总

三条不让步的规矩：
  1. 门控生死**只认 exit code**。review-*.json 里的 verdict 是概率，不是结论，
     任何时候都不许拿它替代 `tg done`。
  2. 并发的安全边界是**目录边界**。两个角色的目录一重叠，就等于没切。
  3. 并发路数有硬上限（默认 2）。多智能体烧的是 token 不是时间——
     Anthropic 实测 15×、MAST 调查里头号失败模式就是 cost runaway。

跑法：python orch.py <命令> ...
      tg.py 默认找同目录、ORCH_TG、或 ~/.config/opencode/skills/tollgate/tg.py
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# 只认这个版本的 tg --json 契约。tg 改了字段含义就升它，orch 跟着改。
TG_JSON_API = 1
# orch 自己输出的 `api` 版本号：加字段不升版，改字段名或含义必须 +1。
ORCH_JSON_API = 1
# orch 自己的落盘格式
ORCH_SCHEMA = 1
# 并发路数上限。不是「性能旋钮」，是刹车。
DEFAULT_MAX_FANOUT = 2
# 依赖链深度上限。链越长，前面一步判断错了，后面全白跑。
DEFAULT_MAX_DEPTH = 3
REVIEWERS = ("jev", "startlux", "local-4b", "none")


# ---------------------------------------------------------------- 基础设施

def out(msg=""):
    print(msg)


# 警告和错误走 **stderr**：orch 的 stdout 是机器契约，
# 一旦掺进人类提示，下游每读一个命令都得猜哪几行是 JSON ——
# 这正是 tg 那边 jout() 立过的规矩，orch 不能自己先破。
WARNINGS = []


def die(msg, code=2):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(code)


def warn(msg):
    WARNINGS.append(msg)
    print("!! " + msg, file=sys.stderr)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def find_root():
    """从 cwd 往上找 .plan/plan.json。orch 一切以「计划」为锚，不是以 git 为锚。"""
    here = Path.cwd().resolve()
    for cand in [here, *here.parents]:
        if (cand / ".plan" / "plan.json").is_file():
            return cand
    return here


def tg_path():
    """找 tg.py。允许换实现：只要它还有 --json 契约，orch 不关心里面怎么写。"""
    env = os.environ.get("ORCH_TG")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        die("ORCH_TG 指向的文件不存在：%s" % env)
    here = Path(__file__).resolve().parent
    for cand in (here / "tg.py",
                 Path.home() / ".config" / "opencode" / "skills" / "tollgate" / "tg.py"):
        if cand.is_file():
            return cand
    die("找不到 tg.py。它该和 orch.py 放在一起，或设 ORCH_TG 指过去。")


def tg_json(args, root):
    """调一条 tg 的 --json 命令，返回 (payload, exitcode)。

    解析不了就是 None —— **不猜、不从人类可读输出里抠数字**。
    抠出来的数看着像真的，错的时候没人查得出来。
    """
    argv = [sys.executable, str(tg_path()), *args, "--json"]
    try:
        r = subprocess.run(argv, cwd=str(root), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        die("调不动 tg：%s" % e)
    try:
        return json.loads(r.stdout), r.returncode
    except Exception:
        return None, r.returncode


def paths_overlap(a, b):
    """产物/目录是否重叠。语义必须和 tg 的判断一致，否则两边会吵起来。"""
    if not a or not b:
        return False
    a = str(a).replace("\\", "/").strip("/")
    b = str(b).replace("\\", "/").strip("/")
    if not a or not b:
        return False
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def chain_depth(steps, n):
    """这一步的依赖链有多深（#1 的深度为 1）。有环就返回 -1。"""
    by = {s["n"]: s for s in steps}
    memo = {}

    def walk(x, stack):
        if x in memo:
            return memo[x]
        if x in stack:
            return -1
        s = by.get(x)
        if s is None:
            return 0
        deps = [d for d in (s.get("depends") or []) if d in by]
        if not deps:
            memo[x] = 1
            return 1
        best = 0
        for d in deps:
            sub = walk(d, stack | {x})
            if sub < 0:
                return -1
            best = max(best, sub)
        memo[x] = best + 1
        return memo[x]

    return walk(n, set())


def read_json(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


def orch_dir(root):
    return root / ".orch"


def load_roles(root):
    p = orch_dir(root) / "roles.json"
    if not p.is_file():
        return {"schema": ORCH_SCHEMA, "max_fanout": DEFAULT_MAX_FANOUT,
                "max_depth": DEFAULT_MAX_DEPTH, "roles": {}}
    st = read_json(p)
    st.setdefault("schema", ORCH_SCHEMA)
    st.setdefault("max_fanout", DEFAULT_MAX_FANOUT)
    st.setdefault("max_depth", DEFAULT_MAX_DEPTH)
    st.setdefault("roles", {})
    return st


def save_roles(root, st):
    write_json(orch_dir(root) / "roles.json", st)


def load_state(root):
    p = orch_dir(root) / "state.json"
    if not p.is_file():
        return {"schema": ORCH_SCHEMA, "fans": []}
    return read_json(p)


def save_state(root, st):
    write_json(orch_dir(root) / "state.json", st)


def need_plan(root):
    if not (root / ".plan" / "plan.json").is_file():
        die("这里没有 .plan/plan.json。先 tg init \"目标\" --why \"为什么这件事该存在\"")
    s, rc = tg_json(["status"], root)
    if s is None:
        die("拿不到 tg status --json。tg 版本对不上，或者它崩了 —— "
            "orch 不会去猜计划长什么样。先手动跑一遍 tg status 看错在哪。")
    if s.get("api") != TG_JSON_API:
        die("tg --json 契约版本是 %r，orch 只认 %d。加字段可以，改字段就得升版，"
            "两边统一了再用。" % (s.get("api"), TG_JSON_API))
    return s


# ---------------------------------------------------------------- role

def cmd_role_add(a):
    root = find_root()
    need_plan(root)
    st = load_roles(root)
    name = a.name
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
        die("角色名只许小写字母/数字/._-（别用中文和空格，它要进文件名）：%r" % name)
    if name in st["roles"] and not a.force:
        die("角色 %r 已登记。要改就加 --force" % name)
    if a.reviewer not in REVIEWERS:
        die("--reviewer 只能是 %s 之一（jev 默认离线零外传；startlux 需要 key；"
            "local-4b 是离线兜底；none = 只跑断言）" % " / ".join(REVIEWERS))
    d = str(a.dir or "").replace("\\", "/").strip().strip("/")
    if not d:
        die("--dir 不许为空。角色边界必须是目录边界，没有目录就不算角色。")
    for other, info in st["roles"].items():
        if other == name:
            continue
        if paths_overlap(d, info.get("dir", "")):
            die("角色 %r 的目录 %s 和 %r 的 %s 重叠了。\n"
                "    并发的安全边界是目录边界：一重叠就是两个 agent 抢同一批文件，\n"
                "    冲突不是「可能出现」，是「什么时候出现」。先拆目录。"
                % (name, d, other, info.get("dir")))
    st["roles"][name] = {"dir": d, "reviewer": a.reviewer, "note": a.note or ""}
    save_roles(root, st)
    out("已登记角色 %s → %s/（reviewer=%s）" % (name, d, a.reviewer))
    if a.reviewer == "startlux":
        out("  注意：startlux 的 key 还没接上时它只是个空位，别当它能评。")


def cmd_role_list(a):
    root = find_root()
    st = load_roles(root)
    roles = st["roles"]
    if a.json:
        out(json.dumps({"api": ORCH_JSON_API, "cmd": "role-list", "ok": True,
                        "schema": st["schema"],
                        "max_fanout": st["max_fanout"],
                        "max_depth": st["max_depth"],
                        "roles": [{"name": k, "dir": v.get("dir"),
                                   "reviewer": v.get("reviewer"),
                                   "note": v.get("note", "")}
                                  for k, v in roles.items()]},
                       ensure_ascii=False, indent=1))
        return
    if not roles:
        out("还没有角色。加一个：orch role add backend --dir api/ --reviewer jev")
        out("（并发的安全边界是目录边界：一个角色 = 一个它独占的目录）")
        return
    out("角色（%d 个，并发上限 %d 路）:" % (len(roles), st["max_fanout"]))
    for k, v in roles.items():
        out("  %-12s → %-20s reviewer=%s%s"
            % (k, v.get("dir", "-") + "/", v.get("reviewer", "-"),
               ("   # " + v["note"]) if v.get("note") else ""))
    dirs = [v.get("dir", "") for v in roles.values()]
    for i in range(len(dirs)):
        for j in range(i + 1, len(dirs)):
            if paths_overlap(dirs[i], dirs[j]):
                warn("目录重叠：%s 与 %s —— 这两个角色不能并行" % (dirs[i], dirs[j]))


# ---------------------------------------------------------------- fan

BRIEF_TMPL = """# 简报 · #{n} {title} · 角色 `{role}`

你是这个计划里**唯一**的 `{role}`。这份文件是自足的：需要的都在这里，别去猜别人的实现。

## 你只能碰这一个目录

`{dir}/`

**不许读、不许改**这些地方（它们是别的角色/别组的产物，读了就等于用别人的中间状态污染判断）：
{forbidden}

## 唯一的共同事实来源

{contract}

字段名、路由、状态码、错误体都以它为准。它不够用 → **停下来汇报**，不要自己扩充、
不要自己发明约定、不要为了「跑起来」去猜。

## 这一步的边界

- 产物目录：`{out}`
- 请求轮数上限：{rounds}
- 停止条件：{stops}
- 你正在做的步骤：`#{n} {title}`

## 验收断言（会真跑；exit code 才是生死）

{accept}

通不过就是没做完。不要用「应该可以了」代替跑一遍。

## 收工怎么算

1. 跑 `{cmd_gate}`，必须全绿；
2. 把它记进账：`{cmd_sync}`；
3. 你**不要**自己调 `tg done`，交给上层。

## 报告格式（三到五行，不要写作文）

1. 创建/修改了哪些文件
2. 断言里哪几条是你亲手验过的
3. 有没有偏离契约（没有就写「无」）
"""


def cmd_fan(a):
    root = find_root()
    status = need_plan(root)
    st = load_roles(root)

    step = next((s for s in status["steps"] if s["n"] == a.step), None)
    if step is None:
        die("计划里没有 #%d。跑 tg status 看有哪些步骤" % a.step)
    if step["status"] == "done":
        die("#%d 已经是 done。要重做先 tg reset %d" % (a.step, a.step))

    if a.roles:
        roles = [r.strip() for r in a.roles.split(",") if r.strip()]
    else:
        roles = list(st["roles"])
    if not roles:
        die("没有角色。先 orch role add <名字> --dir <目录> --reviewer jev")
    unknown = [r for r in roles if r not in st["roles"]]
    if unknown:
        die("没登记过的角色：%s。先 orch role add" % "、".join(unknown))

    limit = a.max_fanout if a.max_fanout is not None else st["max_fanout"]
    max_depth = st["max_depth"]

    # ① 并发路数有硬上限：这不是性能旋钮，是刹车
    if len(roles) > limit:
        die("要派 %d 路，上限是 %d 路。\n"
            "    多智能体烧的是 token 账不是时间账：Anthropic 实测多 agent 研究系统\n"
            "    15× token 换 +90.2%%，还明说编码任务并行度低；MAST 查 1642 条轨迹，\n"
            "    头号失败模式就是 cost runaway。\n"
            "    真要加：orch fan %d --max-fanout %d —— 但先回答「这 %d 路各自产出\n"
            "    的东西，最后由谁看、谁合、合不上怎么办」。"
            % (len(roles), limit, a.step, len(roles), len(roles)))

    # ② 目录重叠再查一遍：roles.json 是可以被手改的，契约得每次现查
    for i in range(len(roles)):
        for j in range(i + 1, len(roles)):
            di, dj = st["roles"][roles[i]]["dir"], st["roles"][roles[j]]["dir"]
            if paths_overlap(di, dj):
                die("角色 %s 的 %s 与 %s 的 %s 重叠 → 这不是并发，是抢文件。"
                    % (roles[i], di, roles[j], dj))

    # ③ 依赖链深度：链越长，前面一步判断错，后面全白跑
    depth = chain_depth(status["steps"], a.step)
    if depth < 0:
        die("#%d 的依赖链成环了。先 tg graph 看清楚" % a.step)
    if depth > max_depth:
        die("#%d 的依赖链深度 %d > 上限 %d。\n"
            "    链越长，前面一步判断错了，后面全白跑——而且不会有人中途告诉你。\n"
            "    先拆短（把中间那一步的产出写成契约，让后一步只依赖契约）。\n"
            "    确实必须这么深：改 .orch/roles.json 的 \"max_depth\"，并在计划里"
            "写明为什么不是链太长的借口。" % (a.step, depth, max_depth))

    # ④ 契约在不在。没有契约的并发 = 一定返工，这是 A/B 实测里最贵的一课
    contract = a.contract
    if not contract:
        cands = [s for s in status["steps"]
                 if s["status"] == "done" and str(s.get("out", "")).endswith(".md")]
        contract = cands[0]["out"] if cands else None
    if not contract:
        warn("没找到契约文件（--contract 指定，或先让前面某步产出一份 .md）。"
             "没有契约就并发 = 各自脑补 = 集成时返工，别省这一步。")
    elif not (root / contract).is_file():
        die("契约文件不存在：%s" % contract)

    # ⑤ 现状提醒：tg 的 doing 目前是单值
    others_doing = [n for n in status.get("doing", []) if n != a.step]
    if others_doing or len(roles) > 1:
        warn("tg 的 `doing` 现在是单值（tg start 会拒绝第二个）。所以两路并发时：\n"
             "    第一路正常 tg start <n> 拿归账边界；其余各路直接\n"
             "    tg sync --step <n> --session <sid>（每个 subagent 一个独立会话，\n"
             "    游标按会话分开，归属仍然是干净的）。这是绕过，不是支持 ——\n"
             "    doing 放宽成受 max_fanout 约束的多值已经排进内核改动。")

    # ⑥ 生成简报
    bdir = orch_dir(root) / "briefs"
    bdir.mkdir(parents=True, exist_ok=True)
    accept = "\n".join("- `%s`" % x for x in (step.get("accept") or [])) \
        or "- （这步没有断言 → 它没有验证价值，先补 --accept）"
    stops = "；".join(step.get("stop") or []) or "（未设置 → 建议补：现实与计划不符时停下来汇报）"

    briefs = {}
    for role in roles:
        # 禁区要列**所有**别的角色的目录 —— 包括同批并发的那个。
        # 这里最容易写错成「同批的不算冲突」，那正好把整个设计的支点拆了：
        # 目录边界之所以是安全边界，是因为不管同时跑不跑，谁都不许越界。
        forbid = []
        for other, info in st["roles"].items():
            if other == role:
                continue
            forbid.append("- `%s/`（角色 %s%s）"
                          % (info["dir"], other,
                             "，正在同批并发" if other in roles else ""))
        forbid.append("- `.plan/`、`.orch/`（账本与编排状态，只读不写）")
        forbid.append("- 契约文件本身（不够用就停下来汇报）")
        body = BRIEF_TMPL.format(
            n=step["n"], title=step["title"], role=role,
            dir=st["roles"][role]["dir"],
            forbidden="\n".join(forbid),
            contract=("`%s`（先完整读一遍）" % contract) if contract
            else "**没有契约文件** —— 先停下来要一份，别自己发明字段名。",
            out=step.get("out") or "-",
            rounds=(("%d 轮" % step["budget_rounds"]) if step.get("budget_rounds")
                    else "未设（建议补 --rounds）"),
            stops=stops,
            accept=accept,
            cmd_gate="tg gate %d" % step["n"],
            cmd_sync="tg sync --step %d --session <你自己的会话 id>" % step["n"],
        )
        p = bdir / ("%d-%s.md" % (step["n"], role))
        p.write_text(body, encoding="utf-8")
        briefs[role] = str(p.relative_to(root)).replace("\\", "/")

    state = load_state(root)
    state.setdefault("fans", []).append({
        "ts": now_iso(), "step": step["n"], "title": step["title"],
        "roles": roles, "briefs": briefs, "max_fanout": limit,
        "depth": depth, "contract": contract,
    })
    save_state(root, state)

    if a.json:
        out(json.dumps({
            "api": ORCH_JSON_API, "cmd": "fan", "ok": True,
            "step": step["n"], "title": step["title"], "depth": depth,
            "max_fanout": limit, "roles": roles, "briefs": briefs,
            "contract": contract,
            "warnings": list(WARNINGS),
            "dispatch": [{"role": r, "dir": st["roles"][r]["dir"] + "/",
                          "brief": briefs[r],
                          "gate_cmd": ["gate", str(step["n"]), "--json"],
                          "sync_cmd": ["sync", "--step", str(step["n"]),
                                       "--session", "<sid>"]}
                         for r in roles],
        }, ensure_ascii=False, indent=1))
        return

    out("已生成 %d 份简报（#%d %s，依赖深度 %d，并发上限 %d 路）："
        % (len(briefs), step["n"], step["title"], depth, limit))
    for role in roles:
        out("  %-12s %s   （只碰 %s/）"
            % (role, briefs[role], st["roles"][role]["dir"]))
    out("")
    out("接下来（orch 不替你派 agent —— 它不调模型、不管进程）：")
    out("  1. 用上面每份简报的全文，各开一个 subagent（同一批工具调用里发出去才是真并发）")
    out("  2. 第一路拿边界：tg start %d" % step["n"])
    out("  3. 每路收工后各自归账：tg sync --step %d --session <该 subagent 的会话 id>"
        % step["n"])
    out("  4. 验收：tg gate %d --json（exit code 说话）" % step["n"])
    out("  5. 全部通过再 tg done %d，然后 orch rollup 看三层汇总" % step["n"])


# ---------------------------------------------------------------- rollup

REVIEW_REQUIRED = ("schema", "step", "reviewer", "verdict")


def read_review(root, n):
    """读 .plan/review-<n>.json。文件坏了要明说，不许当成「没评过」静默略过。"""
    p = root / ".plan" / ("review-%d.json" % n)
    if not p.is_file():
        return None, None
    try:
        d = read_json(p)
    except Exception as e:
        return None, "review-%d.json 解析不了：%s" % (n, e)
    missing = [k for k in REVIEW_REQUIRED if k not in d]
    if missing:
        return None, "review-%d.json 缺字段：%s" % (n, "、".join(missing))
    if d.get("reviewer") not in REVIEWERS:
        return None, "review-%d.json 的 reviewer=%r 不在允许列表里（%s）" % (
            n, d.get("reviewer"), "/".join(REVIEWERS))
    return d, None


def cmd_rollup(a):
    root = find_root()
    status = need_plan(root)
    report, _ = tg_json(["report"], root)
    steps = status["steps"]

    only = a.step
    if only is not None:
        steps = [s for s in steps if s["n"] == only]
        if not steps:
            die("计划里没有 #%d" % only)

    cost_by_step = {}
    if report and report.get("steps"):
        cost_by_step = {s["n"]: s for s in report["steps"]}

    rows = []
    bad_files = []
    for s in steps:
        n = s["n"]
        gate = None
        if a.gate:
            g, rc = tg_json(["gate", str(n)], root)
            if g is None:
                bad_files.append("gate #%d 的 --json 输出解析不了" % n)
            else:
                gate = {"ok": bool(g.get("ok")), "passed": g.get("passed", 0),
                        "failed": g.get("failed", 0), "timed": g.get("timed", 0)}
        review, err = read_review(root, n)
        if err:
            bad_files.append(err)
        rows.append({
            "n": n, "title": s["title"], "status": s["status"],
            "accept_n": len(s.get("accept") or []),
            # 断言层：默认读的是计划里的状态，不是现在重跑的结果（要真跑加 --gate）
            "gate": gate,
            "gate_src": "rerun" if a.gate else "plan_status",
            "review": review,
            "review_err": err,
            "cost_usd": (cost_by_step.get(n) or {}).get("cost_usd"),
            "reqs": s.get("reqs", 0),
        })

    # 三层分列，不许混着说「AI 评价通过」
    #
    # 打架检测**必须**有真断言结果：不重跑 gate 时 status==done 只说明「当时过了」，
    # 而现在这套代码可能已经变了。拿它去比语义 verdict，得出的「没有打架」是假的 ——
    # 所以这里宁可不检测，并且明说没检测，也不给一个看着像结论的 0。
    conflicts = []
    if a.gate:
        for r in rows:
            if not r["review"]:
                continue
            v = str(r["review"].get("verdict", "")).lower()
            g = r["gate"]
            if g is None or v not in ("pass", "fail"):
                continue
            if v == "pass" and not g["ok"]:
                conflicts.append((r["n"], "断言没过，但 reviewer 说 pass"))
            elif v == "fail" and g["ok"]:
                conflicts.append((r["n"], "断言过了，但 reviewer 说 fail"))

    reviewed = [r for r in rows if r["review"]]
    human_ok = [r for r in reviewed if r["review"].get("human_verified") is True]
    jev_rows = [r for r in reviewed if r["review"].get("reviewer") == "jev"]
    jev_spot = [r for r in jev_rows if r["review"].get("human_verified") is True]

    payload = {
        "api": ORCH_JSON_API, "cmd": "rollup", "ok": True,
        "plan_id": status["plan"]["id"], "gate_src": "rerun" if a.gate else "plan_status",
        "steps": rows,
        "conflicts": [{"n": n, "why": w} for n, w in conflicts],
        "conflict_check": ("rerun" if a.gate else "skipped_no_gate"),
        "reviewed": len(reviewed), "human_verified": len(human_ok),
        "jev_dual_track": {"reviews": len(jev_rows), "spotchecked": len(jev_spot)},
        "bad_review_files": bad_files,
        "warnings": list(WARNINGS),
        "note": "verdict 只是概率建议；步骤完成与否只由断言 exit code 决定。",
    }

    if a.json:
        out(json.dumps(payload, ensure_ascii=False, indent=1))
        return

    out("== 第 1 层：断言（exit code 说了算）==")
    if not a.gate:
        out("  （读的是计划里的状态，不是现在重跑的结果；要真跑加 --gate）")
    for r in rows:
        g = r["gate"]
        if not r["accept_n"]:
            # 零断言步骤在 tg 那边 gate 是「通过」的（没有可失败的断言）。
            # 直接印「通过」就是自欺：没人验过它，它只是没被检查。
            out("  #%d %-26s 无断言 —— 既不算通过，也不算破了"
                % (r["n"], r["title"][:26]))
            out("      !! 补 --accept，或者把它降级成「不再是步骤」")
            continue
        if g is None:
            out("  #%d %-26s 计划状态=%-8s 断言 %d 条（未重跑，加 --gate 才看得到现在）"
                % (r["n"], r["title"][:26], r["status"], r["accept_n"]))
        else:
            tag = "通过" if g["ok"] else "不通过"
            extra = ("  ⏳%d 条没验完（既非通过也非破了）" % g["timed"]) if g["timed"] else ""
            out("  #%d %-26s %s（%d 过 / %d 破 / %d 超时）%s"
                % (r["n"], r["title"][:26], tag, g["passed"], g["failed"],
                   g["timed"], extra))

    out("")
    out("== 第 2 层：语义（只出概率，不判生死）==")
    if not reviewed:
        out("  一条 review-*.json 都没有。这层可以空着——但没有它，")
        out("  你只有一个「没崩」的信号，不知道「做对了」没有。")
    for r in reviewed:
        rv = r["review"]
        probs = rv.get("probs") or {}
        p = probs.get("pass")
        out("  #%d %-24s %-8s %-8s%s%s"
            % (r["n"], r["title"][:24], rv.get("reviewer", "-"),
               str(rv.get("verdict", "-")),
               ("  概率 %s" % (("%.2f" % p) if isinstance(p, (int, float)) else p))
               if p is not None else "",
               "" if rv.get("human_verified") is True else "  （未人工抽查）"))

    out("")
    out("== 第 3 层：人（只在两层打架时介入）==")
    if not a.gate:
        out("  ⏸ 没查。打架检测要真断言结果，而这次没重跑 gate ——")
        out("    加 --gate 才谈得上「打架」。「没查」不等于「没打架」。")
    elif conflicts:
        for n, why in conflicts:
            out("  ⚔ #%d %s" % (n, why))
        out("  分层打架一律交给人，不许「谁说的算」和稀泥。")
    else:
        out("  没有打架（断言与语义 verdict 一致或缺失）。")
    if jev_rows:
        out("")
        out("  jev 双轨期：%d 次评价里有 %d 次做过人工抽查。"
            % (len(jev_rows), len(jev_spot)))
        if len(jev_spot) < len(jev_rows):
            out("  抽查没做全之前，jev 的 verdict **不许**当 done 的依据 ——")
            out("  它只是「另一种看法」，而 done 只认断言 exit code。")
    if bad_files:
        out("")
        for b in bad_files:
            out("  !! %s" % b)

    if a.cost:
        out("")
        out("== 钱（每步）==")
        tot = 0.0
        for r in rows:
            c = r["cost_usd"]
            if c is None:
                out("  #%d %-24s 没有入账" % (r["n"], r["title"][:24]))
                continue
            tot += float(c)
            out("  #%d %-24s $%.6f ｜ %d 轮" % (r["n"], r["title"][:24], float(c), r["reqs"]))
        out("  合计 $%.6f" % tot)
        out("  这是账本里的**下限**：没 sync 的会话不在这里。"
            + ("（这次一步都没入账，所以合计 0 不代表不花钱）" if tot == 0 else ""))


# ---------------------------------------------------------------- 入口

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="orch",
        description="多角色编排层：只经 tg 的 CLI + --json 契约，不调模型、不管进程")
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("role", help="登记/查看角色（角色 = 一个它独占的目录）")
    p.add_argument("action", choices=["add", "list"])
    p.add_argument("name", nargs="?", help="add 时的角色名")
    p.add_argument("--dir", help="这个角色独占的产物目录")
    p.add_argument("--reviewer", default="jev",
                   help="谁来评：%s（默认 jev，离线、零外传）" % "/".join(REVIEWERS))
    p.add_argument("--note", help="一句话说明")
    p.add_argument("--force", action="store_true", help="覆盖已登记的同名角色")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("fan", help="派发前检查 + 生成每个角色一份简报（不派发、不调模型）")
    p.add_argument("step", type=int)
    p.add_argument("--roles", help="逗号分隔；默认全部已登记角色")
    p.add_argument("--max-fanout", type=int, default=None,
                   help="这次最多几路（默认取 .orch/roles.json 里的 %d）" % DEFAULT_MAX_FANOUT)
    p.add_argument("--contract", help="契约文件路径；默认自动找已完成步骤里产出的 .md")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("rollup", help="三层汇总：断言 / 语义 / 人（不许混着说）")
    p.add_argument("--step", type=int, default=None)
    p.add_argument("--gate", action="store_true",
                   help="真重跑每步断言（慢但是真数据）；不给就只读计划状态")
    p.add_argument("--cost", action="store_true", help="附上每步花了多少钱")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args()

    if getattr(a, "version", False) or not a.cmd:
        out("orch 0.1.0（tg --json api %d）  零依赖 · 不调模型 · 不管进程"
            % TG_JSON_API)
        if not a.cmd:
            ap.print_help()
        return 0

    if a.cmd == "role":
        if a.action == "add":
            if not a.name:
                die("role add 要给角色名：orch role add backend --dir api/")
            cmd_role_add(a)
        else:
            cmd_role_list(a)
        return 0

    if a.cmd == "fan":
        cmd_fan(a)
        return 0

    if a.cmd == "rollup":
        cmd_rollup(a)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
