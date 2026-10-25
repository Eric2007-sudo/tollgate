#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg.py — tollgate 的命令行内核。纯标准库，零依赖，单文件。

tollgate 管的是 agent 干活时的两道闸：
  闸一「该不该发」  计划 + 轮数预算 + 确定性门禁   （内核继承自 plan-ledger v0.3）
  闸二「塞多少」    本轮上下文大小 / 花费 / 该压缩还是该换会话（本文件新增）

v0.2 新增（来源见 README 的博采众长表）：
  depends/ready/parallel  ← CCPM：任务依赖图 + 可并行分组 + 产物冲突检测
  handoff                 ← shadcn/improve：为「最弱的执行者」生成自足式简报
  stop 条件               ← shadcn/improve：出偏差时停并汇报，而不是即兴发挥
  git 提交戳 + drift 检查 ← shadcn/improve：计划基于哪个 commit，工作树走远了就报警
  reconcile               ← shadcn/improve：重跑已完成的门禁，发现回归
  轮次上限                ← LazyCodex $ulw-loop：同一步重试到上限就强制 block
  reuse 梯子              ← Ponytail：先问「这段代码真的需要存在吗」
  report                  ← rtk gain / ponytail band：成本与复用率报表

v0.3 新增（一次真实重构实测暴露的缺口）：
  amend                   ← 计划跑到一半发现断言/预算写错，v0.2 只能删了重建
  轮次口径                ← 实测 token 预算差 74 倍：--rounds 定预算，--reqs 记轮数，
                            --baseline-context 折算，report 分出「轮数超」还是「上下文胀」

tollgate 0.1 新增：
  ctx / price / serve     ← caveman（输出侧压缩）+ headroom（上下文压缩）
                            + codebase-memory-mcp（按需检索）+ claude-mem（本地只读看板）
                            先算「这一轮值多少钱」，再比较「压缩」和「换会话」哪个更省

tollgate 0.2 新增：
  sync                    两闸交界：把 session_message 里逐轮真实的 tokens/cost
                          自动写进账本，取代手填 spend。口径与 spend 完全一致
                          （in 只算新输入，out 含推理，cache_read 单列不入预算），
                          游标存在 .plan/sync.json，同一轮永远不会重复记账。

四条不变的设计约束：
  1. 零依赖      只用标准库，Windows / macOS / Linux 同一份代码
  2. 单一真相    .plan/plan.json 是唯一真源，plan.md 只是渲染视图
  3. 追加写      .plan/ledger.jsonl 与 .plan/ctx.jsonl 只 append 永不改写，崩溃可恢复
  4. 确定性门禁  「完成」由断言 exit code 决定，不由模型自述决定

向后兼容：目录仍是 .plan/，schema 仍是 2 —— plan-ledger 建的计划 tg 直接能读，反之亦然。
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLAN_DIR = ".plan"
SCHEMA = 2

TIERS = ("cheap", "std", "deep")

# Ponytail 的梯子：从「不需要存在」到「必须新写」，越靠前越省钱
REUSE_RUNGS = ("skip", "existing", "stdlib", "native", "dep", "line", "new")
REUSE_LABEL = {
    "skip": "1 不需要存在",
    "existing": "2 仓库里已有",
    "stdlib": "3 标准库能做",
    "native": "4 平台原生支持",
    "dep": "5 已装的依赖能做",
    "line": "6 一行写得出来",
    "new": "7 只能新写",
}


# ---------------------------------------------------------------- 基础设施

def out(msg=""):
    print(msg)


def die(msg, code=2):
    out("ERROR: " + msg)
    sys.exit(code)


def warn(msg):
    out("!! " + msg)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def sh(cmd, cwd, timeout=20):
    """跑一条命令，返回 (ok, stdout.strip())。失败不抛异常。"""
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(cwd), timeout=timeout,
                           capture_output=True)
    except Exception:
        return False, ""
    if r.returncode != 0:
        return False, ""
    return True, r.stdout.decode("utf-8", "replace").strip()


def find_root():
    """从 cwd 向上找 .plan/plan.json；找不到就用 cwd。"""
    here = Path.cwd().resolve()
    for cand in [here, *here.parents]:
        if (cand / PLAN_DIR / "plan.json").is_file():
            return cand
    return here


def paths(root):
    d = root / PLAN_DIR
    return {
        "dir": d,
        "json": d / "plan.json",
        "md": d / "plan.md",
        "ledger": d / "ledger.jsonl",
        "gates": d / "gates.log",
        "notes": d / "notes",
        "brief": d / "brief",
    }


def load(root):
    p = paths(root)
    if not p["json"].is_file():
        die('没有找到 .plan/plan.json。先运行：tg init "你的目标"')
    with open(p["json"], encoding="utf-8") as fh:
        plan = json.load(fh)
    # 兼容 v1 计划文件
    plan.setdefault("exclude", [])
    plan.setdefault("acks", [])
    plan.setdefault("max_rounds", 3)
    plan.setdefault("schema", 1)
    plan.setdefault("baseline_context", 0)   # 单次请求的典型上下文，用于轮次口径折算
    for s in plan.get("steps", []):
        s.setdefault("depends", [])
        s.setdefault("stop", [])
        s.setdefault("read", [])
        s.setdefault("accept", [])
        s.setdefault("reuse", "")
        s.setdefault("rounds", 0)            # 已开始过几轮（start 累加）
        s.setdefault("budget_rounds", 0)      # 这一步允许花几轮请求（0 = 未设）
        s.setdefault("note", "")
    return plan


def load_ledger(root):
    p = paths(root)
    if not p["ledger"].is_file():
        return []
    rows = []
    with open(p["ledger"], encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def append_ledger(root, rec):
    p = paths(root)
    p["dir"].mkdir(parents=True, exist_ok=True)
    with open(p["ledger"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def save(root, plan):
    p = paths(root)
    p["dir"].mkdir(parents=True, exist_ok=True)
    p["notes"].mkdir(exist_ok=True)
    plan["schema"] = SCHEMA
    with open(p["json"], "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=1)
    render(root, plan)


def step_by_n(plan, n):
    for s in plan["steps"]:
        if s["n"] == n:
            return s
    die("没有第 %s 步。现有: %s" % (n, ", ".join(str(s["n"]) for s in plan["steps"])))


def spent_for(ledger, n):
    return sum(r.get("in", 0) + r.get("out", 0) for r in ledger if r.get("step") == n)


def reqs_for(ledger, n=None):
    """某步（或全计划）记账里登记过的请求轮数。"""
    return sum(r.get("reqs", 0) for r in ledger
               if n is None or r.get("step") == n)


def fmt_k(n):
    if n >= 1000:
        v = n / 1000.0
        return ("%.1f" % v).rstrip("0").rstrip(".") + "k"
    return str(n)


def fmt_v(v):
    """把 amend 的新旧值渲染成人能读的一行。"""
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]"
    if v is None:
        return "-"
    return str(v)


def paths_overlap(a, b):
    """产物路径是否重叠（相等或一方是另一方的父路径）。"""
    if not a or not b:
        return False
    a = a.replace("\\", "/").rstrip("/")
    b = b.replace("\\", "/").rstrip("/")
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def git_head(root):
    ok, sha = sh("git rev-parse --short HEAD", root)
    return sha if ok else ""


def git_drift(root, base):
    """返回工作树相对 base 前进了多少个 commit；无法判断时返回 -1。"""
    if not base:
        return -1
    ok, n = sh("git rev-list --count %s..HEAD" % base, root)
    if not ok:
        return -1
    try:
        return int(n)
    except ValueError:
        return -1


# ---------------------------------------------------------------- 渲染

def render(root, plan):
    p = paths(root)
    ledger = load_ledger(root)
    total_budget = sum(s.get("budget", 0) for s in plan["steps"])
    done = sum(1 for s in plan["steps"] if s["status"] == "done")

    L = []
    L.append("# 计划 · %s" % plan["goal"])
    L.append("")
    if plan.get("why"):
        L.append("> 为什么它该存在：%s" % plan["why"])
    L.append("> id `%s` ｜ 步骤 %d（完成 %d）｜ 总预算 %s tok"
             % (plan["id"], len(plan["steps"]), done, fmt_k(total_budget)))
    if plan.get("baseline_context"):
        L.append("> 会话基线 %s tok/轮（轮次预算按它折算；只记 token 数看不出钱花在哪）"
                 % fmt_k(plan["baseline_context"]))
    if plan.get("git_head"):
        L.append("> 基线 commit `%s`（工作树走远后 gate 会提醒）" % plan["git_head"])
    if plan.get("exclude"):
        L.append("> 禁读：%s" % " ｜ ".join("`%s`" % e for e in plan["exclude"]))
    L.append("> 创建 %s ｜ 最近变更 %s" % (plan.get("created", "?"), plan.get("updated", "?")))
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 步骤总览")
    L.append("")
    L.append("| # | 标题 | 档位 | 预算 | 依赖 | 复用 | 产物 | 状态 | 实耗 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for s in plan["steps"]:
        used = spent_for(ledger, s["n"])
        mark = {"todo": "·", "doing": "→", "done": "OK", "blocked": "!!"}.get(s["status"], "?")
        deps = ",".join(str(d) for d in s["depends"]) or "-"
        reuse = s.get("reuse") or "-"
        L.append("| %d | %s | `%s` | %s | %s | %s | `%s` | %s | %s |"
                 % (s["n"], s["title"], s["tier"], fmt_k(s.get("budget", 0)), deps,
                    reuse, s.get("out", "-"), mark,
                    fmt_k(used) if used else "-"))
    L.append("")

    for s in plan["steps"]:
        L.append("### #%d %s  [%s]" % (s["n"], s["title"], s["status"]))
        L.append("")
        if s.get("depends"):
            L.append("- 依赖：%s" % "、".join("#%d" % d for d in s["depends"]))
        if s.get("reuse"):
            L.append("- 复用梯子：%s" % REUSE_LABEL.get(s["reuse"], s["reuse"]))
        if s.get("read"):
            L.append("- 计划内读取：%s" % "、".join("`%s`" % r for r in s["read"]))
        else:
            L.append("- 计划内读取：（未声明 → 先 grep 定位，再决定读不读）")
        L.append("- 产物：`%s`" % s.get("out", "-"))
        if s.get("budget_rounds"):
            L.append("- 请求轮数上限：%d 轮（超了就 block，别硬撑）" % s["budget_rounds"])
        if s.get("accept"):
            L.append("- 验收断言：")
            for a in s["accept"]:
                L.append("    - `%s`" % a)
        if s.get("stop"):
            L.append("- 停止条件（命中就停并汇报，别即兴发挥）：")
            for st in s["stop"]:
                L.append("    - %s" % st)
        if s.get("note"):
            L.append("- 备注：%s" % s["note"])
        L.append("")

    if plan.get("exclude"):
        L.append("---")
        L.append("")
        L.append("## 禁读清单")
        L.append("")
        L.append("以下路径**不要**整份读取，需要时用 `grep` / `rg` 定位片段：")
        L.append("")
        for e in plan["exclude"]:
            L.append("- `%s`" % e)
        L.append("")

    with open(p["md"], "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


# ---------------------------------------------------------------- 断言

def parse_pair(expr, kind):
    body = expr[len(kind):]
    if "@" not in body:
        die("断言格式错误：%s 应为 %s<pattern>@<path>" % (expr, kind))
    pattern, path = body.rsplit("@", 1)
    return pattern, path


def run_check(expr, root):
    """返回 (passed, detail)。任何未知语法一律判失败，不静默放行。"""
    expr = expr.strip()

    if expr.startswith("file:exists:"):
        target = root / expr[len("file:exists:"):].strip()
        return target.exists(), "exists -> %s" % target

    if expr.startswith("file:absent:"):
        target = root / expr[len("file:absent:"):].strip()
        return (not target.exists()), "absent -> %s" % target

    if expr.startswith("grep:"):
        pattern, path = parse_pair(expr, "grep:")
        target = root / path.strip()
        if not target.is_file():
            return False, "文件不存在: %s" % target
        text = target.read_text(encoding="utf-8", errors="replace")
        return bool(re.search(pattern, text)), "grep %r in %s" % (pattern, path)

    if expr.startswith("nogrep:"):
        pattern, path = parse_pair(expr, "nogrep:")
        target = root / path.strip()
        if not target.is_file():
            return False, "文件不存在: %s" % target
        text = target.read_text(encoding="utf-8", errors="replace")
        return (not re.search(pattern, text)), "nogrep %r in %s" % (pattern, path)

    if expr.startswith("cmd:"):
        cmd = expr[len("cmd:"):].strip()
        try:
            r = subprocess.run(cmd, shell=True, cwd=str(root), timeout=600,
                               capture_output=True)
        except subprocess.TimeoutExpired:
            return False, "cmd 超时: %s" % cmd
        ok = (r.returncode == 0)
        tail = (r.stdout + r.stderr).decode("utf-8", "replace").strip().splitlines()
        return ok, "exit=%d  %s" % (r.returncode, (tail[-1] if tail else ""))

    if expr.startswith("ack:"):
        return False, '需人工确认（tg ack "<该项原文>"）'

    return False, "无法识别的断言语法: %s" % expr


def run_gates(root, step, plan):
    acks = set(plan.get("acks", []))
    results = []
    for expr in step.get("accept", []):
        if expr.startswith("ack:") and expr in acks:
            results.append((expr, True, "人工已确认"))
            continue
        ok, detail = run_check(expr, root)
        results.append((expr, ok, detail))
    return results


def gate_report(root, step, plan, log=True):
    results = run_gates(root, step, plan)
    lines = ["[%s] gate #%d %s" % (now_iso(), step["n"], step["title"])]
    failed = 0
    for expr, ok, detail in results:
        lines.append("  %s  %s\n      %s" % ("PASS" if ok else "FAIL", expr, detail))
        if not ok:
            failed += 1
    if not results:
        lines.append("  (无断言 → 自动通过，但这步没有验证价值)")
    if log:
        p = paths(root)
        p["dir"].mkdir(parents=True, exist_ok=True)
        with open(p["gates"], "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return results, failed, "\n".join(lines)


# ---------------------------------------------------------------- 命令

def cmd_init(a):
    root = Path.cwd().resolve()
    p = paths(root)
    if p["json"].exists() and not a.force:
        die("已有计划（%s）。用 --force 覆盖，或直接 tg status" % p["json"])
    p["dir"].mkdir(parents=True, exist_ok=True)
    p["notes"].mkdir(exist_ok=True)
    if a.force:
        for f in (p["ledger"], p["gates"]):
            if f.exists():
                f.unlink()
    plan = {
        "schema": SCHEMA,
        "id": hashlib.sha1(a.goal.encode("utf-8")).hexdigest()[:6],
        "goal": a.goal,
        "why": a.why or "",
        "created": now_iso(),
        "updated": now_iso(),
        "git_head": git_head(root),
        "max_rounds": a.max_rounds,
        "baseline_context": a.baseline_context,
        "exclude": list(a.exclude or []),
        "acks": [],
        "steps": [],
    }
    save(root, plan)
    out("已建立计划 `%s` → %s" % (plan["id"], p["dir"]))
    if plan["git_head"]:
        out("基线 commit：`%s`" % plan["git_head"])
    if not plan["why"]:
        warn("没写 --why。计划起手先问一句：这个任务真的需要存在吗？"
             "（Ponytail 梯子第 1 级就是「不需要」）")
    out('下一步：tg add --title "..." --tier cheap --budget 8000 '
        '--out notes/01-x.md --reuse existing --accept "file:exists:notes/01-x.md"')


def cmd_add(a):
    root = find_root()
    plan = load(root)
    n = (max((s["n"] for s in plan["steps"]), default=0)) + 1
    if a.out is None:
        die("每一步都必须有产物路径 --out。纯分析类步骤写 notes/%02d-x.md" % n)
    if a.reuse not in REUSE_RUNGS:
        die("--reuse 必须是 %s 之一" % " / ".join(REUSE_RUNGS))

    deps = []
    for chunk in (a.depends or "").split(","):
        chunk = chunk.strip()
        if chunk:
            try:
                deps.append(int(chunk))
            except ValueError:
                die("--depends 用逗号分隔步骤号，例如 --depends 1,2")
    for d in deps:
        if d >= n or d < 1:
            die("--depends %d 不合法：只能依赖已存在的更早步骤" % d)

    step = {
        "n": n,
        "title": a.title,
        "tier": a.tier,
        "budget": a.budget,
        "out": a.out,
        "read": list(a.read or []),
        "reuse": a.reuse,
        "depends": deps,
        "stop": list(a.stop or []),
        "accept": list(a.accept or []),
        "status": "todo",
        "rounds": 0,
        "budget_rounds": a.rounds or 0,
        "note": a.note or "",
    }
    plan["steps"].append(step)
    plan["updated"] = now_iso()
    save(root, plan)
    out("已加 #%d %s（tier=%s，预算 %s，复用=%s）"
        % (n, a.title, a.tier, fmt_k(a.budget), a.reuse))
    base = plan.get("baseline_context") or 0
    if base and a.budget and a.budget < base:
        warn("这步预算 %s 还不到一次请求的上下文（%s）。绝对 token 预算在小步上"
             "几乎必然失真，改用 --rounds 按轮数定预算（实测偏差可达 70× 以上）"
             % (fmt_k(a.budget), fmt_k(base)))
    if not a.rounds:
        warn("没给 --rounds。建议写「这步最多允许几轮请求」——轮数比 token 数"
             "好估一个数量级，--rounds 3 就是「最多问三次」")
    if not step["accept"]:
        warn("没有验收断言，gate 会无条件通过。至少加一条 file:exists:")
    if a.reuse == "new" and not a.note:
        warn("复用梯子选到「只能新写」，却没有 --note 说明为什么前 6 级都不成立")
    if not step["stop"]:
        warn("没有 --stop 停止条件。小模型在「现实与计划不符」时会即兴发挥，"
             "写一条「如果 X 就停下来汇报」")


def cmd_exclude(a):
    root = find_root()
    plan = load(root)
    for e in a.patterns:
        if e not in plan["exclude"]:
            plan["exclude"].append(e)
    plan["updated"] = now_iso()
    save(root, plan)
    out("禁读清单：%s" % ("、".join(plan["exclude"]) or "（空）"))


def _deps_done(plan, step):
    for d in step.get("depends", []):
        try:
            ds = step_by_n(plan, d)
        except SystemExit:
            return False, "#%d 不存在" % d
        if ds["status"] != "done":
            return False, "#%d 还没完成" % d
    return True, ""


def cmd_ready(a):
    """列出可以立刻开始的步骤（依赖已满足）。"""
    root = find_root()
    plan = load(root)
    ready, waiting = [], []
    for s in plan["steps"]:
        if s["status"] in ("done", "blocked", "doing"):
            continue
        ok, why = _deps_done(plan, s)
        (ready if ok else waiting).append((s, why))
    if not ready:
        out("没有可执行的步骤。")
    for i, (s, _) in enumerate(ready):
        out("#%d %s  (tier=%s, 预算 %s, 复用=%s)"
            % (s["n"], s["title"], s["tier"], fmt_k(s["budget"]), s.get("reuse") or "-"))
    for s, why in waiting:
        out("… #%d %s  ← %s" % (s["n"], s["title"], why))
    if len(ready) > 1:
        out("")
        out("有 %d 步可并行，跑 tg parallel 看它们是否真的不冲突。" % len(ready))


def cmd_parallel(a):
    """按依赖 + 产物冲突，把剩余步骤切成可并行的批次。"""
    root = find_root()
    plan = load(root)
    remaining = [s for s in plan["steps"] if s["status"] not in ("done", "blocked")]
    if not remaining:
        out("没有剩余步骤。")
        return
    done = set(s["n"] for s in plan["steps"] if s["status"] == "done")
    batches = []
    placed = set(done)
    guard = 0
    while True:
        guard += 1
        if guard > 50:
            die("依赖成环，无法排序")
        batch = []
        for s in remaining:
            if s["n"] in placed:
                continue
            if any(d not in placed for d in s.get("depends", [])):
                continue
            clash = None
            for t in batch:
                if paths_overlap(s.get("out", ""), t.get("out", "")):
                    clash = t["n"]
                    break
            if clash is not None:
                continue
            batch.append(s)
        if not batch:
            rest = [s for s in remaining if s["n"] not in placed]
            if rest:
                out("无法继续排序（依赖成环或缺依赖）：%s"
                    % "、".join("#%d" % s["n"] for s in rest))
            break
        batches.append(batch)
        for s in batch:
            placed.add(s["n"])

    for i, batch in enumerate(batches, 1):
        head = "批次 %d（%d 个可同时跑）:" % (i, len(batch)) if len(batch) > 1 \
            else "批次 %d（单步，无可并行的同伴）:" % i
        out(head)
        for s in batch:
            out("  #%d %s  →  %s" % (s["n"], s["title"], s.get("out", "-")))
        for j in range(len(batch)):
            for k in range(j + 1, len(batch)):
                if paths_overlap(batch[j].get("out", ""), batch[k].get("out", "")):
                    out("  !! #%d 与 #%d 产物重叠，不能真并行" % (batch[j]["n"], batch[k]["n"]))
    out("")
    out("提示：并行只对「产物不重叠」的步骤安全。改同一批文件的步骤必须串行。")


def cmd_graph(a):
    root = find_root()
    plan = load(root)
    out("依赖图（→ 表示依赖）:")
    for s in plan["steps"]:
        if s.get("depends"):
            chain = " → ".join("#%d" % d for d in s["depends"])
            out("  %s → #%d %s %s" % (chain, s["n"], s["title"],
                                      "  [%s]" % s["status"]))
        else:
            out("  (根) → #%d %s  [%s]" % (s["n"], s["title"], s["status"]))


def cmd_start(a):
    root = find_root()
    plan = load(root)
    s = step_by_n(plan, a.n)
    if s["status"] == "done":
        die("#%d 已经是 done。真要重做，先 tg reset %d" % (a.n, a.n))

    for other in plan["steps"]:
        if other["status"] == "doing" and other["n"] != a.n:
            die("#%d 还在 doing。一次只做一步，先 tg done %d 或 tg block %d"
                % (other["n"], other["n"], other["n"]))

    ok, why = _deps_done(plan, s)
    if not ok and not a.force:
        die("#%d 的前置依赖没完成：%s。真要跳，加 --force" % (a.n, why))

    cap = plan.get("max_rounds", 3)
    if s.get("rounds", 0) >= cap:
        s["status"] = "blocked"
        s["note"] = "重试已达上限 %d 轮" % cap
        plan["updated"] = now_iso()
        save(root, plan)
        die("#%d 已重试 %d 轮（上限）。已标记 blocked。\n"
            "    换策略：拆步骤 / 换档位 / 补禁读清单 / 回官方模型跑一次。\n"
            "    确认真要继续：tg reset %d --rounds"
            % (s["n"], s.get("rounds", 0), s["n"]))

    s["status"] = "doing"
    s["rounds"] = s.get("rounds", 0) + 1
    plan["updated"] = now_iso()
    save(root, plan)
    out("#%d %s 开始（第 %d/%d 轮，tier=%s，预算 %s）"
        % (s["n"], s["title"], s["rounds"], cap, s["tier"], fmt_k(s["budget"])))
    if s.get("budget_rounds"):
        out("请求轮数上限：%d 轮（超了就 block，别硬撑）" % s["budget_rounds"])
    out("产物必须落在：%s" % s["out"])
    for st in s.get("stop", []):
        out("停止条件：%s" % st)
    out("完成前跑：tg gate %d" % s["n"])


def cmd_reset(a):
    root = find_root()
    plan = load(root)
    s = step_by_n(plan, a.n)
    s["status"] = "todo"
    s["note"] = ""
    if a.rounds:
        s["rounds"] = 0
    plan["updated"] = now_iso()
    save(root, plan)
    if a.rounds:
        out("#%d 已重置为 todo，轮次计数清零" % s["n"])
    else:
        out("#%d 已重置为 todo（轮次计数保留，当前 %d 轮；加 --rounds 可清零）"
            % (s["n"], s.get("rounds", 0)))


def cmd_block(a):
    root = find_root()
    plan = load(root)
    s = step_by_n(plan, a.n)
    s["status"] = "blocked"
    s["note"] = a.why
    plan["updated"] = now_iso()
    save(root, plan)
    out("#%d 标记为 blocked：%s" % (s["n"], a.why))


def cmd_amend(a):
    """改已有计划。

    实战里最常发生的事不是「写不出计划」，而是「计划跑到第 3 步才发现第 1 步的
    断言写错了 / 预算定低了」。v0.2 只能删了重建，v0.3 让改动留痕。
    """
    root = find_root()
    plan = load(root)

    if a.n == 0:
        changes = {}
        if a.goal is not None and a.goal != plan.get("goal"):
            changes["goal"] = [plan.get("goal", ""), a.goal]
            plan["goal"] = a.goal
        if a.why is not None and a.why != plan.get("why", ""):
            changes["why"] = [plan.get("why", ""), a.why]
            plan["why"] = a.why
        if a.max_rounds is not None and a.max_rounds != plan.get("max_rounds", 3):
            changes["max_rounds"] = [plan.get("max_rounds", 3), a.max_rounds]
            plan["max_rounds"] = a.max_rounds
        if a.baseline_context is not None \
                and a.baseline_context != plan.get("baseline_context", 0):
            changes["baseline_context"] = [plan.get("baseline_context", 0),
                                           a.baseline_context]
            plan["baseline_context"] = a.baseline_context
        added = []
        for e in (a.add_exclude or []):
            if e not in plan["exclude"]:
                plan["exclude"].append(e)
                added.append(e)
        if added:
            changes["exclude+"] = added
        if not changes:
            die("amend 0 没有任何改动。可用：--goal / --why / --max-rounds / "
                "--baseline-context / --add-exclude")
        plan["updated"] = now_iso()
        append_ledger(root, {"ts": now_iso(), "step": 0, "in": 0, "out": 0,
                             "amend": changes, "note": a.reason or ""})
        save(root, plan)
        out("计划级改动 %d 项：" % len(changes))
        for k, v in changes.items():
            if k == "exclude+":
                out("  +%s %s" % (k[:-1], "、".join(v)))
            else:
                out("  %s: %s → %s" % (k, fmt_v(v[0]), fmt_v(v[1])))
        return

    s = step_by_n(plan, a.n)
    was_done = s["status"] == "done"
    if was_done and not a.force:
        die("#%d 已经 done。改已完成步骤会动摇已有结论。\n"
            "    要么 tg reset %d 重做，要么加 --force（改动会记进 ledger）\n"
            "    断言漏写这类情况：amend %d --force --add-accept \"...\" 然后 tg reconcile"
            % (a.n, a.n, a.n))

    changes = {}

    def setv(key, new):
        if new is None:
            return
        old = s.get(key)
        if old == new:
            return
        changes[key] = [old, new]
        s[key] = new

    def listop(field, adds, rms, clear):
        cur = list(s.get(field, []))
        old = list(cur)
        if clear:
            cur = []
        for x in (rms or []):
            if x in cur:
                cur.remove(x)
            else:
                warn("--rm-%s 的 %r 不在 #%d 的 %s 里，忽略" % (field, x, a.n, field))
        for x in (adds or []):
            if x not in cur:
                cur.append(x)
        if cur != old:
            changes[field] = [old, cur]
            s[field] = cur

    setv("title", a.title)
    setv("tier", a.tier)
    setv("budget", a.budget)
    setv("budget_rounds", a.rounds)
    setv("out", a.out)
    setv("reuse", a.reuse)
    setv("note", a.note)
    listop("accept", a.add_accept, a.rm_accept, a.clear_accept)
    listop("stop", a.add_stop, a.rm_stop, a.clear_stop)
    listop("read", a.add_read, a.rm_read, a.clear_read)

    if a.depends is not None:
        deps = []
        for chunk in a.depends.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                d = int(chunk)
            except ValueError:
                die("--depends 用逗号分隔步骤号，例如 --depends 1,2")
            if d < 1 or d == a.n:
                die("--depends %d 不合法：只能依赖别的步骤" % d)
            if not any(x["n"] == d for x in plan["steps"]):
                die("--depends %d 不存在" % d)
            if d not in deps:
                deps.append(d)
        if s.get("depends") != deps:
            changes["depends"] = [s.get("depends"), deps]
            s["depends"] = deps
        ok, why = _deps_done(plan, s)
        if s["status"] == "done" and not ok:
            warn("#%d 已 done，但新依赖没完成（%s）。依赖顺序反了。" % (a.n, why))

    if not changes:
        die("amend %d 没有任何改动。" % a.n)

    plan["updated"] = now_iso()
    append_ledger(root, {"ts": now_iso(), "step": a.n, "in": 0, "out": 0,
                         "amend": changes, "forced": was_done,
                         "note": a.reason or ""})
    save(root, plan)
    out("#%d 已改 %d 项：%s" % (a.n, len(changes), "、".join(changes)))
    for k, v in changes.items():
        out("  %s: %s → %s" % (k, fmt_v(v[0]), fmt_v(v[1])))
    if was_done:
        warn("#%d 是 done 步骤，被 --force 改了。立刻 tg reconcile 重验全部已完成步骤。"
             % a.n)
    elif "accept" in changes:
        out("验收断言变了 → 现在跑 tg gate %d 看新断言过不过。" % a.n)
    if "budget" in changes and plan.get("baseline_context"):
        base = plan["baseline_context"]
        if changes["budget"][1] and changes["budget"][1] < base:
            warn("新预算 %s 仍低于一次请求的上下文（%s），token 口径依旧不可用。"
                 "改用 --rounds 按轮数定。" % (fmt_k(changes["budget"][1]), fmt_k(base)))


def cmd_spend(a):
    root = find_root()
    plan = load(root)
    s = step_by_n(plan, a.n)
    rec = {"ts": now_iso(), "step": a.n, "in": a.input, "out": a.output,
           "note": a.note or ""}
    if a.reqs:
        rec["reqs"] = a.reqs
    append_ledger(root, rec)
    ledger = load_ledger(root)
    used = spent_for(ledger, a.n)
    budget = s.get("budget", 0)
    pct = (used / budget * 100) if budget else 0
    out("#%d 累计 %s / %s（%.0f%%）" % (s["n"], fmt_k(used), fmt_k(budget), pct))
    reqs = reqs_for(ledger, a.n)
    br = s.get("budget_rounds", 0)
    if reqs:
        out("   轮次：%d 轮%s ｜ 单轮平均 %s"
            % (reqs, ("（预算 %d 轮）" % br) if br else "",
               fmt_k(int(used / reqs))))
        if br and reqs > br:
            warn("已用 %d 轮，超过本步预算 %d 轮。继续之前先想：是任务本身要这么多轮，"
                 "还是每轮都在重新读同一批文件？" % (reqs, br))
    if not a.reqs:
        warn("这笔记账没带 --reqs。不知道轮数就无法判断超支是「轮数多」还是"
             "「单轮上下文大」，只能看到总量。下次记 --reqs <轮数>")
    if budget and used > budget:
        warn("超预算 %s tok。" % fmt_k(used - budget))
    render(root, plan)


def cmd_gate(a):
    root = find_root()
    plan = load(root)
    if a.n is not None:
        s = step_by_n(plan, a.n)
    else:
        s = next((x for x in plan["steps"] if x["status"] == "doing"), None)
        if s is None:
            todos = [x for x in plan["steps"] if x["status"] == "todo"]
            if not todos:
                out("没有 doing 的步骤。全部 done 或未开始。")
                sys.exit(0)
            s = todos[0]

    results, failed, text = gate_report(root, s, plan)
    out(text)

    drift = git_drift(root, plan.get("git_head"))
    if drift > 0:
        warn("工作树已比基线 commit `%s` 前进 %d 个 commit。"
             "如果计划写在旧代码上，先核对这一步的前提还成立吗。" % (plan["git_head"], drift))

    if failed:
        out("")
        out("gate 失败 %d 条。修完再跑，不要 tg done。" % failed)
        sys.exit(1)
    out("")
    out("gate 通过 %d 条。" % len(results))


def cmd_done(a):
    root = find_root()
    plan = load(root)
    s = step_by_n(plan, a.n)
    results, failed_n, text = gate_report(root, s, plan)
    failed = [(e, d) for (e, ok, d) in results if not ok]
    if failed and not a.force:
        out("拒绝标记完成：gate 未通过")
        for e, d in failed:
            out("  FAIL  %s   (%s)" % (e, d))
        out("")
        out("要么修到通过，要么显式 tg done %d --force（会记进 ledger）" % a.n)
        sys.exit(1)
    s["status"] = "done"
    plan["updated"] = now_iso()
    append_ledger(root, {"ts": now_iso(), "step": a.n, "in": 0, "out": 0,
                         "note": "DONE" + (" (forced)" if failed else "")})
    save(root, plan)
    ledger = load_ledger(root)
    used = spent_for(ledger, a.n)
    out("#%d %s 完成。实耗 %s / 预算 %s"
        % (s["n"], s["title"], fmt_k(used), fmt_k(s.get("budget", 0))))
    if opencode_db() is not None and cost_for(ledger, a.n)[0] is None:
        warn("这一步账本里没有真实用量。宿主就在本机——先 tg sync 再 done，"
             "否则「实耗」只是手记的 token 数，「到底花了多少钱」没人知道。")

    nxt = next((x for x in plan["steps"]
                if x["status"] != "done" and _deps_done(plan, x)[0]), None)
    if nxt:
        out("下一步：#%d %s（tier=%s）" % (nxt["n"], nxt["title"], nxt["tier"]))
    else:
        rest = [x for x in plan["steps"] if x["status"] != "done"]
        if rest:
            out("没有可立刻开始的步骤（后面 %d 步仍在等依赖）。跑 tg ready 看。"
                % len(rest))
        else:
            out("全部步骤完成。跑 tg reconcile 做收尾核验。")


def cmd_ack(a):
    root = find_root()
    plan = load(root)
    expr = a.expr.strip()
    if not expr.startswith("ack:"):
        expr = "ack:" + expr
    if expr not in plan.get("acks", []):
        plan.setdefault("acks", []).append(expr)
    plan["updated"] = now_iso()
    save(root, plan)
    out("已确认：%s" % expr)


def cmd_status(a):
    root = find_root()
    plan = load(root)
    ledger = load_ledger(root)
    steps = plan["steps"]
    total = len(steps)
    done = sum(1 for s in steps if s["status"] == "done")
    budget = sum(s.get("budget", 0) for s in steps)
    spent = sum(r.get("in", 0) + r.get("out", 0) for r in ledger)
    pct = int(spent / budget * 100) if budget else 0
    blocked = [s for s in steps if s["status"] == "blocked"]
    cur = next((s for s in steps if s["status"] == "doing"), None) \
        or next((s for s in steps
                 if s["status"] == "todo" and _deps_done(plan, s)[0]), None)

    goal = plan["goal"]
    if len(goal) > 46:
        goal = goal[:44] + "…"

    out("[PLAN %s] 目标: %s" % (plan["id"], goal))
    out("进度: %d/%d · 预算 %s/%s (%d%%) · 卡点 %d"
        % (done, total, fmt_k(spent), fmt_k(budget), pct, len(blocked)))
    if cur:
        out("当前: #%d %s (tier=%s, 预算 %s)"
            % (cur["n"], cur["title"], cur["tier"], fmt_k(cur.get("budget", 0))))
        out("产物: %s" % cur.get("out", "-"))
        for st in cur.get("stop", [])[:2]:
            out("停止条件: %s" % st)
    else:
        out("当前: 无（无待办或都被依赖卡住）")
    out("禁读: %s" % ("、".join(plan["exclude"]) if plan.get("exclude") else "（未设置 → 建议补）"))
    out("提示: tg show 看全文 · tg handoff N 发给便宜模型 · 完成必须 gate 通过")
    if blocked:
        out("")
        for s in blocked:
            out("!! 卡住 #%d %s — %s" % (s["n"], s["title"], s.get("note", "")))


def cmd_show(a):
    root = find_root()
    p = paths(root)
    if not p["md"].is_file():
        render(root, load(root))
    out(p["md"].read_text(encoding="utf-8"))


def cmd_resume(a):
    root = find_root()
    plan = load(root)
    out("== 恢复上下文 ==")
    out("目标: %s" % plan["goal"])
    done = [s for s in plan["steps"] if s["status"] == "done"]
    if done:
        out("已完成:")
        for s in done[-4:]:
            out("  #%d %s → %s" % (s["n"], s["title"], s.get("out", "-")))
        if len(done) > 4:
            out("  ...（更早 %d 步见 tg show）" % (len(done) - 4))
    doing = next((s for s in plan["steps"] if s["status"] == "doing"), None)
    nxt = doing or next((s for s in plan["steps"]
                         if s["status"] == "todo" and _deps_done(plan, s)[0]), None)
    if nxt:
        out("继续: #%d %s (tier=%s, 预算 %s, 第 %d 轮)"
            % (nxt["n"], nxt["title"], nxt["tier"],
               fmt_k(nxt.get("budget", 0)), nxt.get("rounds", 1)))
        out("  产物: %s" % nxt.get("out", "-"))
        for x in nxt.get("accept", []):
            out("  验收: %s" % x)
        for st in nxt.get("stop", []):
            out("  停止: %s" % st)
    for s in plan["steps"]:
        if s["status"] == "blocked":
            out("卡点: #%d %s — %s" % (s["n"], s["title"], s.get("note", "")))
    if plan.get("exclude"):
        out("禁读: %s" % "、".join(plan["exclude"]))
    if nxt:
        out("先跑 tg gate %d 确认当前状态，再动手。" % nxt["n"])
    else:
        out("全部步骤已完成。跑 tg reconcile 做收尾核验。")


def cmd_handoff(a):
    """给「最弱的执行者」写一份自足式简报 —— 强弱模型分工的落地件。"""
    root = find_root()
    plan = load(root)
    s = step_by_n(plan, a.n)
    p = paths(root)
    p["brief"].mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", s["title"]).strip("-")[:40] or "step"
    fname = "%02d-%s.md" % (s["n"], slug)
    target = p["brief"] / fname

    deps = [step_by_n(plan, d) for d in s.get("depends", [])]
    others = [x for x in plan["steps"] if x["n"] != s["n"]]

    L = []
    L.append("# 任务简报：%s" % s["title"])
    L.append("")
    L.append("> 这份简报是**自足**的。你不需要看对话历史，也不需要猜上下文。")
    L.append("> 按步骤做，按验收自检，遇到停止条件就停下汇报。")
    L.append("")
    L.append("## 大目标")
    L.append("")
    L.append(plan["goal"])
    if plan.get("why"):
        L.append("")
        L.append("为什么它该存在：%s" % plan["why"])
    L.append("")
    L.append("## 你这一步")
    L.append("")
    L.append("- 步骤号：#%d（共 %d 步，你是其中之一）" % (s["n"], len(plan["steps"])))
    L.append("- 复杂度档位：`%s`（cheap=机械活 / std=常规实现 / deep=需要判断）" % s["tier"])
    L.append("- token 预算：%s（%d tok）" % (fmt_k(s["budget"]), s["budget"]))
    if s.get("reuse"):
        L.append("- 复用梯子：%s —— 先确认前 6 级都不成立再写新代码"
                 % REUSE_LABEL.get(s["reuse"], s["reuse"]))
    L.append("")

    if deps:
        L.append("## 前置产出（已存在，直接用）")
        L.append("")
        for d in deps:
            L.append("- `%s` — #%d %s" % (d.get("out", "-"), d["n"], d["title"]))
        L.append("")
        L.append("**先读这些文件**，它们已经包含了这一步需要的事实。不要再自己去翻一遍。")
        L.append("")

    L.append("## 允许读的文件")
    L.append("")
    if s.get("read"):
        for r in s["read"]:
            L.append("- `%s`" % r)
        L.append("")
        L.append("**只读上面这些。** 需要看别的位置时，先 `grep` 拿到行号区间，只读那一段。")
    else:
        L.append("（未指定）先 `grep` / `rg` 定位符号，拿到行号区间，只读那一段。")
        L.append("**不要整份读目录里的文件。**")
    L.append("")
    if plan.get("exclude"):
        L.append("## 不要读")
        L.append("")
        for e in plan["exclude"]:
            L.append("- `%s`" % e)
        L.append("")

    L.append("## 产物")
    L.append("")
    L.append("`%s`" % s.get("out", "-"))
    L.append("")
    L.append("做完这一步，这个文件必须存在且内容完整。**没有落盘等于没做。**")
    L.append("")

    L.append("## 验收（机器判定，不是自我感觉）")
    L.append("")
    if s.get("accept"):
        L.append("在项目根目录执行：")
        L.append("")
        L.append("```bash")
        L.append('tg gate %d' % s["n"])
        L.append("```")
        L.append("")
        L.append("它等价于逐条检查下面这些断言，全部通过才算完成：")
        L.append("")
        for ac in s["accept"]:
            if ac.startswith("cmd:"):
                L.append("- `%s` → 期望 **exit code 0**" % ac[len("cmd:"):])
            elif ac.startswith("file:exists:"):
                L.append("- 文件存在：`%s`" % ac[len("file:exists:"):])
            elif ac.startswith("file:absent:"):
                L.append("- 文件已删除：`%s`" % ac[len("file:absent:"):])
            elif ac.startswith("grep:"):
                pat, path = ac[len("grep:"):].rsplit("@", 1)
                L.append("- `%s` 里能匹配到 `%s`" % (path, pat))
            elif ac.startswith("nogrep:"):
                pat, path = ac[len("nogrep:"):].rsplit("@", 1)
                L.append("- `%s` 里**不能**再匹配到 `%s`" % (path, pat))
            else:
                L.append("- `%s`" % ac)
    else:
        L.append("（这一步没有断言 —— 只要求产物文件存在。自己再确认一遍内容是否正确。）")
    L.append("")

    L.append("## 停止条件（命中就停，不要即兴发挥）")
    L.append("")
    if s.get("stop"):
        for st in s["stop"]:
            L.append("- %s" % st)
    else:
        L.append("- 需要改动的文件不在上面「允许读」的列表里")
        L.append("- 验收命令报的错和这一步的目标无关")
        L.append("- 你发现计划的前提已经不成立了（代码和描述对不上）")
    L.append("")
    L.append("**停下时**：跑 `tg block %d --why \"具体卡在哪\"`，然后汇报，不要硬猜。" % s["n"])
    L.append("")

    L.append("## 明确不在范围内")
    L.append("")
    other_outs = [x.get("out") for x in others if x.get("out")]
    if other_outs:
        L.append("下面这些文件属于**别的步骤**，这一轮不要动：")
        L.append("")
        for o in other_outs[:12]:
            L.append("- `%s`" % o)
        L.append("")
    L.append("不要顺手重构、顺手加测试、顺手改文档。范围外的东西留着，会有对应步骤处理。")
    L.append("")

    drift = git_drift(root, plan.get("git_head"))
    L.append("## 前提核对")
    L.append("")
    if plan.get("git_head"):
        L.append("- 计划基于 commit `%s`" % plan["git_head"])
        if drift > 0:
            L.append("- 注意：工作树已经比基线前进 %d 个 commit，先确认下面说的代码还在原处" % drift)
    L.append("- 动手前先确认：这一步要改的文件确实还是描述里的样子")
    L.append("")
    L.append("---")
    L.append("")
    L.append("完成后执行 `tg done %d`。它会在门禁不过时**拒绝**标记完成。" % s["n"])

    text = "\n".join(L) + "\n"
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
    out("简报已写入：%s（%d 字符，约 %d token）"
        % (target, len(text), len(text) // 2))
    out("")
    out("用法：把这份文件原样交给便宜的执行者（或新开的会话），它自带全部上下文。")
    if a.print_it:
        out("")
        out(text)


def cmd_reconcile(a):
    """重跑所有已完成步骤的门禁，发现「曾经通过、现在挂了」的回归。"""
    root = find_root()
    plan = load(root)
    done = [s for s in plan["steps"] if s["status"] == "done"]
    if not done:
        out("没有已完成步骤可核验。")
    broke = []
    for s in done:
        results, failed_n, _ = gate_report(root, s, plan, log=False)
        if failed_n:
            broke.append((s, [r for r in results if not r[1]]))
    if done:
        out("核验了 %d 个已完成步骤：" % len(done))
        if not broke:
            out("  全部仍然通过。")
        for s, fails in broke:
            out("  !! #%d %s 现在不通过了：" % (s["n"], s["title"]))
            for expr, _ok, detail in fails[:3]:
                out("       %s   (%s)" % (expr, detail[:90]))
    blocked = [s for s in plan["steps"] if s["status"] == "blocked"]
    if blocked:
        out("")
        out("仍卡住 %d 步：" % len(blocked))
        for s in blocked:
            out("  #%d %s — %s" % (s["n"], s["title"], s.get("note", "")))
    drift = git_drift(root, plan.get("git_head"))
    if drift > 0:
        out("")
        warn("工作树已比基线前进 %d 个 commit。" % drift)
    if not done and not blocked:
        return
    if broke:
        out("")
        out("回归步骤用 tg reset N 退回 todo 重做。")


def cmd_report(a):
    root = find_root()
    plan = load(root)
    ledger = load_ledger(root)
    steps = plan["steps"]
    if not steps:
        out("还没有步骤。")
        return

    total_budget = sum(s.get("budget", 0) for s in steps)
    total_spent = sum(r.get("in", 0) + r.get("out", 0) for r in ledger)

    out("== 成本 ==")
    out("  预算 %s ｜ 实耗 %s（%d%%）"
        % (fmt_k(total_budget), fmt_k(total_spent),
           int(total_spent / total_budget * 100) if total_budget else 0))
    by_tier = {}
    for s in steps:
        used = spent_for(ledger, s["n"])
        t = by_tier.setdefault(s["tier"], {"n": 0, "budget": 0, "spent": 0})
        t["n"] += 1
        t["budget"] += s.get("budget", 0)
        t["spent"] += used
    for tier in TIERS:
        if tier not in by_tier:
            continue
        t = by_tier[tier]
        share = int(t["spent"] / total_spent * 100) if total_spent else 0
        out("  %-6s %d 步 ｜ 预算 %-7s ｜ 实耗 %-7s ｜ 占总消耗 %d%%"
            % (tier, t["n"], fmt_k(t["budget"]), fmt_k(t["spent"]), share))

    out("")
    out("== 轮次口径 ==")
    base = plan.get("baseline_context") or 0
    total_reqs = reqs_for(ledger)
    total_budget_rounds = sum(s.get("budget_rounds", 0) for s in steps)
    if not total_reqs:
        out("  一条带 --reqs 的记账都没有。")
        out("  只记 token 数看不出钱花在哪：同样的实耗，可能是「问了很多轮」，")
        out("  也可能是「每轮都在重读同一批文件」。下次记 spend N --reqs <轮数>。")
    else:
        out("  记账 %d 轮 ｜ 预算 %s 轮 ｜ 单轮平均上下文 %s"
            % (total_reqs,
               str(total_budget_rounds) if total_budget_rounds else "未设",
               fmt_k(int(total_spent / total_reqs))))
        if base:
            out("  baseline %s/轮 → 单轮是它的 %.1f 倍"
                % (fmt_k(base), (total_spent / total_reqs) / base))
            if total_budget_rounds:
                proj = base * total_budget_rounds
                out("  按轮数折算预期 %s（baseline × %d 轮），实际 %s（%.0f 倍）"
                    % (fmt_k(proj), total_budget_rounds, fmt_k(total_spent),
                       (total_spent / proj) if proj else 0))
        else:
            out("  没设 baseline。跑 tg amend 0 --baseline-context <一次请求大概多少 token>，")
            out("  才能把「轮数预算」折算成钱。")
        rows = []
        for s in steps:
            r = reqs_for(ledger, s["n"])
            if not r:
                continue
            rows.append((s, r, spent_for(ledger, s["n"])))
        for s, r, used in rows:
            br = s.get("budget_rounds", 0)
            c, _ = cost_for(ledger, s["n"])
            out("  #%d %d 轮%s ｜ 单轮 %s ｜ 实耗 %s%s"
                % (s["n"], r, ("/%d" % br) if br else "",
                   fmt_k(int(used / r)), fmt_k(used),
                   (" ｜ %s" % money(c)) if c is not None else ""))

    out("")
    out("== 钱（账本里的真实 cost）==")
    tot, cnt = cost_for(ledger)
    if tot is None:
        out("  账本里没有 cost。跑 tg sync 让宿主自己报账；")
        out("  手记 spend 时是记不到钱的（它只有 token 数），别把 token 数当钱。")
    else:
        est = [r for r in ledger if r.get("cost_est") and r.get("cost")]
        est_tot = sum(float(r["cost"]) for r in est)
        rounds = sum(r.get("reqs", 0) for r in ledger if r.get("cost"))
        out("  %s ｜ %d 笔记账 / %d 轮" % (money(tot), cnt, rounds))
        if rounds:
            out("  单轮 %s" % money(tot / rounds))
        if est:
            out("  其中 %s 来自 %d 笔按本地单价重算（宿主没写 cost，真实值只会更低）"
                % (money(est_tot), len(est)))
        if not est:
            out("  全部来自宿主记录，不是估算。")

    out("")
    out("== 预算准确性 ==")
    over = []
    for s in steps:
        used = spent_for(ledger, s["n"])
        b = s.get("budget", 0)
        if not b or not used:
            continue
        ratio = used / b
        if ratio > 1.5:
            over.append((s, ratio, used))
        elif ratio < 0.3 and s["status"] == "done":
            over.append((s, ratio, used))
    if over:
        for s, ratio, used in over:
            tag = "超支" if ratio > 1.5 else "用了不到三成"
            out("  #%d %s — %s（%d%%）" % (s["n"], s["title"], tag, int(ratio * 100)))
            r = reqs_for(ledger, s["n"])
            if not r:
                out("      未记轮数 → 分不清是轮数多还是单轮上下文大，先补 --reqs")
                continue
            per = used / r
            br = s.get("budget_rounds", 0)
            if base and per > base * 1.5:
                out("      %d 轮 × 单轮 %s ≈ baseline 的 %.1f 倍 → 上下文失控："
                    "补禁读清单 / 砍 --read / 先压缩再问"
                    % (r, fmt_k(int(per)), per / base))
            elif br and r > br:
                out("      %d 轮 > 预算 %d 轮 → 轮数失控：拆步骤 / tg handoff 给便宜模型"
                    % (r, br))
            else:
                out("      %d 轮 × 单轮 %s → 单轮上下文是主因，不是轮数"
                    % (r, fmt_k(int(per))))
        out("  先砍轮数，再砍单轮上下文。压缩输入只值一点钱，少跑几轮省的是整张账单。")
    else:
        out("  没有明显失准的步骤。")

    out("")
    out("== 复用率（Ponytail 梯子）==")
    rungs = {}
    for s in steps:
        rungs[s.get("reuse") or "?"] = rungs.get(s.get("reuse") or "?", 0) + 1
    for r in REUSE_RUNGS + ("?",):
        if r in rungs:
            label = REUSE_LABEL.get(r, "未标注")
            out("  %-9s %d 步  %s" % (r, rungs[r], label))
    new_n = rungs.get("new", 0)
    rated = len(steps) - rungs.get("?", 0)
    if rated:
        out("  复用率 %d%%（%d/%d 步没有新写代码）"
            % (int((rated - new_n) / rated * 100), rated - new_n, rated))
        if new_n and new_n * 2 > rated:
            warn("过半步骤都在「新写」。先重跑一遍梯子：现有的东西真的不能用？")

    out("")
    out("== 状态 ==")
    for st in ("done", "doing", "todo", "blocked"):
        n = sum(1 for s in steps if s["status"] == st)
        if n:
            out("  %-8s %d" % (st, n))


def cmd_drift(a):
    root = find_root()
    plan = load(root)
    ledger = load_ledger(root)
    problems = []
    for s in plan["steps"]:
        used = spent_for(ledger, s["n"])
        if s["status"] == "done" and used == 0:
            problems.append("#%d 标了 done 但没有 spend 记账（不打算记账可忽略）" % s["n"])
        if s["status"] == "done" and not s.get("accept"):
            problems.append("#%d 标了 done 但没有验收断言（无法验证）" % s["n"])
        b = s.get("budget", 0)
        if b and used > b * 1.5:
            problems.append("#%d 实耗超预算 50%%+（预算 %s 定低了，或该拆步骤）"
                            % (s["n"], fmt_k(b)))
        out_path = s.get("out")
        if s["status"] == "done" and out_path and not (root / out_path).exists():
            problems.append("#%d 的产物 %s 不存在（工作没落盘）" % (s["n"], out_path))
        ok, why = _deps_done(plan, s)
        if s["status"] == "done" and not ok:
            problems.append("#%d 已完成但它依赖的 %s（依赖顺序反了）" % (s["n"], why))
    if not plan.get("exclude"):
        problems.append("禁读清单为空（上下文最容易在这里失控）")
    if len(plan["steps"]) > 8:
        problems.append("步骤数 %d > 8，建议拆成两个 plan" % len(plan["steps"]))
    if not plan.get("why"):
        problems.append("没写 why（这个任务该不该存在，没人回答过）")
    unrated = [s for s in plan["steps"] if not s.get("reuse")]
    if unrated and len(unrated) == len(plan["steps"]):
        problems.append("所有步骤都没标复用梯子（默认就是「都新写」）")
    spent_steps = [s for s in plan["steps"] if spent_for(ledger, s["n"])]
    if spent_steps and not plan.get("baseline_context"):
        problems.append("已经有 token 记账但没设 baseline_context，"
                        "折算不出「单轮上下文」这个真正决定账单的量"
                        "（amend 0 --baseline-context N）")
    norq = [s["n"] for s in spent_steps if not reqs_for(ledger, s["n"])]
    if norq:
        problems.append("这些步骤有记账但没有轮数，超支时无法判断是轮数多还是单轮上下文大："
                        + "、".join("#%d" % x for x in norq))
    drift = git_drift(root, plan.get("git_head"))
    if drift > 5:
        problems.append("工作树已比基线 commit 前进 %d 个，计划可能已经过期" % drift)

    if not problems:
        out("没有发现 drift。")
        return
    out("发现 %d 处不一致：" % len(problems))
    for x in problems:
        out("  - " + x)


# ---------------------------------------------------------------- 价格与账本
#
# tollgate 相对 plan-ledger 的增量：把「发一轮请求要花多少钱、上下文里有多少是
# 可复用的稳定前缀」变成可查询、可记账、可看板的东西。
#   caveman            输出侧压缩：省的是输出单价，2~4:1 实测区间
#   headroom           上下文压缩：省的是未命中输入
#   codebase-memory-mcp 按需检索：省的是「一次性大块」
#   claude-mem/work-view  本地只读看板：让数字可看
# 计费口径与 OpenCode 一致：推理 token 按输出价（实测逐位对账过）。

CNY_PER_USD = 6.6892      # 2026-10-25 open.er-api.com；只用于把人话换算，不参与计费
CTX_WARN_RATIO = 0.5      # 单轮上下文占窗口过半：该 handoff / 换会话
CACHE_HEALTH = 0.30       # 缓存命中率低于 30%：稳定前缀在漂移
COMPRESS_RATIO = 3.0      # 压缩比假设，取 caveman/headroom 实测 2~4:1 的下沿
BRIEF_TOKENS = 600        # handoff 简报的典型大小（实测 629）


def opencode_config():
    """找 OpenCode 配置文件（json / jsonc）。找不到返回 None。"""
    cands = []
    for env in ("TOLLGATE_CONFIG", "OPENCODE_CONFIG"):
        val = os.environ.get(env, "").strip()
        if val:
            cands.append(Path(val))
    home = Path.home()
    cands.append(home / ".config" / "opencode" / "opencode.json")
    cands.append(home / ".config" / "opencode" / "opencode.jsonc")
    for c in cands:
        if c.is_file():
            return c
    return None


def load_prices():
    """收集 opencode.json 里各 provider/model 的 cost。

    返回 ({(provider, model): price}, 配置路径)。price 单位 USD / 百万 token，
    结构 {input, output, cache_read, cache_write, context}。
    官方 provider 没在本地配置里写 cost（价目在 models.dev 目录里），这里查不到
    就是查不到——如实报「无价目」，不猜。
    """
    prices = {}
    cfg_path = opencode_config()
    if cfg_path is None:
        return prices, None
    try:
        text = cfg_path.read_text(encoding="utf-8-sig")
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        cfg = json.loads(text)
    except Exception:
        return prices, cfg_path
    for pid, prov in (cfg.get("providers") or {}).items():
        for mid, model in ((prov or {}).get("models") or {}).items():
            c = (model or {}).get("cost")
            if not isinstance(c, dict):
                continue
            cache = c.get("cache") or {}
            prices[(pid, mid)] = {
                "input": float(c.get("input") or 0.0),
                "output": float(c.get("output") or 0.0),
                "cache_read": float(cache.get("read") or 0.0),
                "cache_write": float(cache.get("write") or 0.0),
                "context": int(((model.get("limit") or {}).get("context")) or 0),
            }
    return prices, cfg_path


def opencode_db():
    """宿主用量库。TOLLGATE_DB 可指向任意同构库（测试用）。"""
    env = os.environ.get("TOLLGATE_DB", "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    return db if db.is_file() else None


def read_sessions():
    """读 opencode.db 的 session_v2 聚合行。用只读 URI，避免和正在跑的会话抢锁。"""
    db = opencode_db()
    if db is None:
        return []
    uri = "file:" + str(db).replace("\\", "/") + "?mode=ro"
    rows = []
    try:
        con = sqlite3.connect(uri, uri=True, timeout=10)
    except Exception:
        return []
    try:
        rows = con.execute(
            "select id, model, cost, tokens_input, tokens_output, tokens_reasoning,"
            " tokens_cache_read, tokens_cache_write, time_updated"
            " from session_v2").fetchall()
    except Exception:
        rows = []
    finally:
        con.close()
    result = []
    for sid, model_raw, cost, tin, tout, trea, tcr, tcw, tupd in rows:
        pid = mid = ""
        try:
            m = json.loads(model_raw or "{}")
            pid = m.get("providerID") or ""
            mid = m.get("id") or ""
        except Exception:
            pass
        result.append({
            "session": sid, "provider": pid, "model": mid,
            "cost": float(cost or 0.0),
            "input": int(tin or 0), "output": int(tout or 0),
            "reasoning": int(trea or 0),
            "cache_read": int(tcr or 0), "cache_write": int(tcw or 0),
            "time_updated": int(tupd or 0),
        })
    return result


def current_session(sid=None):
    """挑会话：显式指定 > $OPENCODE_SESSION_ID > 最近更新的那条。"""
    rows = read_sessions()
    if not rows:
        return None
    want = (sid or os.environ.get("OPENCODE_SESSION_ID", "")).strip()
    if want:
        for r in rows:
            if r["session"] == want or r["session"].startswith(want):
                return r
    return max(rows, key=lambda r: r["time_updated"])


def billed_of(row):
    return row["input"] + row["cache_read"]


def cost_usd(tin, tout, trea, tcr, tcw, price):
    """按 OpenCode 的口径算钱：推理 token 走输出价，缓存读单列。"""
    if not price:
        return None
    return (tin * price["input"] + (tout + trea) * price["output"]
            + tcr * price["cache_read"] + tcw * price["cache_write"]) / 1e6


def money(usd):
    if usd is None:
        return "无价目"
    return "$%.6f（¥%.4f）" % (usd, usd * CNY_PER_USD)


def ctx_path(root):
    return paths(root)["dir"] / "ctx.jsonl"


def last_sample(root):
    p = ctx_path(root)
    if not p.is_file():
        return None
    last = None
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                pass
    return last


def append_sample(root, rec):
    p = ctx_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def plan_maybe(root):
    """ctx / serve 不要求已经有计划：没有就返回一个空壳。"""
    if paths(root)["json"].is_file():
        return load(root)
    return {"goal": "", "why": "", "steps": [], "exclude": [], "acks": [],
            "baseline_context": 0, "max_rounds": 0, "schema": SCHEMA,
            "git_head": "", "created": "", "owner": ""}


def _sample_record(row):
    return {
        "ts": now_iso(), "session": row["session"],
        "provider": row["provider"], "model": row["model"],
        "input": row["input"], "output": row["output"],
        "reasoning": row["reasoning"], "cache_read": row["cache_read"],
        "cache_write": row["cache_write"], "cost": row["cost"],
    }


# ---- 逐轮数据：session_message / message 的 data JSON 里存着每轮的 tokens ----
#
# 这是整个 ctx 的数据底座。session_v2 只有会话累计数，但每条 assistant 消息
# 的 data 里都有这一轮的 tokens（input / output / reasoning / cache.read /
# cache.write）和真实 cost。有它就不必靠采样差值猜单轮口径——直接读最后一轮。


def _rows_for_session(con, table, sid, limit):
    try:
        return con.execute(
            'select id, time_created, data from "%s" where session_id=? '
            "order by time_created desc limit ?" % table,
            (sid, limit * 3)).fetchall()
    except Exception:
        return []


# ctx 默认只看最近这么多轮：再长也不必——判断"还能撑几轮"用不着整部历史。
CTX_WINDOW = 40


def read_turns(sid, limit=40):
    """按时间升序返回最近 limit 轮带 tokens 的 assistant 消息。"""
    db = opencode_db()
    if db is None or not sid:
        return []
    uri = "file:" + str(db).replace("\\", "/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=10)
    except Exception:
        return []
    try:
        raw = (_rows_for_session(con, "session_message", sid, limit)
               + _rows_for_session(con, "message", sid, limit))
    finally:
        con.close()

    turns, seen = [], set()
    for mid, tc, data in raw:
        if mid in seen:
            continue
        seen.add(mid)
        try:
            d = json.loads(data or "{}")
        except Exception:
            continue
        t = d.get("tokens")
        if not isinstance(t, dict) or "input" not in t:
            continue
        cache = t.get("cache") or {}
        model = d.get("model") or {}
        row = {
            "id": mid, "ts": int(tc or 0),
            "input": int(t.get("input") or 0),
            "output": int(t.get("output") or 0),
            "reasoning": int(t.get("reasoning") or 0),
            "cache_read": int(cache.get("read") or 0),
            "cache_write": int(cache.get("write") or 0),
            "cost": float(d.get("cost") or 0.0),
            "provider": model.get("providerID") or d.get("providerID") or "",
            "model": model.get("id") or d.get("modelID") or "",
        }
        row["ctx_tokens"] = row["input"] + row["cache_read"]
        turns.append(row)
    turns.sort(key=lambda r: r["ts"])
    return turns[-limit:]


def growth_rate(turns, window=8):
    """上下文每轮增速：近几轮 ctx_tokens 差分的中位数。

    用中位数不用均值：偶发一次读大文件会把均值拉飞。返回 (tok/轮, 用了几个点)。
    """
    tail = [t for t in turns if t["ctx_tokens"]][-window:]
    if len(tail) < 3:
        return 0, 0
    deltas = sorted(b["ctx_tokens"] - a["ctx_tokens"]
                    for a, b in zip(tail, tail[1:])
                    if b["ctx_tokens"] >= a["ctx_tokens"])
    if not deltas:
        return 0, len(tail)
    mid = len(deltas) // 2
    med = deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) // 2
    return med, len(tail)


def ctx_report(root, sid=None):
    """本轮上下文 / 花费 / 该压还是该换会话。

    诚实口径（比功能本身重要）：优先读逐轮数据；读不到才退回采样差值；
    再读不到就说「未知」。绝不把会话累计值冒充成单轮上下文。
    """
    plan = plan_maybe(root)
    ledger = load_ledger(root)
    prices, cfg_path = load_prices()
    row = current_session(sid)
    price = prices.get((row["provider"], row["model"])) if row else None

    rep = {
        "config": str(cfg_path) if cfg_path else None,
        "db": str(opencode_db()) if opencode_db() else None,
        "session": row,
        "price": price,
        "turn": None,
        "turns": 0,
        "growth": None,
        "cumulative": None,
        "history": [],
        "estimate": {"tokens": None, "source": None},
        "savings": None,
        "verdict": [],
    }
    if row is None:
        rep["verdict"].append(
            "读不到 opencode.db 的会话行：只给计划侧判断，钱和上下文都算不出来")
        return rep

    window = (price or {}).get("context") or 0
    turns = read_turns(row["session"], limit=CTX_WINDOW)
    rep["turns"] = len(turns)
    # 窗口读满 = 会话比这个长。此时 len(turns) 只是窗口大小，不是轮次序号，
    # 拿去当"第 N 轮"说就是谎报（这个项目最该避免的错误，见 README 局限）。
    rep["truncated"] = len(turns) >= CTX_WINDOW
    last = turns[-1] if turns else None

    if last is not None:
        est = last["ctx_tokens"]
        turn_usd = last["cost"] or cost_usd(
            last["input"], last["output"], last["reasoning"],
            last["cache_read"], last["cache_write"], price)
        rep["turn"] = dict(last, usd=turn_usd, baseline="message")
        rep["estimate"] = {
            "tokens": est,
            "source": ("最近 %d 轮里最后一轮的 input + cache_read（逐轮数据）"
                       % len(turns)) if rep["truncated"] else
                      ("第 %d 轮的 input + cache_read（逐轮数据）" % len(turns)),
        }
        rep["history"] = [{
            "ts": t["ts"], "ctx": t["ctx_tokens"], "usd": t["cost"],
            "hit": (t["cache_read"] / t["ctx_tokens"]) if t["ctx_tokens"] else 0.0,
            "model": t["model"],
        } for t in turns[-16:]]
        rate, n = growth_rate(turns)
        if rate:
            if window:
                ceiling = int(window * 0.9)
                left = max(0, (ceiling - est) // rate)
            else:
                ceiling, left = None, None   # 窗口未知就不假装算得出「还能撑几轮」
            rep["growth"] = {"tok_per_round": rate, "samples": n,
                             "rounds_left": left, "ceiling": ceiling}
    else:
        prev = last_sample(root)
        if prev and prev.get("session") == row["session"]:
            keys = ("input", "output", "reasoning", "cache_read", "cache_write")
            delta = {k: max(0, row[k] - int(prev.get(k, 0))) for k in keys}
            est = delta["input"] + delta["cache_read"]
            rep["turn"] = dict(
                delta, ctx_tokens=est, baseline="sample", since=prev.get("ts"),
                usd=cost_usd(delta["input"], delta["output"], delta["reasoning"],
                             delta["cache_read"], delta["cache_write"], price))
            rep["estimate"] = {"tokens": est,
                               "source": "两次采样差值（该库没有逐轮数据）"}
        else:
            base = int(plan.get("baseline_context") or 0)
            est = base or None
            rep["turn"] = {"baseline": "none", "ctx_tokens": None, "usd": None}
            rep["estimate"] = {
                "tokens": est,
                "source": "计划里的 --baseline-context 估计值" if base else None,
            }
            rep["verdict"].append(
                "这会话读不到逐轮 token 数据，也没有基线：单轮上下文未知。"
                "跑一次 tg ctx --sample 记基线，下一轮才有差值口径。")

    cum_cost = cost_usd(row["input"], row["output"], row["reasoning"],
                        row["cache_read"], row["cache_write"], price)
    cum_hit = (row["cache_read"] / billed_of(row)) if billed_of(row) else 0.0
    rep["cumulative"] = {"billed": billed_of(row), "usd_recalc": cum_cost,
                         "usd_db": row["cost"], "cache_hit": cum_hit}
    if price is None:
        rep["verdict"].append(
            "这个模型在本地配置里没写 cost，OpenCode 记不到钱、tollgate 也算不出钱。"
            "中转/自建 provider 要在 opencode.json 的模型上加 cost；"
            "官方 provider 的价目在 models.dev 目录里，本地读不到。")
    elif row["cost"] <= 0 and cum_cost:
        rep["verdict"].append(
            "opencode.db 里这会话的 cost 还是 0：该 provider 的模型没写 cost，"
            "OpenCode 记不到钱（下面的重算值只是上界）")
    elif row["cost"] > 0 and cum_cost and cum_cost > row["cost"] * 1.1:
        rep["verdict"].append(
            "db 记的 %s 低于「全按当前单价重算」的 %s：本会话换过更便宜的模型，"
            "或中转实际折扣低于写进 cost 的值。记账以 db 为准，重算只作上界。"
            % (money(row["cost"]), money(cum_cost)))

    # ---- 判断一：窗口占用
    if est and window:
        ratio = est / window
        if ratio >= CTX_WARN_RATIO:
            rep["verdict"].append(
                "上下文 %s tok 已占窗口 %s 的 %.0f%%：再读一个大文件就顶天花板，"
                "优先 `handoff` 换会话" % (fmt_k(est), fmt_k(window), ratio * 100.0))

    # ---- 判断二：按增速预测还能撑几轮（这才是「过闸」该提前说的话）
    g = rep["growth"]
    if g and g["rounds_left"] is not None and g["rounds_left"] <= 5:
        rep["verdict"].append(
            "按近 %d 轮中位增速 +%s tok/轮，还有 %d 轮就撞到窗口 90%%（%s tok）："
            "现在换手比撑到爆再重开便宜"
            % (g["samples"], fmt_k(g["tok_per_round"]), g["rounds_left"],
               fmt_k(g["ceiling"])))

    # ---- 判断三：前缀稳定性（用最近一轮算，比会话累计有意义）
    if last is not None and last["ctx_tokens"] > 20000:
        hit = last["cache_read"] / last["ctx_tokens"]
        if hit < CACHE_HEALTH:
            rep["verdict"].append(
                "最近一轮缓存命中率只有 %.0f%%（健康线 %.0f%%）：稳定前缀在漂移——"
                "把会变的东西（时间戳、随机数、未排序的目录列举）挪到提示末尾"
                % (hit * 100.0, CACHE_HEALTH * 100.0))

    # ---- 判断四：轮数预算
    doing = next((s for s in plan["steps"] if s["status"] == "doing"), None)
    if doing is not None:
        reqs = reqs_for(ledger, doing["n"])
        br = doing.get("budget_rounds", 0)
        if br and reqs > br:
            rep["verdict"].append(
                "第 %d 步已用 %d 轮，超过预算 %d 轮：这是轮数问题，不是压缩问题——"
                "拆步骤或 handoff，别再压输入" % (doing["n"], reqs, br))

    # ---- 两种省钱手段的比价
    if price and est:
        p_in = price["input"]
        compress_save = est * (1 - 1.0 / COMPRESS_RATIO) * p_in / 1e6
        handoff_save = max(0, est - BRIEF_TOKENS) * p_in / 1e6
        rep["savings"] = {
            "compress_usd": compress_save,
            "handoff_usd": handoff_save,
            "ratio": (handoff_save / compress_save) if compress_save else None,
        }
    return rep


def cmd_ctx(a):
    root = find_root()
    rep = ctx_report(root, getattr(a, "session", None))
    row = rep["session"]

    booted = False
    if getattr(a, "sample", False) and row is not None:
        append_sample(root, _sample_record(row))
        booted = True

    if getattr(a, "json", False):
        rep["bootstrapped"] = booted
        out(json.dumps(rep, ensure_ascii=False, indent=1))
        return

    price, turn = rep["price"], rep["turn"]
    est = (rep["estimate"] or {}).get("tokens")

    out("== 上下文 ==")
    if row is None:
        out("  没有可读的会话行（opencode.db 里没有 session_v2 记录）")
    else:
        out("  会话 %s   %s/%s" % (row["session"][:20],
                                   row["provider"] or "?", row["model"] or "?"))
        if est and turn and turn.get("baseline") == "message":
            if rep.get("truncated"):
                line = "  最近 %d 轮里最后一轮的上下文 %s tok" % (rep["turns"], fmt_k(est))
            else:
                line = "  第 %d 轮上下文 %s tok" % (rep["turns"], fmt_k(est))
            if price and price["context"]:
                line += "（窗口 %s 的 %.0f%%）" % (
                    fmt_k(price["context"]), est / price["context"] * 100.0)
            out(line)
            out("  构成 input %s + cache_read %s，本轮命中率 %.1f%%"
                % (fmt_k(turn["input"]), fmt_k(turn["cache_read"]),
                   (turn["cache_read"] / est * 100.0) if est else 0.0))
            if rep.get("truncated"):
                out("  会话比这长：这里只看最近 %d 轮（判断「还能撑几轮」用不着更早的历史）"
                    % CTX_WINDOW)
        elif est:
            out("  自上次采样以来 %s tok（%s）" % (fmt_k(est), rep["estimate"]["source"]))
        else:
            out("  这一轮上下文：未知（该库无逐轮数据且没有基线）")

        g = rep["growth"]
        if g:
            if g["rounds_left"] is not None:
                out("  近 %d 轮中位增速 +%s tok/轮 → 距窗口 90%%（%s）还有 %d 轮"
                    % (g["samples"], fmt_k(g["tok_per_round"]), fmt_k(g["ceiling"]),
                       g["rounds_left"]))
            else:
                out("  近 %d 轮中位增速 +%s tok/轮（窗口未知，算不出还能撑几轮）"
                    % (g["samples"], fmt_k(g["tok_per_round"])))
        out("  累计 billed %s tok   命中率 %.1f%%（会话级）"
            % (fmt_k(billed_of(row)), rep["cumulative"]["cache_hit"] * 100.0))

    out("")
    out("== 钱 ==")
    if price is None:
        out("  该模型没有价目：官方 provider 的价格在 models.dev 目录里，本地读不到；")
        out("  中转/自建 provider 需要在 opencode.json 里给模型写 cost 才算得出钱。")
    else:
        out("  单价 USD/百万：input %.6f · output %.6f · cache.read %.6f"
            % (price["input"], price["output"], price["cache_read"]))
        if turn and turn.get("usd") is not None:
            src = "消息自带 cost" if turn.get("baseline") == "message" else "按单价重算"
            out("  这一轮 %s（%s）" % (money(turn["usd"]), src))
        out("  本会话累计   db 记录 %s" % money(rep["cumulative"]["usd_db"]))
        out("               按当前单价重算 %s（上界）"
            % money(rep["cumulative"]["usd_recalc"]))

    if rep["savings"]:
        s = rep["savings"]
        out("")
        out("== 省钱的两条路，按当前单价、只看输入侧 ==")
        out("  压缩到 1/%.0f      省 %s" % (COMPRESS_RATIO, money(s["compress_usd"])))
        out("  handoff 换会话    省 %s" % money(s["handoff_usd"]))
        if s["ratio"]:
            out("  换手 / 压缩 = %.1f×。这个比值是结构性的，不是巧合：" % s["ratio"])
            out("  换手把上下文打到 ~%d tok 的简报，压缩只削掉 2/3 且每轮都要重付一次压缩调用。"
                % BRIEF_TOKENS)
            out("  所以只要任务还能在简报里说清楚，就先换手；只有在「中间推理必须原样保住、"
                "写不进简报」时才压缩。")

    out("")
    if rep["verdict"]:
        out("== 判断 ==")
        for v in rep["verdict"]:
            out("  · " + v)
    else:
        out("  没有触发任何阈值：可以继续，不用压也不用换。")

    if booted:
        out("")
        out("已记采样 → .plan/ctx.jsonl")


def cmd_price(a):
    prices, cfg_path = load_prices()
    if cfg_path is None:
        out("找不到 opencode.json，无价目可报。")
        return
    out("配置：%s" % cfg_path)
    if not prices:
        out("配置里没有任何模型写了 cost —— 于是 OpenCode 记不到钱。")
        return
    row = current_session()
    cur = (row["provider"], row["model"]) if row else None
    out("")
    out("%-14s %-26s %9s %9s %9s %9s %8s" % (
        "provider", "model", "input", "output", "c.read", "c.write", "窗口"))
    for (pid, mid), p in sorted(prices.items()):
        mark = "  ← 当前" if (pid, mid) == cur else ""
        out("%-14s %-26s %9.6f %9.6f %9.6f %9.6f %8s%s"
            % (pid, mid, p["input"], p["output"], p["cache_read"],
               p["cache_write"], fmt_k(p["context"]) if p["context"] else "-", mark))
    out("")
    out("单位 USD / 百万 token。人民币按 1 USD = %.3f CNY 折算。" % CNY_PER_USD)
    out("中转是人民币标价，所以值是「中转价 ÷ 汇率」；分组倍率通常还会再打折，"
        "所以这里的钱是上界。")


# ---------------------------------------------------------------- 两闸交界
#
# 闸一（计划预算）原先靠手填 `tg spend <n> --in M --out K --reqs R` 记账，而闸二
# 已经能从 session_message 里读出每一轮真实的 tokens 和 cost。中间那道缝不该让
# 人去补：`tg sync` 把「上次 sync 之后宿主真实跑掉的轮次」直接写进账本。
#
# 口径与 spend 严格一致，不另立一套：
#   in      = Σ 每轮 input（新输入，不含缓存命中）
#   out     = Σ (output + reasoning)   —— 推理 token 按输出价计费，算进新 token
#   reqs    = 轮数（assistant 消息条数）
#   cache_read / cache_write 单列存进记录，但不参与预算求和 —— 命中只是便宜的重读，
#   混进预算会让「该压上下文还是该换会话」的判断失真（见 references/context-budget.md）。
#
# 归账只能归到「正在做」的那一步：宿主不知道你的步骤边界，sync 也不知道你在哪
# 一轮切了步骤。所以切步骤之前先 sync 一次，是使用者的责任，工具如实说这句话。

SYNC_KEEP = 2000

# 读窗口和游标窗口必须是同一个数：游标记住的就是读窗口里的那些 id。窗口对齐
# 才能保证「读到的轮次全都认得、认得的都在读窗口里」，既不重复记账，也不会因为
# 游标比窗口窄而反复重记。窗口之外更早的轮次（只有长得离谱的会话才会遇到）第一
# 次对账时会被跳过 —— 这件事必须说出来，不能默默截断。


def sync_path(root):
    return paths(root)["dir"] / "sync.json"


def load_sync(root):
    """读取同步游标：已入账的消息 id 集合。空/坏文件一律当「从没同步过」。"""
    blank = {"session": "", "ids": [], "updated": ""}
    p = sync_path(root)
    if not p.is_file():
        return blank
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return blank
    if not isinstance(d, dict):
        return blank
    ids = d.get("ids")
    return {
        "session": str(d.get("session") or ""),
        "ids": [str(x) for x in ids] if isinstance(ids, list) else [],
        "updated": str(d.get("updated") or ""),
    }


def save_sync(root, data):
    p = sync_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session": data.get("session", ""),
        "ids": (data.get("ids") or [])[-SYNC_KEEP:],
        "updated": now_iso(),
    }
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


def cost_for(ledger, n=None):
    """账本里某步（或全部）真实记录的钱。没有 cost 的记录不计入，返回 (金额, 笔数)。"""
    tot, cnt = 0.0, 0
    for r in ledger:
        if n is not None and r.get("step") != n:
            continue
        c = r.get("cost")
        if not c:
            continue
        tot += float(c)
        cnt += 1
    return (tot, cnt) if cnt else (None, 0)


def cmd_sync(a):
    """把宿主的真实用量对账进账本，替代手填 spend。"""
    root = find_root()
    plan = load(root)

    row = current_session(getattr(a, "session", None))
    if row is None:
        die("读不到宿主的用量库（opencode.db）。没有真实用量就没法对账 ——"
            "这种情况下只能继续手填：tg spend <n> --in M --out K --reqs R")

    if a.step is not None:
        s = step_by_n(plan, a.step)
    else:
        s = next((x for x in plan["steps"] if x.get("status") == "doing"), None)
        if s is None:
            die("当前没有 doing 的步骤，用量无处可归。先 tg start N，"
                "或者显式指定：tg sync --step N")

    turns = read_turns(row["session"], limit=SYNC_KEEP)
    truncated = len(turns) >= SYNC_KEEP
    state = load_sync(root)
    if state["session"] != row["session"]:
        # 换了会话就从零算：上一个会话的 id 与这个会话无关
        state = {"session": row["session"], "ids": []}
    seen = set(state["ids"])
    fresh = [t for t in turns if t["id"] not in seen]

    if not fresh:
        msg = "没有新轮次：会话 %s 最近 %d 轮都已入账。" % (
            row["session"][:12], len(turns))
        if a.json:
            out(json.dumps({"ok": True, "fresh": 0, "turns_read": len(turns),
                            "session": row["session"], "step": s["n"]},
                           ensure_ascii=False, indent=1))
        else:
            out(msg)
        return

    tin = sum(t["input"] for t in fresh)
    tout = sum(t["output"] for t in fresh)
    trea = sum(t["reasoning"] for t in fresh)
    tcr = sum(t["cache_read"] for t in fresh)
    tcw = sum(t["cache_write"] for t in fresh)
    host_cost = sum(t["cost"] for t in fresh)

    prices, _ = load_prices()
    est, est_ok = 0.0, False
    for t in fresh:
        pr = prices.get((t["provider"], t["model"])) or prices.get(
            (row["provider"], row["model"]))
        c = cost_usd(t["input"], t["output"], t["reasoning"],
                     t["cache_read"], t["cache_write"], pr)
        if c is not None:
            est += c
            est_ok = True
    if host_cost > 0:
        cost, cost_src = host_cost, "宿主记录"
    elif est_ok:
        cost, cost_src = est, "按配置单价重算（上界）"
    else:
        cost, cost_src = None, "未知（宿主的消息里没 cost，本地也没价目）"

    rec = {"ts": now_iso(), "step": s["n"], "in": tin, "out": tout + trea,
           "reqs": len(fresh), "cache_read": tcr, "src": "opencode.db",
           "note": "sync 自动对账（%d 轮）" % len(fresh)}
    if tcw:
        rec["cache_write"] = tcw
    if cost is not None:
        rec["cost"] = round(cost, 12)
        if host_cost <= 0:
            rec["cost_est"] = True

    if a.dry_run:
        ledger_after = load_ledger(root) + [rec]
    else:
        append_ledger(root, rec)
        state["ids"] = (state["ids"] or []) + [t["id"] for t in fresh]
        state["session"] = row["session"]
        save_sync(root, state)
        ledger_after = load_ledger(root)

    used = spent_for(ledger_after, s["n"])
    reqs = reqs_for(ledger_after, s["n"])
    budget = s.get("budget", 0)
    br = s.get("budget_rounds", 0)
    pct = (used / budget * 100) if budget else 0
    over_budget = bool(budget) and used > budget
    over_rounds = bool(br) and reqs > br

    if a.json:
        out(json.dumps({
            "ok": True, "fresh": len(fresh), "turns_read": len(turns),
            "session": row["session"], "step": s["n"],
            "synced": {"in": tin, "out": tout, "reasoning": trea,
                       "reqs": len(fresh), "cache_read": tcr,
                       "cache_write": tcw},
            "cost": cost, "cost_src": cost_src, "dry_run": bool(a.dry_run),
            "step_total": {"tokens": used, "budget": budget, "pct": round(pct, 1),
                           "reqs": reqs, "budget_rounds": br},
            "over_budget": over_budget, "over_rounds": over_rounds,
            "truncated": truncated, "record": rec,
        }, ensure_ascii=False, indent=1))
        return

    out("== 对账（源：opencode.db） ==")
    out("  会话 %s ｜ 本次同步 %d 轮（上次 sync 之后）" % (row["session"][:12], len(fresh)))
    if truncated:
        out("  只读到最近 %d 轮 —— 更早的轮次读不到，也就不会计入。这时的 tok 数"
            "不等于「会话总共跑了多少」，那看 tg ctx。" % SYNC_KEEP)
    out("  记入 #%d：新输入 %s + 输出 %s（其中推理 %s）= %s tok"
        % (s["n"], fmt_k(tin), fmt_k(tout), fmt_k(trea), fmt_k(tin + tout + trea)))
    if tcr or tcw:
        out("  缓存读 %s / 写 %s —— 不计入预算：命中只是便宜的重读，"
            "不占新 token" % (fmt_k(tcr), fmt_k(tcw)))
    out("  钱：%s · %s" % (money(cost), cost_src))
    out("")
    out("== 这一步累计 ==")
    out("  #%d %s / %s（%.0f%%）" % (s["n"], fmt_k(used), fmt_k(budget), pct))
    if reqs:
        line = "  轮数：%d 轮" % reqs
        if br:
            line += "（预算 %d 轮）" % br
        line += " ｜ 单轮平均 %s" % fmt_k(int(used / reqs))
        out(line)
    if a.dry_run:
        out("")
        out("--dry-run：没写账本，游标没动，下次 sync 还会看到这 %d 轮。" % len(fresh))
    else:
        out("  已写入 .plan/ledger.jsonl；游标 → .plan/sync.json")
    if over_rounds:
        warn("已用 %d 轮，超过本步预算 %d 轮。继续之前先想：是任务本身要这么多轮，"
             "还是每轮都在重新读同一批文件？" % (reqs, br))
    if over_budget:
        warn("超预算 %s tok。看 tg ctx：该压上下文还是该换会话，那里有数。" % fmt_k(used - budget))


# ---------------------------------------------------------------- 工作台

def ui_dir():
    return Path(__file__).resolve().parent / "ui"


def state_payload(root):
    plan = plan_maybe(root)
    ledger = load_ledger(root)
    rep = ctx_report(root)
    steps = []
    for s in plan.get("steps", []):
        cash, _ = cost_for(ledger, s["n"])
        steps.append({
            "n": s["n"], "title": s["title"], "status": s["status"],
            "tier": s.get("tier", ""), "out": s.get("out", ""),
            "budget": s.get("budget", 0), "spent": spent_for(ledger, s["n"]),
            "budget_rounds": s.get("budget_rounds", 0),
            "reqs": reqs_for(ledger, s["n"]),
            "usd": cash,
            "accept": s.get("accept", []), "note": s.get("note", ""),
            "depends": s.get("depends", []),
        })
    total_budget = sum(s["budget"] for s in steps)
    total_spent = sum(s["spent"] for s in steps)

    # 钱：只认账本里真实记过的 cost。一笔都没有就报 null，让前端去说「没有」，
    # 而不是拿 token 数按当前单价硬折一个看起来很像真的数字。
    usd_total, usd_rows = cost_for(ledger)
    usd_rounds = sum(r.get("reqs", 0) for r in ledger if r.get("cost"))
    money = {
        "ledger_usd": usd_total,
        "rows": usd_rows,
        "rounds": usd_rounds,
        "est_rows": sum(1 for r in ledger if r.get("cost") and r.get("cost_est")),
        "per_round_usd": (usd_total / usd_rounds) if (usd_total and usd_rounds) else None,
        "host_usd": (rep.get("cumulative") or {}).get("usd_db"),
    }

    # 对账状态：只看手上这 40 轮窗口里还有几轮没入账 —— 窗口外的东西看板也不该假装知道。
    state = load_sync(root)
    sid = (rep.get("session") or {}).get("session") or ""
    seen = set(state["ids"])
    pending = None
    if sid:
        pending = sum(1 for t in read_turns(sid, limit=CTX_WINDOW)
                      if t["id"] not in seen)
    syncing = {
        "session": state["session"],
        "synced_ids": len(state["ids"]),
        "updated": state.get("updated", ""),
        "same_session": bool(sid) and state["session"] == sid,
        "pending_in_window": pending,
    }

    return {
        "goal": plan.get("goal", ""),
        "why": plan.get("why", ""),
        "root": str(root),
        "schema": plan.get("schema", SCHEMA),
        "baseline_context": plan.get("baseline_context", 0),
        "exclude": plan.get("exclude", []),
        "steps": steps,
        "totals": {
            "budget": total_budget, "spent": total_spent,
            "reqs": reqs_for(ledger), "steps": len(steps),
            "done": sum(1 for s in steps if s["status"] == "done"),
            "ledger_rows": len(ledger),
        },
        "money": money,
        "sync": syncing,
        "ctx": rep,
        "ts": now_iso(),
    }


def _make_handler(root):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tollgate"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
            return host in ("127.0.0.1", "localhost", "::1")

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            if not self._host_ok():
                self._send(403, b"local only", "text/plain; charset=utf-8")
                return
            path = self.path.split("?")[0]
            if path == "/api/state":
                try:
                    self._json(state_payload(root))
                except Exception as e:
                    self._json({"error": str(e)}, 500)
                return
            static = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                "/style.css": ("style.css", "text/css; charset=utf-8"),
            }
            if path in static:
                name, ctype = static[path]
                f = ui_dir() / name
                if not f.is_file():
                    self._send(404, ("缺 UI 文件：%s" % f).encode("utf-8"),
                               "text/plain; charset=utf-8")
                    return
                self._send(200, f.read_bytes(), ctype)
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):
            if not self._host_ok():
                self._send(403, b"local only", "text/plain; charset=utf-8")
                return
            if self.path.split("?")[0] != "/api/sample":
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            row = current_session()
            if row is None:
                self._json({"ok": False, "error": "没有可读的会话行"}, 400)
                return
            append_sample(root, _sample_record(row))
            self._json({"ok": True})

    return Handler


def cmd_serve(a):
    root = find_root()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", a.port), _make_handler(root))
    except OSError as e:
        die("端口 %d 起不来：%s" % (a.port, e))
    url = "http://127.0.0.1:%d/" % a.port
    out("tollgate 工作台：%s" % url)
    out("根目录 %s" % root)
    out("只绑 127.0.0.1，Ctrl+C 停。")
    if not a.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        out("\n已停止。")
    finally:
        httpd.server_close()


# ---------------------------------------------------------------- 入口

def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        prog="tg", description="tollgate：agent 的过闸 —— 计划预算 · 上下文预算 · 本地账本")
    ap.add_argument("-V", "--version", action="version",
                    version="tollgate 0.2（内核 plan-ledger v0.3 / schema %d）" % SCHEMA)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="建立 .plan/")
    p.add_argument("goal")
    p.add_argument("--why", help="这个任务为什么该存在（Ponytail 梯子第 0 级）")
    p.add_argument("--exclude", action="append", help="禁读路径，可重复")
    p.add_argument("--max-rounds", dest="max_rounds", type=int, default=3,
                   help="单步最多重试轮次，到顶强制 block（默认 3）")
    p.add_argument("--baseline-context", dest="baseline_context", type=int, default=0,
                   help="本会话一次请求的典型上下文 token 数，用于把轮数预算折算成钱")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("add", help="追加一个步骤")
    p.add_argument("--title", required=True)
    p.add_argument("--tier", choices=TIERS, default="std")
    p.add_argument("--budget", type=int, default=8000, help="token 预算（易失真，见 --rounds）")
    p.add_argument("--rounds", type=int, default=0,
                   help="这步最多允许几轮请求（比 token 好估，推荐）")
    p.add_argument("--out", help="产物路径（必填）")
    p.add_argument("--read", action="append", help="计划内要读的文件，可重复")
    p.add_argument("--depends", help="依赖的步骤号，逗号分隔，如 1,2")
    p.add_argument("--reuse", choices=REUSE_RUNGS, default="new",
                   help="复用梯子：skip/existing/stdlib/native/dep/line/new")
    p.add_argument("--stop", action="append", help="停止条件，可重复")
    p.add_argument("--accept", action="append", help="验收断言，可重复")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("amend", help="改已有计划（n=0 改计划级设置）")
    p.add_argument("n", type=int, help="步骤号；0 表示改计划级设置")
    p.add_argument("--goal")
    p.add_argument("--why")
    p.add_argument("--max-rounds", dest="max_rounds", type=int)
    p.add_argument("--baseline-context", dest="baseline_context", type=int)
    p.add_argument("--add-exclude", dest="add_exclude", action="append")
    p.add_argument("--title")
    p.add_argument("--tier", choices=TIERS)
    p.add_argument("--budget", type=int)
    p.add_argument("--rounds", type=int, help="这步允许几轮请求")
    p.add_argument("--out")
    p.add_argument("--reuse", choices=REUSE_RUNGS)
    p.add_argument("--depends", help="整体替换依赖，逗号分隔；传空串表示清空")
    p.add_argument("--note", help="覆盖步骤备注")
    p.add_argument("--add-accept", dest="add_accept", action="append")
    p.add_argument("--rm-accept", dest="rm_accept", action="append")
    p.add_argument("--clear-accept", dest="clear_accept", action="store_true")
    p.add_argument("--add-stop", dest="add_stop", action="append")
    p.add_argument("--rm-stop", dest="rm_stop", action="append")
    p.add_argument("--clear-stop", dest="clear_stop", action="store_true")
    p.add_argument("--add-read", dest="add_read", action="append")
    p.add_argument("--rm-read", dest="rm_read", action="append")
    p.add_argument("--clear-read", dest="clear_read", action="store_true")
    p.add_argument("--reason", default="", help="为什么改（写进 ledger，强烈建议）")
    p.add_argument("--force", action="store_true", help="允许改 done 步骤")
    p.set_defaults(fn=cmd_amend)

    p = sub.add_parser("exclude", help="追加禁读路径")
    p.add_argument("patterns", nargs="+")
    p.set_defaults(fn=cmd_exclude)

    p = sub.add_parser("ready", help="列出可立刻开始的步骤")
    p.set_defaults(fn=cmd_ready)

    p = sub.add_parser("parallel", help="按依赖与产物冲突切并行批次")
    p.set_defaults(fn=cmd_parallel)

    p = sub.add_parser("graph", help="打印依赖图")
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("start", help="开始某一步")
    p.add_argument("n", type=int)
    p.add_argument("--force", action="store_true", help="跳过的依赖检查")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("reset", help="把某一步退回 todo")
    p.add_argument("n", type=int)
    p.add_argument("--rounds", action="store_true", help="同时把重试轮次计数清零")
    p.set_defaults(fn=cmd_reset)

    p = sub.add_parser("block", help="标记卡住")
    p.add_argument("n", type=int)
    p.add_argument("--why", required=True)
    p.set_defaults(fn=cmd_block)

    p = sub.add_parser("spend", help="记一笔 token 消耗")
    p.add_argument("n", type=int)
    p.add_argument("--in", dest="input", type=int, default=0)
    p.add_argument("--out", dest="output", type=int, default=0)
    p.add_argument("--reqs", type=int, default=0, help="这笔记账覆盖了几轮请求")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_spend)

    p = sub.add_parser("gate", help="跑验收断言")
    p.add_argument("n", type=int, nargs="?", default=None)
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("done", help="标记完成（gate 不通过会拒绝）")
    p.add_argument("n", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_done)

    p = sub.add_parser("handoff", help="为便宜模型生成自足式任务简报")
    p.add_argument("n", type=int)
    p.add_argument("--print", dest="print_it", action="store_true", help="同时打印全文")
    p.set_defaults(fn=cmd_handoff)

    p = sub.add_parser("reconcile", help="重跑已完成步骤的门禁，找回归")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("report", help="成本 / 预算准确性 / 复用率报表")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("ack", help="人工确认一条 ack: 断言")
    p.add_argument("expr")
    p.set_defaults(fn=cmd_ack)

    p = sub.add_parser("status", help="状态摘要（≤150 token）")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("show", help="打印完整计划")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("resume", help="崩溃/清空后的恢复提示")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("drift", help="一致性检查")
    p.set_defaults(fn=cmd_drift)

    p = sub.add_parser("ctx", help="本轮上下文预算 + 花费 + 该压缩还是该换会话")
    p.add_argument("--session", help="会话 id（默认 $OPENCODE_SESSION_ID 或最近一条）")
    p.add_argument("--json", action="store_true", help="机器可读输出")
    p.add_argument("--sample", action="store_true",
                   help="顺手记一笔快照到 .plan/ctx.jsonl（下一轮才有差值口径）")
    p.set_defaults(fn=cmd_ctx)

    p = sub.add_parser("price", help="列出配置里的模型价目（USD/百万 token）")
    p.set_defaults(fn=cmd_price)

    p = sub.add_parser("sync", help="把宿主的真实用量对账进账本（不用再手填 spend）")
    p.add_argument("--step", type=int, help="归到哪一步（默认当前 doing 的那一步）")
    p.add_argument("--session", help="会话 id（默认 $OPENCODE_SESSION_ID 或最近一条）")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="只报数，不写账本、不动游标")
    p.add_argument("--json", action="store_true", help="机器可读输出")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("serve", help="本地工作台（127.0.0.1，只读 + 采样）")
    p.add_argument("--port", type=int, default=7717)
    p.add_argument("--no-open", dest="no_open", action="store_true",
                   help="不要自动开浏览器")
    p.set_defaults(fn=cmd_serve)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
