#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orch —— 多角色编排层。零依赖，只经 subprocess 调 tg 的 CLI + 起 opencode 会话。

它**自己不看代码、不写产物、不评质量**：

    role add / role list   把「谁负责哪个目录、谁来评」落盘成一份可以吵的契约
    fan <step>             派发前先查一遍：并发路数、目录重叠、依赖深度、契约在不在；
                           全过了才给每个角色生成一份自足式简报
    dispatch <step>        真派发：照简报起 N 个 headless 会话（opencode run），
                           收 exit code、按会话 id 点名归账、超预算就停手
    rollup                 收工后把「断言层 / 语义层 / 人」三层分开汇总

`dispatch` 是**显式**的：不打这个子命令，orch 一个会话都不起。
`fan` 永远只出简报 —— 先看清要派什么，再决定花钱。

四条不让步的规矩：
  1. 门控生死**只认 exit code**。review-*.json 里的 verdict 是概率，不是结论，
     任何时候都不许拿它替代 `tg done`。
  2. 并发的安全边界是**目录边界**。两个角色的目录一重叠，就等于没切。
  3. 并发路数有硬上限（默认 2）。多智能体烧的是 token 不是时间——
     Anthropic 实测 15×、MAST 调查里头号失败模式就是 cost runaway。
  4. 归账必须**点名会话**（`sync --session`）。子会话环境里的 OPENCODE_SESSION_ID
     指的是**主会话**，不是它自己 —— 这是实测出来的坑（默认值会把主会话的
     20 轮、129k input 记到某一路头上），不是理论风险。

跑法：python orch.py <命令> ...
      tg.py 默认找同目录、ORCH_TG、或 ~/.config/opencode/skills/tollgate/tg.py
"""

import argparse
import json
import os
import re
import shutil
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


def tg_json(args, root, json_flag=True):
    """调一条 tg 的 --json 命令，返回 (payload, exitcode)。

    `--json` 不是每条 tg 子命令都认（`tg start` 就没有）：给它 json_flag=False，
    否则 argparse 会以 exit 2 拒绝，而你会以为是「预算到顶」——
    这种「闸说不行、其实是命令写错了」的误判比没有闸更危险。

    解析不了就是 None —— **不猜、不从人类可读输出里抠数字**。
    抠出来的数看着像真的，错的时候没人查得出来。
    """
    argv = [sys.executable, str(tg_path()), *args] + (["--json"] if json_flag else [])
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


def dir_disp(root, d):
    """角色的 `--dir` 怎么显示 —— `ui/` 加斜杠，`tg.py` 不加。

    以前一律 `{dir}/`，于是单文件角色的简报上写着「不许读改 `tg.py/`」——
    一个不存在的目录。真按它办事的 agent 会以为那个文件不在禁区里。
    判据只认盘上是什么，不认名字里有没有点（`v1.0/` 也是目录）。
    """
    d = str(d or "").replace("\\", "/").strip().strip("/")
    if not d:
        return d
    try:
        if (root / d).is_file():
            return d
    except OSError:
        pass
    return d + "/"


def accept_targets(x):
    """一条断言里点名了哪些路径。认不出来就返回空（= 这条算整步的）。"""
    s = (x or "").strip()
    if not s:
        return []
    if s.startswith(("grep:", "nogrep:")):
        return [s.rsplit("@", 1)[1].strip()] if "@" in s else []
    if s.startswith(("file:exists:", "file:absent:")):
        return [s.split(":", 2)[2].strip()]
    if s.startswith(("cmd:", "ack:")):
        # 命令行里点名了哪个目录/文件，那条就算谁的
        body = s.split(":", 1)[1]
        toks = [t for t in re.split(r"[\s\"'=|&;]+", body) if t]
        return [t for t in toks if "/" in t or "." in t]
    return re.findall(r"[A-Za-z0-9_.\-/]+\.[A-Za-z0-9]+", s)


def accept_owner(roles, x):
    """这条断言归哪一路：返回角色名；认不出是谁的产物就返回 ""（整步级）。"""
    for t in accept_targets(x):
        for name, info in roles.items():
            if paths_overlap(info["dir"], t) or \
               str(info["dir"]).replace("\\", "/").strip("/") == \
               t.replace("\\", "/").strip("/"):
                return name
        # 同一个目标被多个角色覆盖时上面已经返回，这里只剩「没人认领」
    return ""


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
                "max_depth": DEFAULT_MAX_DEPTH, "lead": "", "merge": "", "roles": {}}
    st = read_json(p)
    st.setdefault("schema", ORCH_SCHEMA)
    st.setdefault("max_fanout", DEFAULT_MAX_FANOUT)
    st.setdefault("max_depth", DEFAULT_MAX_DEPTH)
    # 「哪一个是大脑」和「谁合、合不上怎么办」是计划级的答案，不是角色的属性：
    # 大脑只能有一个，合并方案也只有一套。放在 roles.json 顶层，缺了就拒派发。
    st.setdefault("lead", "")
    st.setdefault("merge", "")
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
    if a.reviewer not in REVIEWERS:
        die("--reviewer 只能是 %s 之一（jev 是云上服务、代码会外传；startlux 需要 key；"
            "local-4b 是离线兜底；none = 只跑断言）" % " / ".join(REVIEWERS))
    d = str(a.dir or "").replace("\\", "/").strip().strip("/")
    if not d:
        die("--dir 不许为空。角色边界必须是目录边界，没有目录就不算角色。")
    task = (getattr(a, "task", None) or "").strip()
    merge = (getattr(a, "merge", None) or "").strip()
    drop_lead = bool(getattr(a, "no_lead", False))
    cur_lead = (st.get("lead") or "").strip()
    if drop_lead:
        # 撤大脑得两步走，并且必须说清是谁撤的：单独一个 --force 就能换人，
        # 等于给「为了让流程往下走，先把拍板的换掉」留了后门。
        if not a.force:
            die("撤销大脑要明说：orch role add %s --dir %s --force --no-lead\n"
                "    少了 --force 就等于「手滑就把拍板的撤了」。" % (name, d))
        if cur_lead != name:
            die("只有现任大脑能被撤：现在是 %r，不是 %r。\n"
                "    要换人请两步走：① orch role add %r --dir <目录> --force --no-lead\n"
                "                    ② orch role add <新大脑> --dir <目录> --lead --task \"...\""
                % (cur_lead or "（没有）", name, cur_lead or "（没有）"))
    if a.lead and not drop_lead:
        if cur_lead and cur_lead != name:
            die("大脑只能有一个：现在登记的是 %r，而且 --force 也换不掉它。\n"
                "    加一个旗子就能换人，等于给「为了让流程往下走先换掉拍板的」留后门。\n"
                "    真要换，两步走（每步都留痕）：\n"
                "      ① orch role add %s --dir %s --force --no-lead\n"
                "      ② orch role add %s --dir %s --lead --task \"...\"\n"
                "    多智能体最贵的失败不是算力不够，是没人拍板 —— 两个 lead 等于零个 lead。"
                % (cur_lead, cur_lead,
                   (st["roles"].get(cur_lead) or {}).get("dir", "<目录>"), name, d))
    if name in st["roles"] and not a.force:
        die("角色 %r 已登记。要改就加 --force" % name)
    for other, info in st["roles"].items():
        if other == name:
            continue
        if paths_overlap(d, info.get("dir", "")):
            die("角色 %r 的目录 %s 和 %r 的 %s 重叠了。\n"
                "    并发的安全边界是目录边界：一重叠就是两个 agent 抢同一批文件，\n"
                "    冲突不是「可能出现」，是「什么时候出现」。先拆目录。"
                % (name, d, other, info.get("dir")))
    st["roles"][name] = {"dir": d, "reviewer": a.reviewer, "note": a.note or "",
                         "task": task or (st["roles"].get(name) or {}).get("task", "")}
    if a.lead or drop_lead:
        st.setdefault("lead_history", []).append({
            "ts": now_iso(), "role": name,
            "action": "drop-lead" if drop_lead else "set-lead",
            "from": cur_lead or None,
            "note": a.note or "",
        })
    if drop_lead:
        st["lead"] = ""
    elif a.lead:
        st["lead"] = name
    if merge:
        st["merge"] = merge
    save_roles(root, st)
    out("已登记角色 %s → %s（reviewer=%s%s）"
        % (name, dir_disp(root, d), a.reviewer,
           "，lead=大脑" if (a.lead and not drop_lead) else ("，已撤销大脑" if drop_lead else "")))
    if a.reviewer == "startlux":
        out("  注意：startlux 的 key 还没接上时它只是个空位，别当它能评。")
    # 登记不是目的，开工前那七问答得上来才是。缺什么就当场说出来，
    # 别等到 fan 才拒 —— 那时候人已经在准备开 agent 了。
    rest = []
    if not st["roles"][name]["task"]:
        rest.append("这一路具体做什么：--task \"产出什么、给谁用\"")
    if not st.get("lead"):
        rest.append("哪个是大脑：orch role add <名字> --lead --task \"...\"")
    if not (st.get("merge") or "").strip():
        rest.append("谁合、合不上怎么办：--merge \"谁看、谁合、合不上怎么办\"")
    if rest:
        out("")
        out("开工七问还差 %d 项（fan 会因此拒绝派发）：" % len(rest))
        for x in rest:
            out("  · " + x)


def cmd_role_list(a):
    root = find_root()
    st = load_roles(root)
    roles = st["roles"]
    if a.json:
        out(json.dumps({"api": ORCH_JSON_API, "cmd": "role-list", "ok": True,
                        "schema": st["schema"],
                        "max_fanout": st["max_fanout"],
                        "max_depth": st["max_depth"],
                        "lead": st.get("lead") or None,
                        "merge": st.get("merge") or None,
                        "roles": [{"name": k, "dir": v.get("dir"),
                                   "reviewer": v.get("reviewer"),
                                   "task": v.get("task", ""),
                                   "is_lead": k == (st.get("lead") or ""),
                                   "note": v.get("note", "")}
                                  for k, v in roles.items()]},
                       ensure_ascii=False, indent=1))
        return
    if not roles:
        out("还没有角色。加一个：orch role add backend --dir api/ --reviewer none --task \"...\"")
        out("（并发的安全边界是目录边界：一个角色 = 一个它独占的目录）")
        return
    out("角色（%d 个，并发上限 %d 路）:" % (len(roles), st["max_fanout"]))
    for k, v in roles.items():
        out("  %-12s → %-20s reviewer=%s%s%s"
            % (k, dir_disp(root, v.get("dir", "")) or "-", v.get("reviewer", "-"),
               "  ← 大脑" if k == (st.get("lead") or "") else "",
               ("   # " + v["note"]) if v.get("note") else ""))
        out("  %-12s   %s" % ("", ("做什么：" + v["task"]) if v.get("task")
                              else "做什么：（没写 —— fan 会拒派发）"))
    out("")
    out("开工七问：")
    out("  ① 并发几路        ≤ %d 路（%d 个角色已登记）" % (st["max_fanout"], len(roles)))
    out("  ② 哪个是大脑      %s" % (st.get("lead") or "（没定 → fan 会拒）"))
    out("  ③ 几个角色/各碰哪 %s" % "、".join("%s→%s" % (k, dir_disp(root, v.get("dir", "")) or "-")
                                          for k, v in roles.items()))
    miss = [k for k, v in roles.items() if not (v.get("task") or "").strip()]
    out("  ④ 每路做什么      %s" % ("都写了" if not miss else "缺：" + "、".join(miss)))
    out("  ⑤ 谁合/合不上     %s" % (st.get("merge") or "（没写 → fan 会拒）"))
    dirs = [v.get("dir", "") for v in roles.values()]
    for i in range(len(dirs)):
        for j in range(i + 1, len(dirs)):
            if paths_overlap(dirs[i], dirs[j]):
                warn("目录重叠：%s 与 %s —— 这两个角色不能并行" % (dirs[i], dirs[j]))


# ---------------------------------------------------------------- fan

BRIEF_TMPL = """# 简报 · #{n} {title} · 角色 `{role}`

你是这个计划里**唯一**的 `{role}`。这份文件是自足的：需要的都在这里，别去猜别人的实现。

## 你只能碰这一个目录

`{dir_disp}`

**不许读、不许改**这些地方（它们是别的角色/别组的产物，读了就等于用别人的中间状态污染判断）：
{forbidden}

## 你这一路具体做什么

{task}

只做这一件事。发现还得做别的 → **停下来汇报**，不要顺手把别人的目录也改了：
「各自都对、合不上」就是从这里开始的。

## 唯一的共同事实来源

{contract}

字段名、路由、状态码、错误体都以它为准。它不够用 → **停下来汇报**，不要自己扩充、
不要自己发明约定、不要为了「跑起来」去猜。

## 这一步的边界

- **你这一路只能产出在 `{dir_disp}`** —— 越过这条就算失败，断言全绿也不算。
- 这一路的请求轮数上限：{rounds}
- 你正在做的步骤：`#{n} {title}`（整步产物目录：`{out_disp}`；那是步骤级的，
  有时候写的是别的路的名字，**别拿它当你的边界**）
- 步骤级停止条件（**对所有角色一样**；里面对某个角色点名的那句只对那个角色生效，
  不是给你的）：{stops}

## 验收断言（会真跑；exit code 才是生死）

**你这一路必须自己跑绿的**（断言点名的是 `{dir_disp}`）：
{accept_own}

**整步断言**（别的路也在里面：你跑不过不等于你没做完，但整步就过不了闸）：
{accept_step}

通不过就是没做完。不要用「应该可以了」代替跑一遍。

## 收工怎么算

1. 跑 `{cmd_gate}`：断言里**属于这一路的**必须全绿；整步还差别的路的活，不算你没做完；
2. 把它记进账：`{cmd_sync}`；
3. 你**不要**自己调 `tg done`，交给上层。

## 报告格式（三到五行，不要写作文）

1. 创建/修改了哪些文件
2. 断言里哪几条是你亲手验过的
3. 有没有偏离契约（没有就写「无」）
"""


def prepare_fan(a, root):
    """fan 和 dispatch 共用的「派发前检查 + 简报生成」。

    拿到返回字典就说明这一批可以派了：里面任何一项不合格都已经 die 掉。
    dispatch 不许另写一套检查 —— 两套检查迟早会不一致，
    然后并发就会在没人拦的地方开跑。
    """
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

    # ⑤ 开工七问：前五问必须由人给答案，后两问由工具给（命令是固定的，不用人发明）。
    #    缺一项就拒绝派发 —— 这不是「多问一句更稳」，是把「谁拍板、谁合」写死在文件里。
    lead = (st.get("lead") or "").strip()
    merge = (st.get("merge") or "").strip()
    gaps = []
    if not lead:
        gaps.append("② 哪个是大脑 —— 没登记 lead。多智能体最贵的失败不是算力不够，是没人拍板；\n"
                    "        两个 lead 等于零个 lead。\n"
                    "        → orch role add <名字> --dir <目录> --lead --task \"...\"")
    elif lead not in st["roles"]:
        gaps.append("② 哪个是大脑 —— roles.json 里 lead=%r 已经不在角色表里了" % lead)
    for role in roles:
        if not (st["roles"][role].get("task") or "").strip():
            gaps.append("④ %s 具体做什么 —— 没写 --task。只有目录不算任务：\n"
                        "        指令缺一句，执行的人就会自己发明一句。\n"
                        "        → orch role add %s --dir %s --task \"这一路产出什么、给谁用\""
                        % (role, role, st["roles"][role]["dir"]))
    if not merge:
        gaps.append("⑤ 谁合、合不上怎么办 —— 没写合并方案。并发最贵的一课是「各自都对，合不上」。\n"
                    "        → orch role add <lead> --lead --task \"...\" --merge \"谁看、谁合、合不上怎么办\"")
    if gaps:
        die("开工七问还差 %d 项，不派发（一份简报都没生成）：\n    %s"
            % (len(gaps), "\n    ".join(gaps)))

    # ⑥ 现状提醒：doing 最多 DEFAULT_MAX_FANOUT 路，但每路必须是独立会话
    others_doing = [n for n in status.get("doing", []) if n != a.step]
    if others_doing or len(roles) > 1:
        if getattr(a, "cmd", "") == "dispatch":
            # dispatch 已经把这四件事全包了，再念一遍手动版指令只会让人以为要自己做。
            warn("并发的前提是**每路一个独立会话**：dispatch 会为每一路各起一个，"
                 "并按 %d 路一批往下走。\n"
                 "    start / 归账 / 点名验都由它做，你只需要看最后那行 exit code。"
                 % (getattr(a, "concurrency", None) or limit))
        else:
            warn("并发的前提是**每路一个独立会话**。tg 允许最多 %d 步同时 doing"
                 "（默认 2 路，第 3 路要 tg start N --max-fanout 3）：\n"
                 "    每路各开一个 subagent 会话 → 在**自己的会话里** tg start <n> 拿归账边界\n"
                 "    → 收工 tg gate <n> 点名验、tg sync --step <n> --session <sid> 点名归账。\n"
                 "    两路塞进同一个会话是最坏的情况：轮次混在一起，点谁的名都是猜。"
                 % DEFAULT_MAX_FANOUT)

    # 步骤级 stop 里点名某个角色时，另外几路读到的那句是噪音 —— 明说出来。
    # 上一轮真跑的简报里，tester 的边界上挂着「前端拿不到契约里的字段就停下来」，
    # 那是 frontend 的刹车。抄在某一路名下，人就会以为这条是给它的。
    for s_txt in (step.get("stop") or []):
        named = [r for r in roles if r in s_txt]
        if named and len(named) < len(roles):
            warn("停止条件点了 %s 的名：另外几路读到的那句不对它们生效（简报里已标成步骤级）。"
                 "要每路各有刹车，就写成不点名的，或者拆成两步。" % "、".join(named))

    # ⑥ 生成简报
    bdir = orch_dir(root) / "briefs"
    bdir.mkdir(parents=True, exist_ok=True)
    all_accept = list(step.get("accept") or [])
    stops = "；".join(step.get("stop") or []) or "（未设置 → 建议补：现实与计划不符时停下来汇报）"

    # 断言按「点名了谁的目录」分给各路。全部塞给每个人的那种写法有个真实代价：
    # 一路的简报里有另一路正在同批写的测试，于是「先跑通自己那一路」事实上不可能，
    # agent 只能要么越界去修别人的目录，要么报告「我这边过不了闸」。
    own_of = {x: accept_owner(st["roles"], x) for x in all_accept}

    briefs = {}
    for role in roles:
        # 禁区要列**所有**别的角色的目录 —— 包括同批并发的那个。
        # 这里最容易写错成「同批的不算冲突」，那正好把整个设计的支点拆了：
        # 目录边界之所以是安全边界，是因为不管同时跑不跑，谁都不许越界。
        forbid = []
        for other, info in st["roles"].items():
            if other == role:
                continue
            forbid.append("- `%s`（角色 %s%s）"
                          % (dir_disp(root, info["dir"]), other,
                             "，正在同批并发" if other in roles else ""))
        forbid.append("- `.plan/`、`.orch/`（账本与编排状态，只读不写）")
        forbid.append("- 契约文件本身（不够用就停下来汇报）")

        own = [x for x in all_accept if own_of[x] == role]
        rest = [x for x in all_accept if own_of[x] != role]
        own_md = "\n".join("- `%s`" % x for x in own) or \
            ("- （一条都没有 → 说明这步没有任何断言能验证这一路的产物：**停下来汇报**，"
             "别自己加断言、也别去改别人的目录）")
        rest_md = "\n".join(
            "- `%s`%s" % (x, ("（这条归 `%s` 那一路）" % own_of[x]) if own_of[x] else "")
            for x in rest) or "- （没有整步断言）"
        if not all_accept:
            own_md = "- （这步没有断言 → 它没有验证价值，先补 `--accept`）"
            rest_md = "- （同上）"

        body = BRIEF_TMPL.format(
            n=step["n"], title=step["title"], role=role,
            dir_disp=dir_disp(root, st["roles"][role]["dir"]),
            task=st["roles"][role].get("task") or "（没写 —— 停下来要一句话再动手）",
            forbidden="\n".join(forbid),
            contract=("`%s`（先完整读一遍）" % contract) if contract
            else "**没有契约文件** —— 先停下来要一份，别自己发明字段名。",
            out_disp=dir_disp(root, step.get("out") or "-"),
            rounds=(("%d 轮" % step["budget_rounds"]) if step.get("budget_rounds")
                    else "未设（建议补 --rounds）"),
            stops=stops,
            accept_own=own_md,
            accept_step=rest_md,
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
        "lead": lead, "merge": merge,
    })
    save_state(root, state)

    # 七问的答案要一起落进记录：只写「派了几路」，事后没人知道谁拍板、谁合并。
    preflight = {
        "fanout": {"count": len(roles), "limit": limit},
        "lead": lead,
        "routes": [{"role": r, "dir": dir_disp(root, st["roles"][r]["dir"]),
                    "task": st["roles"][r].get("task", ""),
                    "own_asserts": [x for x in all_accept if own_of[x] == r],
                    "step_asserts": [x for x in all_accept if own_of[x] != r]}
                   for r in roles],
        "merge": merge,
        # 后两问由工具答：命令是固定的，不需要人去发明一套「怎么看进度」
        "observe": ["tg status", "orch rollup", "tg serve（只读看板，只绑 127.0.0.1）"],
        "done_when": ["tg gate %d 的 exit code 为 0" % step["n"],
                      "语义层的 verdict 与概率都不构成 done 依据"],
    }

    return {
        "step": step, "roles": roles, "briefs": briefs, "limit": limit,
        "depth": depth, "contract": contract, "lead": lead, "merge": merge,
        "st": st, "status": status, "all_accept": all_accept, "own_of": own_of,
        "preflight": preflight,
    }


def cmd_fan(a):
    root = find_root()
    ctx = prepare_fan(a, root)
    step, roles, briefs = ctx["step"], ctx["roles"], ctx["briefs"]
    contract, lead, merge = ctx["contract"], ctx["lead"], ctx["merge"]
    limit, depth, st = ctx["limit"], ctx["depth"], ctx["st"]

    if a.json:
        out(json.dumps({
            "api": ORCH_JSON_API, "cmd": "fan", "ok": True,
            "step": step["n"], "title": step["title"], "depth": depth,
            "max_fanout": limit, "roles": roles, "briefs": briefs,
            "contract": contract, "preflight": ctx["preflight"],
            "warnings": list(WARNINGS),
            "dispatch": [{"role": r, "dir": dir_disp(root, st["roles"][r]["dir"]),
                          "brief": briefs[r],
                          "gate_cmd": ["gate", str(step["n"]), "--json"],
                          "sync_cmd": ["sync", "--step", str(step["n"]),
                                       "--session", "<sid>"]}
                         for r in roles],
        }, ensure_ascii=False, indent=1))
        return

    out("开工七问（②④⑤ 由你答，⑥⑦ 由工具答）：")
    out("  ① 并发几路        %d 路（上限 %d 路）" % (len(roles), limit))
    out("  ② 哪个是大脑      %s" % lead)
    for r in roles:
        out("  ③ %-12s 只碰 %-12s ④ %s"
            % (r, dir_disp(root, st["roles"][r]["dir"]), st["roles"][r].get("task", "")))
    out("  ⑤ 谁合/合不上     %s" % merge)
    out("  ⑥ 怎么观察进度    tg status（谁在 doing）· orch rollup（三层分列）· tg serve（只读看板）")
    out("  ⑦ 怎么算跑通      tg gate %d 的 exit code 为 0 —— 概率和账本都不算" % step["n"])
    out("")
    out("已生成 %d 份简报（#%d %s，依赖深度 %d，并发上限 %d 路）："
        % (len(briefs), step["n"], step["title"], depth, limit))
    for role in roles:
        out("  %-12s %s   （只碰 %s）"
            % (role, briefs[role], dir_disp(root, st["roles"][role]["dir"])))
    out("")
    out("接下来，两条路选一条：")
    out("  A. 让机器派（推荐）：python orch.py dispatch %d --concurrency 2" % step["n"])
    out("     它自己 start、起会话、按会话 id 点名归账、超 --max-usd 就停手。")
    out("  B. 自己派：用上面每份简报的全文各开一个 subagent")
    out("     （同一批工具调用里发出去才是真并发），然后手动走下面 1–4：")
    out("  1. 第一路拿边界：tg start %d" % step["n"])
    out("  2. 每路收工后各自归账：tg sync --step %d --session <该 subagent 的会话 id>"
        % step["n"])
    out("     ——**必须点名会话**：子会话里的 OPENCODE_SESSION_ID 是主会话，默认值会记错账")
    out("  3. 验收：tg gate %d --json（exit code 说话）" % step["n"])
    out("  4. 全部通过再 tg done %d，然后 orch rollup 看三层汇总" % step["n"])


# ---------------------------------------------------------------- dispatch

# 派发消息**永远是一行**，只点名简报文件，不塞简报正文。
# 原因不是「省长度」，是 Windows 上 opencode 是 .CMD 包装脚本：多行参数会被 cmd.exe
# 当成多条命令，`--format json` 随之失效 —— 实测多行消息时整个 JSON 流变成人看的
# TUI 渲染（带 ANSI 色），会话 id 就挖不到了，账也就归不上。单行绕开这一整类问题。
SID_RE = re.compile(r'"sessionID"\s*:\s*"(ses_[A-Za-z0-9]+)"')


def opencode_bin():
    p = shutil.which("opencode")
    if not p:
        die("PATH 里找不到 opencode。dispatch 靠它起会话；\n"
            "    只想看它准备跑什么，加 --dry-run（一次进程都不起）。")
    return p


def plan_batches(roles, limit, lead):
    """大脑先单独跑一轮，其余按 limit 分批。

    大脑和别人同批发出去，等于让其余几路读半成品 —— 而它们的简报里写着
    「契约为准、不够用就停下来汇报」，于是它们会合理地停下来等你。
    分批是省返工，不是省时间。
    """
    rest = list(roles)
    batches = []
    if lead and lead in rest and len(rest) > 1:
        batches.append([lead])
        rest.remove(lead)
    while rest:
        batches.append(rest[:limit])
        rest = rest[limit:]
    return batches


def dispatch_message(root, brief_rel, role):
    """给一路的消息：**一行**，只点名简报文件 —— 不塞正文（理由见上面那段注释）。

    简报里的禁区写着 `.plan/`、`.orch/`「只读不写」，所以让它去读自己那份简报不算越界。
    """
    disp = str((root / brief_rel)).replace("\\", "/")
    return ("你的简报在 `%s` ：先完整读一遍再动手，别凭标题猜。"
            "账和边界由派发器处理 —— 不要自己跑 tg sync，也不要 tg start / tg done。"
            "只写你自己那个目录，写完就停。" % disp)


def spawn_role(opencode, root, role, info, message, a, logpath, session=None):
    """起一路（或续一路）。返回 (proc, 日志句柄)。"""
    argv = [opencode, "run", message, "--agent", a.agent,
            "--title", "%s@%s" % (role, root.name),
            "--format", "json", "--auto"]
    if a.model:
        argv += ["--model", a.model]
    if session:
        argv += ["--session", session]
    d = root / info["dir"]
    argv.append(str(d))
    logpath.parent.mkdir(parents=True, exist_ok=True)
    fh = open(logpath, "wb")
    try:
        proc = subprocess.Popen(argv, cwd=str(d), stdout=fh, stderr=subprocess.STDOUT)
    except OSError as e:
        fh.close()
        die("起不动 %s 这一路：%s" % (role, e))
    return proc, fh


def read_session(logpath):
    """从 --format json 的日志里挖会话 id。挖不到就返回 ""，不猜。"""
    try:
        txt = logpath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = SID_RE.search(txt)
    return m.group(1) if m else ""


def cmd_dispatch(a):
    root = find_root()
    ctx = prepare_fan(a, root)
    step, roles, st = ctx["step"], ctx["roles"], ctx["st"]
    limit, lead, n = ctx["limit"], ctx["lead"], step["n"]
    # 两个数不是一回事：
    #   limit（--max-fanout）= 这一批**总共**派几路，是「值不值得付这些钱」的上限
    #   conc（--concurrency） = **同时**跑几路，是分批的依据
    # 4 路并发 2 就是两批。合并成一个数会逼人「想省时间就得一次全烧」。
    conc = a.concurrency or limit
    if conc < 1:
        die("--concurrency 至少是 1（同时跑几路）。要一路一路跑就写 1，别写 0。")
    model = (a.model or os.environ.get("ORCH_MODEL") or "").strip()

    def say(msg=""):
        # --json 时 stdout 只许有 JSON：人看的进度一律压掉，不是转到 stderr
        # （那是给「出错和警告」用的，进度不是）。真要看得加 --verbose。
        if not a.json:
            out(msg)

    opcode = None if a.dry_run else opencode_bin()

    # 归账边界：sync 按「这步什么时候开始」切轮次。没 start 就派发，
    # 等会儿归账要么切在计划创建那一刻（把别人早先的轮次也算进来），要么直接报错。
    started = False
    if step["status"] != "doing":
        if a.dry_run:
            warn("#%d 现在还不是 doing；真跑时派发器会先 `tg start %d` 拿归账边界。"
                 % (n, n))
        else:
            _s, rc = tg_json(["start", str(n)], root, json_flag=False)
            if rc != 0:
                die("`tg start %d` 没通过（exit=%d）。预算到顶、依赖没完成、"
                    "或者同时 doing 的步数超限 ——\n    dispatch 不绕过 tg 的闸，"
                    "先 `tg status` 看是哪一种。" % (n, rc))
            started = True

    batches = plan_batches(roles, conc, lead)
    runs = []
    stop_reason = ""
    total_usd = 0.0
    run_dir = orch_dir(root) / "runs"
    planned_flat = []

    for bi, batch in enumerate(batches, 1):
        say("== 第 %d/%d 批：%s ==" % (bi, len(batches), "、".join(batch)))
        planned = []
        for role in batch:
            info = st["roles"][role]
            msg = dispatch_message(root, ctx["briefs"][role], role)
            argv = ["opencode", "run", "<%d 字的消息>" % len(msg),
                    "--agent", a.agent, "--title", "%s@%s" % (role, root.name),
                    "--format", "json", "--auto"]
            if model:
                argv += ["--model", model]
            argv.append(str((root / info["dir"]).resolve()))
            planned.append({"role": role, "dir": dir_disp(root, info["dir"]),
                            "argv": argv, "message_chars": len(msg), "msg": msg})
        planned_flat.extend(planned)
        if a.dry_run:
            for p in planned:
                say("  %-12s 在 %s" % (p["role"], p["dir"]))
                say("      " + " ".join(p["argv"]))
            continue

        procs = []
        for p in planned:
            role, info = p["role"], st["roles"][p["role"]]
            d = root / info["dir"]
            if not d.is_dir():
                warn("%s 这一路的目录 %s 还不存在，已建空目录（否则会话没有工作目录）。"
                     % (role, dir_disp(root, info["dir"])))
                d.mkdir(parents=True, exist_ok=True)
            logpath = run_dir / ("%d-%s-%s.jsonl"
                                 % (n, role, time.strftime("%H%M%S")))
            proc, fh = spawn_role(opcode, root, role, info, p["msg"], a, logpath)
            procs.append((role, proc, fh, logpath, now_iso()))
            say("  派发 %-12s → %-16s pid %d"
                % (role, dir_disp(root, info["dir"]), proc.pid))

        # 全部起完之后才 wait：先起再等才是并发，边起边等就是串行。
        for role, proc, fh, logpath, t0 in procs:
            try:
                rc = proc.wait(timeout=a.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = -9
                warn("%s 超时 %d 秒，已杀掉（日志 %s）。"
                     % (role, a.timeout, logpath.name))
            fh.close()
            sid = read_session(logpath)
            rec = {"role": role, "dir": dir_disp(root, st["roles"][role]["dir"]),
                   "session": sid or None, "exit": rc, "retries": 0,
                   "log": str(logpath.relative_to(root)).replace("\\", "/"),
                   "started": t0, "ended": now_iso(),
                   # 「归上了账」和「跑通了」是两件事。没归上就等于这一路的钱
                   # 花了但账本里没有 —— 账本撒谎比缺一路更坏，所以不许算 ok。
                   "accounted": bool(sid),
                   "synced_rounds": None, "cost_usd": None}
            if sid:
                s, src = tg_json(["sync", "--step", str(n), "--session", sid], root)
                if s is not None:
                    sy = s.get("synced") or {}
                    rec["synced_rounds"] = sy.get("reqs")
                    rec["cost_usd"] = s.get("cost")
                    if not sy.get("reqs"):
                        warn("%s 的会话里没有可归账的轮次（多半整轮都发生在 start 之前）。"
                             % role)
                else:
                    warn("%s 的账没归上：`tg sync --json` 解析不了（exit=%d）。"
                         "这一路的花费不在账本里。" % (role, src))
            else:
                warn("%s 没拿到会话 id（日志里没有 sessionID），账没归 —— "
                     "去 %s 看它到底干了什么。" % (role, logpath.name))
            runs.append(rec)
            say("  %-12s exit=%-3s 轮次=%-4s cost=%s"
                % (role, rc, rec["synced_rounds"] if rec["synced_rounds"] is not None else "-",
                   ("$%.6f" % rec["cost_usd"]) if rec["cost_usd"] else "-"))

        rep, _rc = tg_json(["report"], root)
        if rep:
            total_usd = float((rep.get("totals") or {}).get("cost_usd") or 0.0)
        if a.max_usd is not None and total_usd > a.max_usd:
            stop_reason = ("第 %d 批之后账本已 $%.6f > 上限 $%.6f，剩下的批次不派了"
                           % (bi, total_usd, a.max_usd))
            warn(stop_reason + "。要接着跑就调 --max-usd。")
            break

    # 返工回路：没跑通的那一路，在**原会话**里补跑 —— 重开会话等于重付一遍上下文。
    # 只补跑不改判据：改的还是同一份简报、同一批断言。
    if not a.dry_run and a.retries > 0:
        for rec in list(runs):
            tries = 0
            while rec["exit"] != 0 and tries < a.retries and rec["session"]:
                tries += 1
                rec["retries"] = tries
                msg = ("上一轮没跑通（exit=%s）。不要重头再来：先 `tg status` 看第 %d 步，"
                       "再补完简报里没做完的部分。收工的唯一标准是简报里点名给你那几条断言全绿。"
                       % (rec["exit"], n))
                logpath = run_dir / ("%d-%s-retry%d-%s.jsonl"
                                     % (n, rec["role"], tries, time.strftime("%H%M%S")))
                proc, fh = spawn_role(opcode, root, rec["role"],
                                      st["roles"][rec["role"]], msg, a, logpath,
                                      session=rec["session"])
                say("  返工 %-12s（第 %d 次）" % (rec["role"], tries))
                try:
                    rc = proc.wait(timeout=a.timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    rc = -9
                fh.close()
                rec["exit"] = rc
                s, _src = tg_json(["sync", "--step", str(n), "--session", rec["session"]], root)
                if s is not None:
                    rec["cost_usd"] = s.get("cost")
            if rec["retries"]:
                say("  %-12s 返工 %d 次后 exit=%s" % (rec["role"], rec["retries"], rec["exit"]))

    failed = [r for r in runs if r["exit"] != 0]
    unaccounted = [r["role"] for r in runs if not r.get("accounted")]
    ok = (not failed) and (not unaccounted) and (not stop_reason)
    state = load_state(root)
    state.setdefault("dispatches", []).append({
        "ts": now_iso(), "step": n, "roles": roles, "batches": batches,
        "model": model or None, "agent": a.agent, "dry_run": bool(a.dry_run),
        "started_by_dispatch": started, "runs": runs,
        "total_usd": total_usd or None, "stop_reason": stop_reason or None,
    })
    save_state(root, state)

    nxt = ["tg gate %d --json（exit code 说话）" % n,
           "全绿再 tg done %d" % n,
           "orch rollup --gate --cost"]

    if a.json:
        out(json.dumps({
            "api": ORCH_JSON_API, "cmd": "dispatch", "ok": ok,
            "step": n, "title": step["title"], "dry_run": bool(a.dry_run),
            "model": model or None, "agent": a.agent,
            "max_fanout": limit, "concurrency": conc, "roles": roles,
            "batches": [{"batch": i + 1, "roles": b} for i, b in enumerate(batches)],
            "started_by_dispatch": started,
            "runs": runs, "failed": [r["role"] for r in failed],
            "unaccounted": unaccounted,
            "total_usd": total_usd or None, "stop_reason": stop_reason or None,
            "planned": planned_flat,
            "warnings": list(WARNINGS), "next": nxt,
        }, ensure_ascii=False, indent=1))
        return 0 if ok else 1

    if a.dry_run:
        out("")
        out("这是 dry-run：一个进程都没起、账本一个字没动。去掉 --dry-run 才真派。")
        return 0

    out("")
    if stop_reason:
        out("停手：" + stop_reason)
    if failed:
        out("没跑通：%s —— 断言层还没到，别急着 tg done。" % "、".join(r["role"] for r in failed))
    if unaccounted:
        out("账没归上：%s —— 这些路真花了钱，但账本里没有，不算通过（先看日志再 tg sync）。"
            % "、".join(unaccounted))
    if not failed and not unaccounted:
        out("每一路都 exit 0，账也都点名归上了。但 exit 0 只是「它说自己干完了」，生死看门禁。")
    out("账本这一轮：$%.6f（下限，没归上的会话不在里面）" % total_usd)
    out("")
    out("接下来：")
    for x in nxt:
        out("  · " + x)
    return 0 if ok else 1


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


def claim_stat(rv):
    """人到底确认了哪几条 claim → (已确认, 总数)。没有逐条数据时总数为 None。

    「整份 review 翻了 true」是粗粒度的：13 条 claim 里确认 1 条和确认 13 条，
    顶层 human_verified 都是 false，但这两件事完全不是一回事 —— 所以逐条数出来。
    """
    hc = rv.get("human_confirmed_claims") or {}
    if isinstance(hc, dict) and hc.get("total"):
        return int(hc.get("confirmed") or 0), int(hc["total"])
    checks = rv.get("checks") or []
    claim_rows = [c for c in checks
                  if "jev 自己的汇总" not in (c.get("name") or "")]
    if not claim_rows:
        return 0, None
    return sum(1 for c in claim_rows if c.get("human_confirmed") is True), len(claim_rows)


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
    claim_conf = claim_tot = 0
    for r in reviewed:
        c, t = claim_stat(r["review"])
        r["claim_conf"], r["claim_tot"] = c, t
        if t:
            claim_conf += c
            claim_tot += t

    payload = {
        "api": ORCH_JSON_API, "cmd": "rollup", "ok": True,
        "plan_id": status["plan"]["id"], "gate_src": "rerun" if a.gate else "plan_status",
        "steps": rows,
        "conflicts": [{"n": n, "why": w} for n, w in conflicts],
        "conflict_check": ("rerun" if a.gate else "skipped_no_gate"),
        "reviewed": len(reviewed), "human_verified": len(human_ok),
        "human_claims": {"confirmed": claim_conf, "total": claim_tot or None},
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
               "" if rv.get("human_verified") is True else
               (("  （人确认 %d/%d 条 claim）" % (r.get("claim_conf", 0), r["claim_tot"]))
                if r.get("claim_tot") else "  （未人工抽查）")))

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
        out("  jev 双轨期：%d 次评价里有 %d 次全部 claim 经人抽查。"
            % (len(jev_rows), len(jev_spot)))
        if claim_tot:
            if claim_conf < claim_tot:
                out("  逐条看：人确认了 %d/%d 条 claim —— 部分确认**不算**验证通过，"
                    "顶层 human_verified 仍是 false。" % (claim_conf, claim_tot))
            else:
                # 反过来那一半也要说：全绿了就不许再照抄上面那句，
                # 否则人做的确认被一句话抹掉，读出来的还是「没人验过」。
                out("  逐条看：%d/%d 条 claim 全部经人确认（每一步的顶层 human_verified 都是 true）。"
                    % (claim_conf, claim_tot))
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
        description="多角色编排层：fan 只出简报（不派发），dispatch 才起会话（不评质量）")
    ap.add_argument("--version", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("role", help="登记/查看角色（角色 = 一个它独占的目录）")
    p.add_argument("action", choices=["add", "list"])
    p.add_argument("name", nargs="?", help="add 时的角色名")
    p.add_argument("--dir", help="这个角色独占的产物目录")
    p.add_argument("--task", help="这一路具体做什么（一句话：产出什么、给谁用）；fan 缺它就不派发")
    p.add_argument("--lead", action="store_true",
                   help="这个是大脑（唯一决策者）；全计划只能有一个，--force 也换不掉，要换走两步")
    p.add_argument("--no-lead", dest="no_lead", action="store_true",
                   help="撤销现任大脑（必须配 --force；换大脑的第一步）")
    p.add_argument("--merge", help="谁看、谁合、合不上怎么办（计划级答案，写一次就够）")
    p.add_argument("--reviewer", default="jev",
                   help="谁来评：%s（默认 jev —— 它是云上服务，代码会外传；"
                        "要零外传用 none，本地小模型用 local-4b）" % "/".join(REVIEWERS))
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

    p = sub.add_parser("dispatch", help="真派发：照简报起 N 个 headless 会话，自动点名归账")
    p.add_argument("step", type=int)
    p.add_argument("--roles", help="逗号分隔；默认全部已登记角色")
    p.add_argument("--max-fanout", type=int, default=None,
                   help="这次最多几路（默认取 .orch/roles.json 里的 %d）" % DEFAULT_MAX_FANOUT)
    p.add_argument("--concurrency", type=int, default=None,
                   help="同时跑几路（默认 = --max-fanout；4 路并发 2 就是分两批）")
    p.add_argument("--contract", help="契约文件路径；默认自动找已完成步骤里产出的 .md")
    p.add_argument("--model", default=None,
                   help="provider/model#variant；默认取 $ORCH_MODEL，再退 opencode 默认（当前是中转）")
    p.add_argument("--agent", default="general", help="用哪个 agent 起会话（默认 general）")
    p.add_argument("--retries", type=int, default=1,
                   help="某一路 exit!=0 时，在**原会话**里补跑几次（默认 1；0 = 不补）")
    p.add_argument("--timeout", type=int, default=2400, help="单路最多跑多少秒（默认 2400）")
    p.add_argument("--max-usd", type=float, default=None,
                   help="本轮累计花费上限（美元，账本口径）；超了就不再开下一批")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印会执行的命令，不起进程、不动账本")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("rollup", help="三层汇总：断言 / 语义 / 人（不许混着说）")
    p.add_argument("--step", type=int, default=None)
    p.add_argument("--gate", action="store_true",
                   help="真重跑每步断言（慢但是真数据）；不给就只读计划状态")
    p.add_argument("--cost", action="store_true", help="附上每步花了多少钱")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args()

    if getattr(a, "version", False) or not a.cmd:
        out("orch 0.3.0（tg --json api %d）  零依赖 · 不评质量 · dispatch 才起会话"
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

    if a.cmd == "dispatch":
        return cmd_dispatch(a)

    if a.cmd == "rollup":
        cmd_rollup(a)
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
