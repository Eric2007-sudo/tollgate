# -*- coding: utf-8 -*-
"""orch（编排层）端到端测试：角色契约 / 派发前刹车 / 三层汇总。

这一套测的不是「命令能不能跑通」，而是三件一旦松掉就会静默出事的事：

  1. 并发刹车真的刹得住（路数上限、目录重叠、依赖深度）；
  2. `--json` 的 stdout 是**纯**契约，人类提示一律走 stderr ——
     否则下游每读一条命令都得猜哪几行是 JSON；
  3. 语义 verdict **永远**不会变成 done。断言层和语义层分列，
     打架交给人，「没查」必须印成「没查」而不是「没打架」。

跑法：python tests/test_orch.py
退出码 0 = 全绿。
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
TG = os.path.join(BASE, "tg.py")
ORCH = os.path.join(BASE, "orch.py")
FAILED = []
LOGS = []


def oc(args, cwd, expect=0, must=None, absent=None, must_err=None, env_extra=None):
    """跑一条 orch 命令。stdout 和 stderr 分开判 —— 这是本套测试的重点之一。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    r = subprocess.run([sys.executable, ORCH] + args, cwd=cwd, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    so, se = r.stdout or "", r.stderr or ""
    LOGS.append("$ orch.py %s  [exit %d]\n--stdout--\n%s\n--stderr--\n%s"
                % (" ".join(args), r.returncode, so, se))
    bad = []
    if r.returncode != expect:
        bad.append("exit=%d 期望 %d" % (r.returncode, expect))
    if must and must not in so + se:
        bad.append("输出里缺 %r" % must)
    if absent and absent in so + se:
        bad.append("输出里不该有 %r" % absent)
    if must_err and must_err not in se:
        bad.append("stderr 里缺 %r" % must_err)
    if bad:
        FAILED.append("orch %s → %s" % (" ".join(args), "; ".join(bad)))
        print("FAIL  %s   %s" % (" ".join(args)[:64], "; ".join(bad)))
        print(so)
        print(se)
    else:
        print("OK    %s" % " ".join(args)[:78])
    return r


def tg(args, cwd, expect=0):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, TG] + args, cwd=cwd, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != expect:
        FAILED.append("tg %s → exit=%d 期望 %d" % (" ".join(args), r.returncode, expect))
        print("FAIL  tg %s" % " ".join(args))
        print((r.stdout or "") + (r.stderr or ""))
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


def read_text(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def write_json(p, obj):
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def review(root, n, **kw):
    d = {"schema": 1, "step": n, "reviewer": "jev", "verdict": "pass",
         "probs": {"pass": 0.8, "fail": 0.2}, "evidence": [],
         "reviewed_at": "2026-10-25T21:00:00"}
    d.update(kw)
    write_json(os.path.join(root, ".plan", "review-%d.json" % n), d)


def roles_file(root):
    with open(os.path.join(root, ".orch", "roles.json"), encoding="utf-8") as fh:
        return json.load(fh)


def main():
    root = tempfile.mkdtemp(prefix="orch_")
    print("workdir:", root)
    subprocess.run("git init -q && git config user.email a@b.c && git config user.name t",
                   cwd=root, shell=True, capture_output=True)

    # ---------------- 前置：一个真计划 ----------------
    tg(["init", "orch 测试计划", "--why", "编排层得有个真计划才测得出来"], root)
    tg(["add", "--title", "契约", "--out", "contract/CONTRACT.md",
        "--rounds", "2", "--reuse", "existing",
        "--accept", "file:exists:contract/CONTRACT.md"], root)
    os.makedirs(os.path.join(root, "contract"), exist_ok=True)
    with open(os.path.join(root, "contract", "CONTRACT.md"), "w", encoding="utf-8") as fh:
        fh.write("# 契约\n\n字段名以这里为准。\n")
    tg(["start", "1"], root)
    tg(["done", "1"], root)
    tg(["add", "--title", "后端", "--out", "api/", "--rounds", "20",
        "--depends", "1", "--reuse", "new",
        "--accept", "file:exists:api/app.py"], root)
    tg(["add", "--title", "前端", "--out", "web/", "--rounds", "20",
        "--depends", "1", "--reuse", "new",
        "--accept", "file:exists:web/index.html"], root)
    # 故意不给 --accept：零断言步骤在 tg 那边 gate 是「通过」的，
    # orch 的 rollup 必须把它印成「既不算通过也不算破了」。
    tg(["add", "--title", "没断言的步骤", "--out", "tests/", "--rounds", "5",
        "--depends", "1", "--reuse", "new"], root)
    # 第五步是给「坏 review 文件」留的位子：坏文件必须能被报出来，
    # 而不能因为「这一步没有 review 文件」被静默跳过。
    tg(["add", "--title", "数据库", "--out", "db/", "--rounds", "5",
        "--depends", "1", "--reuse", "new",
        "--accept", "file:exists:db/schema.sql"], root)

    # ---------------- 角色：并发的安全边界是目录边界 ----------------
    oc(["role", "add", "backend", "--dir", "api/", "--reviewer", "jev"], root,
       must="已登记角色 backend")
    oc(["role", "add", "frontend", "--dir", "web/", "--note", "只做界面"], root)
    oc(["role", "list"], root, must="reviewer=jev")

    # 目录重叠 = 两个 agent 抢同一批文件 = 不是并发
    r = oc(["role", "add", "sneaky", "--dir", "api/sub/"], root, expect=2,
           must="重叠")
    check("目录重叠被拒时说清「不是可能出现，是什么时候出现」",
          "什么时候出现" in r.stderr, r.stderr[:200])

    oc(["role", "add", "BadName", "--dir", "misc/"], root, expect=2,
       must="角色名只许小写")
    oc(["role", "add", "x1", "--dir", "misc/", "--reviewer", "gpt9"], root, expect=2,
       must="--reviewer 只能是")
    oc(["role", "add", "empty", "--dir", "  "], root, expect=2, must="不许为空")

    oc(["role", "list", "--json"], root)
    r = oc(["role", "list", "--json"], root)
    d = json.loads(r.stdout)
    check("role list --json 是纯 JSON", d.get("api") == 1 and "roles" in d)
    check("role list --json 带并发上限", d.get("max_fanout") == 2, repr(d.get("max_fanout")))
    check("角色表里没有被拒的重叠角色",
          sorted(x["name"] for x in d["roles"]) == ["backend", "frontend"],
          repr([x["name"] for x in d["roles"]]))

    # ---------------- fan：派发前的四道刹车 ----------------
    oc(["fan", "99"], root, expect=2, must="计划里没有 #99")
    oc(["fan", "1"], root, expect=2, must="已经是 done")
    oc(["fan", "2", "--roles", "backend,nosuch"], root, expect=2, must="没登记过")

    # ① 路数上限：3 路 > 默认 2 路
    oc(["role", "add", "tester", "--dir", "tests/"], root)
    r = oc(["fan", "2", "--roles", "backend,frontend,tester"], root, expect=2,
           must="上限是 2 路", must_err="cost runaway")
    check("刹车被踩时问的是「谁看、谁合、合不上怎么办」，不是「加个参数就行」",
          "谁看、谁合" in r.stderr, r.stderr[:300])

    # 默认取全部已登记角色 —— 角色一多，自己的刹车会先拦住
    oc(["fan", "2"], root, expect=2, must="上限是 2 路")

    # ② 依赖深度：把上限压到 1，深度 2 的步骤必须被拦
    st = roles_file(root)
    st["max_depth"] = 1
    write_json(os.path.join(root, ".orch", "roles.json"), st)
    oc(["fan", "2", "--roles", "backend"], root, expect=2,
       must="依赖链深度 2 > 上限 1")
    st = roles_file(root)
    st["max_depth"] = 3
    write_json(os.path.join(root, ".orch", "roles.json"), st)

    # ③ 契约：显式指定不存在的必须直接拒（不能悄悄降级成「没契约」）
    oc(["fan", "2", "--roles", "backend", "--contract", "contract/NOPE.md"], root,
       expect=2, must="契约文件不存在")

    # ④ 正常放行：生成每人一份自足式简报
    r = oc(["fan", "2", "--roles", "backend,frontend"], root, must="已生成 2 份简报")
    bdir = os.path.join(root, ".orch", "briefs")
    b1 = os.path.join(bdir, "2-backend.md")
    b2 = os.path.join(bdir, "2-frontend.md")
    check("每个角色一份简报", os.path.isfile(b1) and os.path.isfile(b2))
    body = read_text(b1)
    check("简报写死角色只能碰自己的目录", "`api/`" in body)
    check("简报点名契约文件（共同的唯一事实来源）", "contract/CONTRACT.md" in body)
    check("简报列了别的角色的目录作为禁区", "web/" in body)
    check("简报带会真跑的验收断言", "file:exists:api/app.py" in body)
    check("简报给了归账命令", "tg sync --step 2 --session" in body)
    check("简报明说不要自己 tg done", "不要**自己调 `tg done`" in body)
    check("简报要求偏离契约时停下来汇报", "停下来汇报" in body)
    check("两份简报内容不同（不是复制粘贴）", read_text(b2) != body)

    # --json 纯度：stdout 只有 JSON，警告走 stderr 也进 payload
    r = oc(["fan", "2", "--roles", "backend,frontend", "--json"], root,
           must_err="doing")
    d = json.loads(r.stdout)
    check("fan --json 的 stdout 是纯 JSON（人类提示都在 stderr）",
          d.get("api") == 1 and d.get("cmd") == "fan")
    check("fan --json 把警告也放进 payload", len(d.get("warnings") or []) >= 1)
    check("fan --json 报出依赖深度", d.get("depth") == 2, repr(d.get("depth")))
    check("fan --json 给每路一条 dispatch",
          [x["role"] for x in d["dispatch"]] == ["backend", "frontend"])
    check("dispatch 里的 gate 命令是 argv 形式（调用方不用拆字符串）",
          d["dispatch"][0]["gate_cmd"] == ["gate", "2", "--json"])
    check("fan 不调模型、不派 agent：输出里只有命令，没有模型名",
          "deepseek" not in r.stdout and "gpt" not in r.stdout)

    # ---------------- rollup：三层必须分开说 ----------------
    os.makedirs(os.path.join(root, "api"), exist_ok=True)
    with open(os.path.join(root, "api", "app.py"), "w", encoding="utf-8") as fh:
        fh.write("x = 1\n")

    review(root, 1, human_verified=True, confidence=0.9)
    # 断言能过，但 reviewer 说 fail → 必须被抓成「打架」
    review(root, 2, verdict="fail", probs={"pass": 0.2, "fail": 0.8})
    # 坏文件三种：reviewer 非法 / 缺字段 / JSON 本身坏了
    review(root, 3, reviewer="gpt9")
    write_json(os.path.join(root, ".plan", "review-4.json"), {"step": 4})
    with open(os.path.join(root, ".plan", "review-5.json"), "w", encoding="utf-8") as fh:
        fh.write("{不是 json")

    before = {s["n"]: s["status"] for s in plan_json(root)["steps"]}
    r = oc(["rollup", "--gate", "--cost"], root)
    text = r.stdout
    check("rollup 三层分列（断言 / 语义 / 人）",
          "第 1 层：断言" in text and "第 2 层：语义" in text and "第 3 层：人" in text)
    check("零断言步骤不冒充「通过」",
          "无断言 —— 既不算通过，也不算破了" in text,
          text[:400])
    check("rollup 抓到「断言过了但 reviewer 说 fail」", "断言过了，但 reviewer 说 fail" in text)
    check("打架交给人的原话在", "不许「谁说的算」和稀泥" in text)
    check("非法 reviewer 被报出来（不是静默略过）", "不在允许列表里" in text)
    check("缺字段的 review 被报出来", "缺字段" in text)
    check("坏 JSON 的 review 被报出来", "解析不了" in text)
    check("jev 双轨期会说清抽查没做全就不许当 done 依据",
          "不许**当 done 的依据" in text)
    check("钱那节明说账本是下限", "下限" in text)

    after = {s["n"]: s["status"] for s in plan_json(root)["steps"]}
    check("rollup 一个字的 verdict 都没改计划状态（pass 不会变成 done）",
          before == after, "%r → %r" % (before, after))

    # 不给 --gate：打架检测必须明说「没查」，不能报 0 个打架冒充清白
    r = oc(["rollup"], root, must="没查")
    check("不重跑 gate 时不说「没有打架」", "没有打架" not in r.stdout)
    check("不重跑 gate 时点明「没查不等于没打架」", "没查」不等于「没打架" in r.stdout)

    r = oc(["rollup", "--json"], root)
    d = json.loads(r.stdout)
    check("rollup --json 是纯 JSON", d.get("api") == 1 and d.get("cmd") == "rollup")
    check("rollup --json 标出冲突检测被跳过",
          d.get("conflict_check") == "skipped_no_gate", repr(d.get("conflict_check")))
    check("rollup --json 统计双轨抽查次数",
          d.get("jev_dual_track", {}).get("spotchecked") == 1,
          repr(d.get("jev_dual_track")))

    r = oc(["rollup", "--gate", "--json"], root)
    d = json.loads(r.stdout)
    check("rollup --gate --json 里 conflicts 非空",
          len(d.get("conflicts") or []) == 1, repr(d.get("conflicts")))
    check("rollup --json 带 note：verdict 只是概率建议",
          "exit code" in d.get("note", ""))

    # ---------------- 契约版本：tg 换了 api 就得停 ----------------
    fake = os.path.join(root, "fake_tg.py")
    with open(fake, "w", encoding="utf-8") as fh:
        fh.write("import json, sys\n"
                 "print(json.dumps({'api': 99, 'cmd': 'status', 'steps': []}))\n")
    oc(["role", "list"], root, env_extra={"ORCH_TG": fake})
    oc(["fan", "2", "--roles", "backend"], root, expect=2,
       must="orch 只认 1", env_extra={"ORCH_TG": fake})
    oc(["fan", "2", "--roles", "backend"], root, expect=2,
       must="ORCH_TG 指向的文件不存在",
       env_extra={"ORCH_TG": os.path.join(root, "no_such_tg.py")})

    # 没有计划的地方：直接拒，不要瞎猜
    bare = tempfile.mkdtemp(prefix="orch_bare_")
    oc(["role", "add", "x", "--dir", "a/"], bare, expect=2, must="没有 .plan/plan.json")

    r = oc(["--version"], root, must="不调模型")
    check("--version 把定位写清楚（不调模型、不管进程）", "不管进程" in r.stdout)

    # ---------------- 收尾 ----------------
    print("\n==== %d 项失败 ====" % len(FAILED))
    for f in FAILED:
        print("  -", f)
    log = os.path.join(root, ".orch", "test-orch-output.txt")
    try:
        os.makedirs(os.path.dirname(log), exist_ok=True)
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
