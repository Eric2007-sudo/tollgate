# -*- coding: utf-8 -*-
"""plan-ledger v0.3 端到端：amend（改计划）+ 轮次口径（--rounds / --reqs / baseline）

跑法：python tests/test_v03.py
退出码 0 = 全绿。
"""
import json
import os
import re
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
            absent="不到一次请求的上下文")
    check("按轮数定了预算就不再骂 token 预算小（给了 --rounds 还报就是假警告）",
          "不到一次请求的上下文" not in (r.stdout or ""))
    r = run(["add", "--title", "手写小预算", "--budget", "500", "--out", "notes/04.md",
             "--depends", "1"], root, must="不到一次请求的上下文")
    check("手写 --budget 且没给 --rounds 时仍然警告（这时才是真问题）",
          "不到一次请求的上下文" in (r.stdout or ""))

    # 复用梯子：默认档不是「用户的选择」，没写就不该被问「为什么新写」
    r = run(["add", "--title", "没写复用档", "--out", "notes/05.md",
             "--depends", "1", "--rounds", "2"], root, absent="却没有 --note")
    check("没显式给 --reuse 时不该问「为什么新写」（默认档不等于选过）",
          "却没有 --note" not in (r.stdout or ""))
    r = run(["add", "--title", "显式新写", "--out", "notes/06.md",
             "--depends", "1", "--rounds", "2", "--reuse", "new"], root,
            must="却没有 --note")
    check("显式 --reuse new 且没 --note 时才问（这时问得对）",
          "却没有 --note" in (r.stdout or ""))

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

    # ---------------- plan.json 被 Windows 编辑器加了 BOM 也要能读 ----------------
    # PowerShell 的 Set-Content、记事本、VS Code 另存都可能写 BOM。加了 BOM 还是同一份
    # 计划，不该让整个 CLI 崩掉 —— 这条在真实使用里踩到过。
    pj = os.path.join(root, ".plan", "plan.json")
    with open(pj, encoding="utf-8-sig") as fh:
        raw = fh.read()
    with open(pj, "w", encoding="utf-8-sig") as fh:      # 这个 encoding 会写 BOM
        fh.write(raw)
    check("测试确实写出了 BOM", open(pj, "rb").read(3) == b"\xef\xbb\xbf")
    r = run(["status"], root, expect=0)
    check("plan.json 带 BOM 时 status 不崩", r.returncode == 0)
    r = run(["report"], root, expect=0)
    check("plan.json 带 BOM 时 report 也正常", r.returncode == 0)

    # ---------------- handoff 简报带上轮数上限与「已经花了什么」 ----------------
    r = run(["handoff", "1"], root, expect=0)
    briefs = sorted(os.listdir(os.path.join(root, ".plan", "brief")))
    check("handoff 生成了简报文件", len(briefs) == 1, repr(briefs))
    body = open(os.path.join(root, ".plan", "brief", briefs[0]), encoding="utf-8").read() \
        if briefs else ""
    check("简报里有轮数上限", "轮数上限：2 轮" in body)
    check("简报里有「已经花了什么」", "## 这一步已经花了什么" in body)
    check("简报报出已用轮数", "轮数：2 / 2 轮" in body)
    check("简报没 cost 时指路 sync", "没有 cost 记录" in body)
    check("简报明说新会话的轮数不会自动记账", "不会自动记进这个计划" in body)
    check("简报的复用级数与档位对得上（existing 是第 2 级）",
          "前 1 级都不成立" in body, body[body.find("复用梯子"):][:40])

    # 产物路径撞车的步骤：自己的产物不该出现在自己的「不在范围内」里，也不该重复列
    run(["add", "--title", "共用产物", "--out", "notes/01.md", "--depends", "1",
         "--rounds", "2", "--reuse", "existing"], root)
    r = run(["handoff", "7"], root, expect=0)
    b7 = [x for x in os.listdir(os.path.join(root, ".plan", "brief")) if x.startswith("07-")]
    body7 = (open(os.path.join(root, ".plan", "brief", b7[0]), encoding="utf-8").read()
             if b7 else "")
    sec7 = body7.split("## 明确不在范围内")[-1] if "## 明确不在范围内" in body7 else body7
    check("自己的产物不算「别的步骤的东西」", "notes/01.md" not in sec7)
    check("「不在范围内」不留重复条目", sec7.count("`tg.py`") <= 1,
          "重复 %d 次" % sec7.count("`tg.py`"))

    # ---------------- 断言超时：既不算通过，也不算破了 ----------------
    # 狗粮实测：把「跑全部四套件」的检查写进断言，done / reconcile 每次都会重跑它，
    # 一次吃掉两三分钟。以前超时被当 FAIL —— 好代码会被冤枉成回归，坏代码则可能被
    # 一个「超时也算失败」的模糊印象糊过去。超时是**没结论**，得单独说。
    with open(os.path.join(root, "slow.py"), "w", encoding="utf-8") as fh:
        fh.write("import time\ntime.sleep(8)\n")
    run(["add", "--title", "慢断言", "--out", "notes/08.md", "--depends", "1",
         "--rounds", "2", "--reuse", "existing",
         "--accept", "cmd:python slow.py"], root)
    r = run(["gate", "8", "--timeout", "1"], root, expect=1, must="TIMEOUT")
    check("超时标成 TIMEOUT（不冒充 FAIL）", "TIMEOUT" in (r.stdout or ""))
    check("超时明说「没验完」", "没验完" in (r.stdout + r.stderr))
    check("超时给放宽时间的命令", "--timeout" in (r.stdout + r.stderr))
    r = run(["gate", "8", "--timeout", "0"], root, must="gate 通过")
    check("--timeout 0 = 不限时，跑完就过", "exit=0" in (r.stdout or ""))
    check("通过了也带耗时（谁在吃时间一眼看见）",
          re.search(r"exit=0\s+\d+\.\d+s", r.stdout or "") is not None,
          (r.stdout or "")[-200:])
    r = run(["done", "8", "--timeout", "1"], root, expect=1, must="不能当通过")
    check("超时时 done 拒绝标记完成", "不能当通过" in (r.stdout + r.stderr))
    check("超时没被记成 done", step(root, 8)["status"] != "done",
          repr(step(root, 8)["status"]))

    # 判「没结论」和判「回归」是两件事：reconcile 里必须分开报
    run(["done", "8", "--force", "--timeout", "1"], root)
    r = run(["reconcile", "--timeout", "1"], root, expect=0, must="没验完")
    check("reconcile 把超时报成「没结论」", "还没结论" in (r.stdout or "")
          or "不知道还算不算通过" in (r.stdout or ""))
    check("reconcile 明说超时不是回归", "不是回归" in (r.stdout or ""))
    check("只有超时时 reconcile 不判失败（exit 0）", r.returncode == 0)

    # 真回归：产物被删 → 报「现在不通过了」，并用非 0 退出（脚本能判）
    with open(os.path.join(root, "notes", "09.md"), "w", encoding="utf-8") as fh:
        fh.write("x\n")
    run(["add", "--title", "有产物的步骤", "--out", "notes/09.md", "--depends", "1",
         "--rounds", "2", "--reuse", "existing",
         "--accept", "file:exists:notes/09.md"], root)
    run(["gate", "9"], root, must="gate 通过")
    run(["done", "9", "--timeout", "60"], root)
    os.remove(os.path.join(root, "notes", "09.md"))
    r = run(["reconcile", "--timeout", "60"], root, expect=1, must="现在不通过了")
    check("真回归用非 0 退出", r.returncode == 1)
    run(["reset", "9"], root)          # 复原，别影响后面那条「reconcile 可用」

    # ---------------- --json 接缝：编排层（orch）唯一的机器接口 ----------------
    # 这一组测的不是「能不能打印 JSON」，而是「不用问一句人话就能驱动它」：
    # 整个 stdout 必须能一次 json.loads、必须带 api 版本号、失败仍然用 exit code 说话。
    def jrun(args, expect):
        r = run(args, root, expect=expect)
        try:
            return r, json.loads(r.stdout)
        except Exception as e:
            check("--json 必须整体可 json.loads：%s" % " ".join(args), False,
                  "%s | stdout=%r" % (e, (r.stdout or "")[:200]))
            return r, {}

    r, d = jrun(["status", "--json"], 0)
    check("status --json 带 api 版本", d.get("api") == 1, repr(d.get("api")))
    check("status --json 报 cmd 名", d.get("cmd") == "status", repr(d.get("cmd")))
    check("status --json 的 plan.id 是活的",
          d.get("plan", {}).get("id") == plan_json(root)["id"])
    check("status --json 的 steps 与 plan 对齐",
          len(d.get("steps", [])) == len(plan_json(root)["steps"]))
    check("status --json 的 doing 一定是列表（并发时会有多个号）",
          isinstance(d.get("doing"), list), repr(d.get("doing")))
    check("status --json 的进度是数不是字符串",
          isinstance(d.get("progress", {}).get("done"), int))

    r, d = jrun(["parallel", "--json"], 0)
    check("parallel --json 给批次列表", isinstance(d.get("batches"), list),
          repr(type(d.get("batches"))))
    check("parallel --json 给 max_fanout（并发路数要靠它拴住）",
          isinstance(d.get("max_fanout"), int), repr(d.get("max_fanout")))
    check("parallel --json 的 conflicts 是列表", isinstance(d.get("conflicts"), list))

    r, d = jrun(["report", "--json"], 0)
    check("report --json 有 totals", isinstance(d.get("totals", {}).get("budget"), int))
    check("report --json 每步带 verdict",
          all("budget_verdict" in s for s in d.get("steps", [])),
          repr(d.get("steps", [])[:1]))
    check("report --json 的 by_session 是列表", isinstance(d.get("by_session"), list))

    # 通过的情况：自己造一个一定通过的步骤，别借前面用例的状态
    with open(os.path.join(root, "notes", "11.md"), "w", encoding="utf-8") as fh:
        fh.write("ok\n")
    run(["add", "--title", "json 成功用例", "--out", "notes/11.md", "--depends", "1",
         "--rounds", "1", "--reuse", "existing",
         "--accept", "file:exists:notes/11.md"], root)
    n_ok = len(plan_json(root)["steps"])
    r, d = jrun(["gate", str(n_ok), "--json"], 0)
    check("gate --json 通过时 ok=true", d.get("ok") is True, repr(d.get("ok")))
    check("gate --json 给 checks 明细",
          isinstance(d.get("checks"), list) and bool(d["checks"]), repr(d.get("checks")))
    check("gate --json 的 exit 字段与真退出码一致", d.get("exit") == 0, repr(d.get("exit")))

    # 失败的情况：--json 也得 exit 1，而且不许把真 FAIL 冒充成超时
    run(["add", "--title", "json 失败用例", "--out", "notes/12.md", "--depends", "1",
         "--rounds", "1", "--reuse", "existing",
         "--accept", "file:exists:notes/绝不存在.txt"], root)
    n_bad = len(plan_json(root)["steps"])
    r, d = jrun(["gate", str(n_bad), "--json"], 1)
    check("gate --json 失败时 ok=false", d.get("ok") is False, repr(d.get("ok")))
    check("gate --json 失败时 exit 字段=1", d.get("exit") == 1, repr(d.get("exit")))
    check("gate --json 真 FAIL 不记成 timeout",
          all(c.get("timeout") is False for c in d.get("checks", [])),
          repr(d.get("checks")))
    check("gate --json 把 failed 与 timed 分开数",
          d.get("failed") == 1 and d.get("timed") == 0,
          "failed=%r timed=%r" % (d.get("failed"), d.get("timed")))

    # 超时：记 timed、不记 failed
    r, d = jrun(["gate", "8", "--timeout", "1", "--json"], 1)
    check("gate --json 超时记进 timed 而不是 failed",
          d.get("timed") == 1 and d.get("failed") == 0,
          "failed=%r timed=%r" % (d.get("failed"), d.get("timed")))
    check("gate --json 单条 check 带 timeout=true",
          any(c.get("timeout") for c in d.get("checks", [])), repr(d.get("checks")))

    # ---------------- C1：doing 可以是多值，但受 --max-fanout 约束 ----------------
    # 这是 A 阶段实测出来的真缺口：两路并发时第二路只能绕过 start、收工直接
    # `sync --step N --session <id>`，那一路就没有 started_ms，归账边界退化成
    # 计划 created。与其让人绕，不如把上限明说出来，并让「不点名就猜」的命令全部闭嘴。
    fan3 = tempfile.mkdtemp(prefix="plfan_")
    subprocess.run("git init -q", cwd=fan3, shell=True, capture_output=True)
    run(["init", "并发"], fan3)
    for t, o in (("A 后端", "api/"), ("B 前端", "web/"), ("C 测试", "tests/")):
        run(["add", "--title", t, "--out", o,
             "--accept", "file:exists:%sx.txt" % o], fan3)

    r = run(["start", "1"], fan3)
    check("第 1 路 start 正常", r.returncode == 0, r.stderr[:120])
    r = run(["start", "2"], fan3)
    check("第 2 路 start 放行（2 路 = 默认上限）", r.returncode == 0, r.stderr[:120])
    check("放行时提醒每路要独立会话、按会话归账",
          "独立会话" in (r.stdout or "") + (r.stderr or ""))
    r = run(["start", "3"], fan3, expect=2)
    check("第 3 路被拒（3 路 > 上限 2）", r.returncode == 2)
    check("被拒时说是 token 不是时间（cost runaway）", "cost runaway" in r.stderr)
    check("被拒时追问「谁看、谁合、合不上怎么办」", "谁看、谁合" in r.stderr)
    check("被拒时给出抬高上限的具体命令", "tg start 3 --max-fanout 3" in r.stderr)
    check("被拒时没人话跑到 stdout（错误只在 stderr）",
          (r.stdout or "").strip() == "", repr((r.stdout or "")[:80]))
    r = run(["start", "3", "--max-fanout", "3"], fan3)
    check("--max-fanout 3 显式放行", r.returncode == 0, r.stderr[:120])

    # 多路 doing 时，不带编号的命令不许「取第一个」——猜错等于给错的步骤发通行证
    r = run(["gate"], fan3, expect=2)
    check("多路 doing 时 gate 不点名就拒", r.returncode == 2)
    check("gate 拒时把候选编号全列出来",
          "#1" in r.stderr and "#3" in r.stderr, r.stderr[:160])
    r = run(["gate", "--json"], fan3, expect=2)
    d3 = json.loads(r.stdout)
    check("gate --json 在多路 doing 时仍是纯 JSON",
          d3.get("ok") is False and d3.get("step") is None, repr(d3)[:160])
    check("gate --json 用 ambiguous 列出候选", d3.get("ambiguous") == [1, 2, 3],
          repr(d3.get("ambiguous")))
    check("gate --json 的 exit 字段与真退出码一致", d3.get("exit") == 2)

    d3 = json.loads(run(["status", "--json"], fan3).stdout)
    check("status --json 的 doing 是三项列表", d3["doing"] == [1, 2, 3],
          repr(d3["doing"]))
    txt = run(["status"], fan3).stdout
    check("status 文字版把并发的步骤全列出来（不只报第一个）",
          "3 步同时在跑" in txt and "#3" in txt, txt[:300])
    txt = run(["resume"], fan3).stdout
    check("resume 不挑一个装成「下一步」",
          "没有「下一步」这个单值" in txt, txt[-200:])
    check("resume 不谎称「全部步骤已完成」", "全部步骤已完成" not in txt)

    # 空分支：零步骤、零 doing 时，--json 的 stdout 也必须是一个 JSON 对象，
    # 而且「没有数据」和「数据是 0」不许混为一谈 —— totals 给 null 而不是 0，
    # 因为 0 会被读成「一分钱没花」，null 才诚实地表示「还没有计划」。
    bare = tempfile.mkdtemp(prefix="pljson_")
    subprocess.run("git init -q", cwd=bare, shell=True, capture_output=True)
    run(["init", "空计划", "--why", "验 --json 的空分支"], bare)
    r = run(["gate", "--json"], bare)
    d2 = json.loads(r.stdout)
    check("没有 doing 步骤时 gate --json 仍给纯 JSON",
          d2.get("api") == 1 and d2.get("step") is None, repr(d2)[:160])
    check("没有可验步骤时 gate --json 的 exit 字段是 0", d2.get("exit") == 0)
    r = run(["report", "--json"], bare)
    check("零步骤时 report --json 的 totals 是 null 而不是 0",
          json.loads(r.stdout)["totals"] is None)
    r = run(["parallel", "--json"], bare)
    check("零步骤时 parallel --json 给空批次",
          json.loads(r.stdout)["batches"] == [])
    # 用法错误：退出码说话，人话进 stderr，stdout 一个字都不许掺
    r = run(["gate", "99", "--json"], bare, expect=2)
    check("--json 下用法错误：stdout 空、stderr 有 ERROR",
          (r.stdout or "").strip() == "" and "ERROR" in (r.stderr or ""),
          "stdout=%r stderr=%r" % ((r.stdout or "")[:80], (r.stderr or "")[:80]))

    # 会话 id 显示：同一批 subagent 的 id 前缀会撞，一律截 12 位就分不清谁是谁，
    # 而「归属错了比不记更坏」。这两个 id 是 A/B 实测里真实撞上的那一对。
    import importlib.util
    spec = importlib.util.spec_from_file_location("tollgate_tg", PL)
    tgmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tgmod)
    two = ["ses_e8ce09f97ffetn6K5ggB3rw27V", "ses_e8ce09f98ffeH9jKpjw8mxL1bE"]
    labels = tgmod.short_sids(two)
    check("短 id 必须彼此可分（撞了要自动加长）",
          labels[two[0]] != labels[two[1]], repr(labels))
    check("短 id 不短于 12 位（别短到看不出是会话）",
          all(len(v) >= 12 for v in labels.values()), repr(labels))
    check("只有一个会话时不必加长",
          list(tgmod.short_sids([two[0]]).values()) == [two[0][:12]])

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
