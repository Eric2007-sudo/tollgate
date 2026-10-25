# -*- coding: utf-8 -*-
"""plan-ledger v0.3 端到端：amend（改计划）+ 轮次口径（--rounds / --reqs / baseline）

跑法：python tests/test_v03.py
退出码 0 = 全绿。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PL = os.path.join(os.path.dirname(HERE), "tg.py")
FAILED = []
LOGS = []


def run(args, cwd, expect=0, must=None, absent=None):
    r = subprocess.run([sys.executable, PL] + args, cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    text = (r.stdout or "") + (r.stderr or "")
    LOGS.append("$ pl.py %s  [exit %d]\n%s" % (" ".join(args), r.returncode, text))
    bad = []
    if r.returncode != expect:
        bad.append("exit=%d 期望 %d" % (r.returncode, expect))
    if must and must not in text:
        bad.append("输出里缺 %r" % must)
    if absent and absent in text:
        bad.append("输出里不该有 %r" % absent)
    if bad:
        FAILED.append("pl.py %s → %s" % (" ".join(args), "; ".join(bad)))
        print("FAIL  %s   %s" % (" ".join(args)[:64], "; ".join(bad)))
        print(text)
    else:
        print("OK    %s" % " ".join(args)[:78])
    return r


def check(name, cond, detail=""):
    if cond:
        print("OK    %s" % name)
    else:
        FAILED.append("%s %s" % (name, detail))
        print("FAIL  %s   %s" % (name, detail))


def plan_json(cwd):
    with open(os.path.join(cwd, ".plan", "plan.json"), encoding="utf-8") as fh:
        return json.load(fh)


def step(cwd, n):
    return next(s for s in plan_json(cwd)["steps"] if s["n"] == n)


def ledger(cwd):
    p = os.path.join(cwd, ".plan", "ledger.jsonl")
    if not os.path.exists(p):
        return []
    rows = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main():
    root = tempfile.mkdtemp(prefix="plv03_")
    print("workdir:", root)
    subprocess.run("git init -q && git config user.email a@b.c && git config user.name t",
                   cwd=root, shell=True, capture_output=True)
    with open(os.path.join(root, "app.py"), "w", encoding="utf-8") as fh:
        fh.write("def hello():\n    return 1\n")
    subprocess.run("git add -A && git commit -qm init", cwd=root, shell=True,
                   capture_output=True)
    os.makedirs(os.path.join(root, "notes"), exist_ok=True)

    # ---------------- baseline_context 进计划级 ----------------
    run(["init", "重构 hello 并补测试", "--why", "调用方要传名字",
         "--exclude", "node_modules", "--baseline-context", "160000"], root)
    check("init 写入 baseline_context",
          plan_json(root).get("baseline_context") == 160000,
          repr(plan_json(root).get("baseline_context")))

    # ---------------- add --rounds / 警告 ----------------
    r = run(["add", "--title", "列调用点", "--tier", "cheap", "--budget", "6000",
             "--out", "notes/01.md", "--reuse", "existing", "--rounds", "2",
             "--accept", "file:exists:notes/01.md"], root,
            absent="没给 --rounds")
    check("add --rounds 2 落到 budget_rounds", step(root, 1).get("budget_rounds") == 2,
          repr(step(root, 1).get("budget_rounds")))
    md = open(os.path.join(root, ".plan", "plan.md"), encoding="utf-8").read()
    check("plan.md 渲染轮数上限", "请求轮数上限：2 轮" in md)
    check("plan.md 渲染会话基线", "会话基线" in md)
    r = run(["add", "--title", "改签名", "--tier", "std", "--budget", "9000",
             "--out", "app.py", "--depends", "1", "--reuse", "line",
             "--accept", "grep:def hello\\(name@app.py"], root,
            must="没给 --rounds")
    check("漏 --rounds 会警告", "没给 --rounds" in (r.stdout or ""))
    r = run(["add", "--title", "太小预算的步骤", "--budget", "500", "--out", "notes/03.md",
             "--depends", "1", "--rounds", "1"], root,
            must="不到一次请求的上下文")
    check("预算 < baseline 会警告", "不到一次请求的上下文" in (r.stdout or ""))

    # ---------------- amend：todo 步骤 ----------------
    r = run(["amend", "2", "--add-accept", "file:exists:app.py",
             "--reason", "忘了断言产物存在"], root, must="已改 1 项")
    check("amend 追加断言", "file:exists:app.py" in step(root, 2)["accept"],
          repr(step(root, 2)["accept"]))
    check("amend 写进 ledger",
          any(r.get("amend", {}).get("accept") for r in ledger(root)))
    run(["amend", "2", "--rm-accept", "file:exists:app.py"], root)
    check("amend 删除断言", "file:exists:app.py" not in step(root, 2)["accept"])
    run(["amend", "2", "--tier", "deep", "--budget", "20000", "--rounds", "5"], root)
    check("amend 改档位/预算/轮数",
          (step(root, 2)["tier"], step(root, 2)["budget"], step(root, 2)["budget_rounds"])
          == ("deep", 20000, 5),
          repr((step(root, 2)["tier"], step(root, 2)["budget"],
                step(root, 2)["budget_rounds"])))
    run(["amend", "3", "--add-stop", "如果预算超 1 轮就停", "--depends", "2"], root)
    check("amend 改依赖/停止条件",
          step(root, 3)["depends"] == [2] and step(root, 3)["stop"])

    # 空改动要拒绝
    run(["amend", "1"], root, expect=2, must="没有任何改动")
    # 非法依赖要拒绝
    run(["amend", "2", "--depends", "2"], root, expect=2, must="只能依赖别的步骤")
    run(["amend", "2", "--depends", "9"], root, expect=2, must="不存在")

    # ---------------- amend：计划级（n=0） ----------------
    run(["amend", "0", "--why", "调用方已经上线，不做会报错",
         "--add-exclude", "data/*.json", "--max-rounds", "2"], root,
        must="计划级改动")
    pj = plan_json(root)
    check("amend 0 改 why", pj["why"].startswith("调用方已经上线"), repr(pj.get("why")))
    check("amend 0 加禁读", "data/*.json" in pj["exclude"], repr(pj.get("exclude")))
    check("amend 0 改 max_rounds", pj["max_rounds"] == 2, repr(pj.get("max_rounds")))
    run(["amend", "0"], root, expect=2, must="没有任何改动")
    run(["amend", "0", "--baseline-context", "180000"], root)
    check("amend 0 改 baseline", plan_json(root)["baseline_context"] == 180000)

    # ---------------- 记账 + 轮次口径 ----------------
    run(["start", "1"], root)
    run(["spend", "1", "--in", "180000", "--out", "1200", "--reqs", "2"], root,
        must="单轮平均")
    check("spend --reqs 落进 ledger",
          any(r.get("reqs") == 2 for r in ledger(root) if r.get("step") == 1))
    with open(os.path.join(root, "notes", "01.md"), "w", encoding="utf-8") as fh:
        fh.write("x\n")
    run(["done", "1"], root)
    r = run(["spend", "1", "--in", "1000", "--out", "10"], root, must="没带 --reqs")
    check("漏 --reqs 会提示", "没带 --reqs" in (r.stdout or ""))

    r = run(["report"], root)
    check("report 有轮次口径段", "== 轮次口径 ==" in (r.stdout or ""))
    check("report 算出单轮平均", "单轮平均上下文" in (r.stdout or ""))
    check("report 给出 baseline 倍数", "单轮是它的" in (r.stdout or ""))

    # ---------------- done 步骤的 amend 护栏 ----------------
    run(["amend", "1", "--add-accept", "grep:def hello@app.py"], root,
        expect=2, must="已经 done")
    r = run(["amend", "1", "--add-accept", "grep:def hello@app.py",
             "--force", "--reason", "断言漏写"], root, must="reconcile")
    check("--force 改 done 会提示 reconcile", "reconcile" in (r.stdout or ""))
    check("--force 改动留痕",
          any(x.get("forced") and x.get("amend") for x in ledger(root)))
    check("done 步骤断言已补",
          "grep:def hello@app.py" in step(root, 1)["accept"])

    # ---------------- clear-accept 让 gate 变空 ----------------
    run(["amend", "1", "--clear-accept", "--force"], root)
    check("clear-accept 清空断言", step(root, 1)["accept"] == [])

    # ---------------- drift 抓到「有记账没轮数 / 无 baseline」 ----------------
    run(["spend", "2", "--in", "1000", "--out", "10"], root)
    run(["amend", "0", "--baseline-context", "0"], root, must="计划级改动")
    r = run(["drift"], root, expect=0, must="baseline_context")
    check("drift 报出缺 baseline", "baseline_context" in (r.stdout or ""))
    check("drift 报出缺轮数", "没有轮数" in (r.stdout or ""))

    # ---------------- 收尾：reconcile 仍可用 ----------------
    r = run(["reconcile"], root, expect=0)
    check("reconcile 可用", r.returncode == 0)

    print("\n==== %d 项失败 ====" % len(FAILED))
    for f in FAILED:
        print("  -", f)
    log = os.path.join(root, ".plan", "test-v03-output.txt")
    try:
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("\n\n".join(LOGS))
        print("full log:", log)
    except OSError:
        pass
    if not FAILED:
        print("KEEP:", root)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
