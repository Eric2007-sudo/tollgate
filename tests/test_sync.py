# -*- coding: utf-8 -*-
"""tg sync 的离线回归：把宿主逐轮用量对账进账本。

用构造出来的同构 sqlite 库（session_v2 + session_message），完全不碰真实宿主数据。
跑法：python tests/test_sync.py     退出码 0 = 全绿。
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TG = os.path.join(ROOT, "tg.py")

sys.stdout.reconfigure(encoding="utf-8")
FAILED = []

MODEL = ("kouxian", "deepseek-v4.1-flash")
PRICE = {"input": 0.1, "output": 0.4, "cache_read": 0.01, "cache_write": 0.1}


def expect(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def make_config(path, context=128000):
    cfg = {"$schema": "https://opencode.ai/config.json",
           "providers": {"kouxian": {"package": "x", "models": {
               "deepseek-v4.1-flash": {
                   "limit": {"context": context, "output": 8192},
                   "cost": {"input": PRICE["input"], "output": PRICE["output"],
                            "cache": {"read": PRICE["cache_read"],
                                      "write": PRICE["cache_write"]}},
               }}}}}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)


def make_db(path, turns, sid="ses_test", model=MODEL):
    """turns: [(input, cache_read, output, reasoning, cost), ...] 时间升序。

    每条消息的 id 是 msg_000 / msg_001 …，重写库时 id 保持稳定 —— 这正是测
    「游标不重复记账」需要的前提。
    """
    if os.path.isfile(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript("""
        create table session_v2 (
            id text, model text, cost real, tokens_input int, tokens_output int,
            tokens_reasoning int, tokens_cache_read int, tokens_cache_write int,
            time_created int, time_updated int);
        create table session_message (
            id text, session_id text, type text, seq int,
            time_created int, time_updated int, data text);
    """)
    con.execute(
        "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?)",
        (sid, json.dumps({"id": model[1], "providerID": model[0]}),
         sum(t[4] for t in turns), sum(t[0] for t in turns),
         sum(t[2] for t in turns), sum(t[3] for t in turns),
         sum(t[1] for t in turns), 0, 1, 1000))
    for i, (tin, tcr, tout, trea, cost) in enumerate(turns):
        data = {"model": {"id": model[1], "providerID": model[0]},
                "tokens": {"input": tin, "output": tout, "reasoning": trea,
                           "cache": {"read": tcr, "write": 0}},
                "cost": cost}
        con.execute(
            "insert into session_message values (?,?,?,?,?,?,?)",
            ("msg_%03d" % i, sid, "assistant", i, 100 + i, 100 + i,
             json.dumps(data)))
    con.commit()
    con.close()


def run(tmp, args, sid="ses_test"):
    env = dict(os.environ)
    env["TOLLGATE_DB"] = os.path.join(tmp, "fake.db")
    env["TOLLGATE_CONFIG"] = os.path.join(tmp, "opencode.json")
    env["OPENCODE_SESSION_ID"] = sid
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, TG] + args, cwd=tmp, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def run_json(tmp, args, sid="ses_test"):
    r = run(tmp, args + ["--json"], sid)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        print("输出不是 JSON：\n" + r.stdout)
        return None


def ledger(tmp):
    p = os.path.join(tmp, ".plan", "ledger.jsonl")
    if not os.path.isfile(p):
        return []
    rows = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def sync_state(tmp):
    p = os.path.join(tmp, ".plan", "sync.json")
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8-sig") as fh:
        return json.load(fh)


def case(name, turns, budget=100000, rounds=10, extra=None):
    """建一个「计划 + 一步 doing + 假宿主库」的临时仓库。"""
    print("\n== %s ==" % name)
    tmp = tempfile.mkdtemp(prefix="tgsync_")
    make_config(os.path.join(tmp, "opencode.json"))
    make_db(os.path.join(tmp, "fake.db"), turns)
    r = run(tmp, ["init", "对账测试"])
    if r.returncode != 0:
        print(r.stdout + r.stderr)
    run(tmp, ["add", "--title", "干活", "--out", "out/a.txt",
              "--budget", str(budget), "--rounds", str(rounds)])
    for a in (extra or []):
        run(tmp, a)
    run(tmp, ["start", "1"])
    return tmp


TURNS3 = [
    (1000, 50000, 200, 50, 0.0003),
    (1200, 52000, 300, 100, 0.0004),
    (900, 54000, 150, 0, 0.0002),
]


def main():
    # ---------- 1. 基本对账：in / out(含推理) / 轮数 / 钱 ----------
    tmp = case("1 基本对账", TURNS3)
    d = run_json(tmp, ["sync"])
    expect(d is not None, "sync --json 能跑通")
    if d:
        expect(d["fresh"] == 3, "同步到 3 轮（实际 %s）" % d.get("fresh"))
        expect(d["synced"]["in"] == 3100, "in = Σinput = 3100（实际 %s）" % d["synced"]["in"])
        expect(d["synced"]["out"] == 650, "out = Σoutput = 650（实际 %s）" % d["synced"]["out"])
        expect(d["synced"]["reasoning"] == 150, "reasoning 单列 = 150（实际 %s）"
               % d["synced"]["reasoning"])
        expect(d["synced"]["cache_read"] == 156000,
               "cache_read 单列 = 156000（实际 %s）" % d["synced"]["cache_read"])
        expect(abs(d["cost"] - 0.0009) < 1e-12, "钱 = 宿主三笔之和 0.0009（实际 %s）" % d["cost"])
        expect(d["cost_src"] == "宿主记录", "钱的口径标为「宿主记录」")
        # 账本口径必须和 spend 一致：tok 数 = in + out + reasoning，不含 cache_read
        expect(d["step_total"]["tokens"] == 3100 + 650 + 150,
               "步骤累计 = 3900（in+out+推理，不含缓存读；实际 %s）"
               % d["step_total"]["tokens"])
        expect(not d["over_budget"], "没超预算")
    rows = ledger(tmp)
    expect(len(rows) == 1 and rows[0]["step"] == 1, "账本只写了一笔，归到 #1")
    if rows:
        expect(rows[0]["reqs"] == 3, "账本记了 3 轮")
        expect(rows[0]["src"] == "opencode.db", "账本标了数据源 opencode.db")
        expect("cost" in rows[0] and not rows[0].get("cost_est"),
               "账本存了宿主真实 cost，没标估算")

    # ---------- 2. 幂等：同一批轮次不会被记第二遍 ----------
    d = run_json(tmp, ["sync"])
    expect(d is not None and d["fresh"] == 0, "再 sync 一次：没有新轮次")
    expect(len(ledger(tmp)) == 1, "账本仍然只有 1 笔（实际 %d）" % len(ledger(tmp)))
    st = sync_state(tmp)
    expect(len(st.get("ids") or []) == 3, "游标记了 3 条消息 id（实际 %d）"
           % len(st.get("ids") or []))

    # ---------- 3. 新轮次只记增量，不重复记旧的 ----------
    make_db(os.path.join(tmp, "fake.db"),
            TURNS3 + [(2000, 56000, 400, 100, 0.0006)])
    d = run_json(tmp, ["sync"])
    expect(d is not None and d["fresh"] == 1, "只同步新增的 1 轮（实际 %s）"
           % (d or {}).get("fresh"))
    if d:
        expect(d["synced"]["in"] == 2000, "增量只算新一轮的 in=2000（实际 %s）"
               % d["synced"]["in"])
        expect(d["step_total"]["tokens"] == 3900 + 2000 + 400 + 100,
               "步骤累计 6400（实际 %s）" % d["step_total"]["tokens"])
        expect(d["step_total"]["reqs"] == 4, "累计 4 轮（实际 %s）" % d["step_total"]["reqs"])

    # ---------- 4. --dry-run：报数但不写账本、不动游标 ----------
    make_db(os.path.join(tmp, "fake.db"),
            TURNS3 + [(2000, 56000, 400, 100, 0.0006),
                      (1500, 58000, 250, 0, 0.0003)])
    before_rows = len(ledger(tmp))
    before_ids = len(sync_state(tmp).get("ids") or [])
    d = run_json(tmp, ["sync", "--dry-run"])
    expect(d is not None and d["fresh"] == 1 and d["dry_run"], "dry-run 报出 1 轮待记")
    expect(len(ledger(tmp)) == before_rows, "dry-run 后账本没变（%d 笔）" % len(ledger(tmp)))
    expect(len(sync_state(tmp).get("ids") or []) == before_ids,
           "dry-run 后游标没动（%d 条）" % len(sync_state(tmp).get("ids") or []))
    d = run_json(tmp, ["sync"])
    expect(d is not None and d["fresh"] == 1, "dry-run 之后再 sync，那一轮仍然记得到")

    # ---------- 5. 没有 doing 步骤：拒绝归账（宁可不记，也不记到错的地方） ----------
    tmp2 = tempfile.mkdtemp(prefix="tgsync_")
    make_config(os.path.join(tmp2, "opencode.json"))
    make_db(os.path.join(tmp2, "fake.db"), TURNS3)
    run(tmp2, ["init", "没有 doing 的计划"])
    run(tmp2, ["add", "--title", "还没开始", "--out", "out/b.txt"])
    r = run(tmp2, ["sync"])
    expect(r.returncode != 0, "无 doing 步骤时 sync 以非 0 退出（实际 %d）" % r.returncode)
    expect("doing" in (r.stdout + r.stderr), "错误里说明「没有 doing 的步骤」")
    expect(len(ledger(tmp2)) == 0, "拒绝归账时账本没被写脏")

    # ---------- 6. --step 显式指定 ----------
    tmp3 = case("6 --step 指定归账", TURNS3)
    run(tmp3, ["add", "--title", "第二步", "--out", "out/c.txt"])
    d = run_json(tmp3, ["sync", "--step", "2"])
    expect(d is not None and d["step"] == 2, "记到 #2（实际 %s）" % (d or {}).get("step"))
    rows = ledger(tmp3)
    expect(len(rows) == 1 and rows[0]["step"] == 2, "账本那笔的 step = 2")

    # ---------- 7. 宿主没记 cost：按配置单价重算，并标明是估算 ----------
    tmp4 = case("7 无 cost 时按价目重算",
                [(1000, 0, 100, 0, 0.0), (1000, 0, 100, 0, 0.0)])
    d = run_json(tmp4, ["sync"])
    expect(d is not None and d["cost_src"].startswith("按配置单价重算"),
           "口径标成重算（实际 %s）" % (d or {}).get("cost_src"))
    if d and d["cost"] is not None:
        want = (2000 * PRICE["input"] + 200 * PRICE["output"]) / 1e6
        expect(abs(d["cost"] - want) < 1e-12,
               "重算值 %.9f == 手算 %.9f" % (d["cost"], want))
        expect(d["record"].get("cost_est") is True, "记录里标了 cost_est")

    # ---------- 8. 超预算/超轮次要报警，且说得清是缺哪一样 ----------
    tmp5 = case("8 超预算报警", TURNS3, budget=1000, rounds=1)
    r = run(tmp5, ["sync"])
    expect(r.returncode == 0, "超预算本身不阻断（sync 只报数）")
    expect("超预算" in r.stdout + r.stderr, "输出里有「超预算」警告")
    expect("超过本步预算" in r.stdout + r.stderr, "输出里也有轮数超预算的警告")

    # ---------- 9. 游标坏掉时从头算，而不是崩 ----------
    tmp6 = case("9 游标损坏", TURNS3)
    run(tmp6, ["sync"])
    with open(os.path.join(tmp6, ".plan", "sync.json"), "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 json")
    d = run_json(tmp6, ["sync"])
    expect(d is not None, "游标文件损坏后 sync 仍能跑")
    expect(d is not None and d["fresh"] == 3, "从头重算 3 轮（实际 %s）"
           % (d or {}).get("fresh"))

    # ---------- 10. 换会话：旧会话的游标不能吃掉新会话的轮次 ----------
    tmp7 = case("10 换会话", TURNS3)
    run(tmp7, ["sync"])
    make_db(os.path.join(tmp7, "fake.db"),
            [(700, 0, 70, 0, 0.0001), (800, 0, 80, 0, 0.0001)], sid="ses_other")
    d = run_json(tmp7, ["sync", "--session", "ses_other"])
    expect(d is not None and d["fresh"] == 2, "新会话的 2 轮照记（实际 %s）"
           % (d or {}).get("fresh"))
    expect(sync_state(tmp7).get("session") == "ses_other", "游标跟着换到新会话")

    # ---------- 11. report 里能看见真实钱，而不是只有 token ----------
    tmp8 = case("11 report 显示真实账", TURNS3)
    run(tmp8, ["sync"])
    r = run(tmp8, ["report"])
    body = r.stdout + r.stderr
    expect(r.returncode == 0, "report 正常结束")
    expect("$0.000900" in body, "report 里出现宿主记录的真实金额 $0.000900")
    expect("全部来自宿主记录" in body, "report 明说这笔钱不是估算的")
    expect("账本里没有 cost" not in body, "有 cost 时不再提示「没有 cost」")

    # ---------- 12. 只有 token 没有钱时，report 不许把 token 当钱 ----------
    tmp9 = case("12 只有 token 没有钱", TURNS3)
    run(tmp9, ["spend", "1", "--in", "3000", "--out", "300", "--reqs", "2"])
    r = run(tmp9, ["report"])
    body = r.stdout + r.stderr
    expect("账本里没有 cost" in body, "report 明说账本里没有 cost")
    expect("tg sync" in body, "并指路到 sync")

    # ---------- 13. done 时提醒没对账的步骤（闸一收尾顺手拉闸二一把） ----------
    tmp10 = case("13 done 提醒对账", TURNS3)
    r = run(tmp10, ["done", "1"])
    expect(r.returncode == 0, "没有 accept 断言时 done 直接过")
    expect("没有真实用量" in (r.stdout + r.stderr), "done 提醒先跑 tg sync")

    tmp11 = case("14 对账后 done 不再唠叨", TURNS3)
    run(tmp11, ["sync"])
    r = run(tmp11, ["done", "1"])
    expect(r.returncode == 0, "sync 之后 done 仍然过")
    expect("没有真实用量" not in (r.stdout + r.stderr), "sync 之后 done 不再提醒")

    # ---------- 15. 长会话不许把「窗口大小」谎报成「第 N 轮」 ----------
    tmp12 = tempfile.mkdtemp(prefix="tgsync_")
    make_config(os.path.join(tmp12, "opencode.json"))
    make_db(os.path.join(tmp12, "fake.db"),
            [(500, 1000 * (i + 1), 50, 0, 0.0001) for i in range(45)])
    r = run(tmp12, ["ctx"])
    body = r.stdout + r.stderr
    expect(r.returncode == 0, "ctx 在 45 轮的长会话上正常结束")
    expect("第 40 轮上下文" not in body, "不再把「读到的第 40 条」谎报成「第 40 轮」")
    expect("最近 40 轮" in body, "改成说清「这里只看最近 40 轮」")

    print("\n" + ("全绿。" if not FAILED else "失败 %d 项：" % len(FAILED)))
    for m in FAILED:
        print("  - " + m)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
