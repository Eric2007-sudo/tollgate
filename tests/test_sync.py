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
import time

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


def now_ms():
    """夹具里的轮次时间戳必须用「刚刚」，不能用 100 / 200 这种小整数。

    `tg sync` 会按「这一步开始的时刻」划归账边界（`started_ms`），秒级小整数会被
    归一成 1970 年，于是每一轮都落在边界之前、全被跳过 —— 那样测的就不是对账了。
    """
    return int(time.time() * 1000)


def iso_ms(ms):
    """epoch 毫秒 → 计划里 `created` 那种秒精度时间串（夹具要用）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ms / 1000.0))


def make_db(path, turns, sid="ses_test", model=MODEL, t0=None):
    """turns: [(input, cache_read, output, reasoning, cost), ...] 时间升序。

    每条消息的 id 是 msg_000 / msg_001 …，重写库时 id 保持稳定 —— 这正是测
    「游标不重复记账」需要的前提。
    """
    if t0 is None:
        t0 = now_ms()
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
         sum(t[1] for t in turns), 0, t0 - len(turns) * 1000, t0))
    for i, (tin, tcr, tout, trea, cost) in enumerate(turns):
        data = {"model": {"id": model[1], "providerID": model[0]},
                "tokens": {"input": tin, "output": tout, "reasoning": trea,
                           "cache": {"read": tcr, "write": 0}},
                "cost": cost}
        # 时间戳都落在「刚刚」之前（每秒一轮），不能有「未来」的轮次：
        # 否则把边界挪到当下的用例会漏掉它们，测的就不是边界了。
        ts = t0 - (len(turns) - 1 - i) * 1000
        con.execute(
            "insert into session_message values (?,?,?,?,?,?,?)",
            ("msg_%03d" % i, sid, "assistant", i, ts, ts,
             json.dumps(data)))
    con.commit()
    con.close()


def add_session(path, sid, turns, prefix="msgB", t0=None):
    """往同一个库里再塞一个会话。make_db 会删库，所以测多会话得自己加。

    t0 默认「刚刚」：轮次必须落在这一步开始之后，否则会被归账边界挡住。
    """
    if t0 is None:
        t0 = now_ms()
    con = sqlite3.connect(path)
    con.execute(
        "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?)",
        (sid, json.dumps({"id": MODEL[1], "providerID": MODEL[0]}),
         sum(t[4] for t in turns), sum(t[0] for t in turns),
         sum(t[2] for t in turns), sum(t[3] for t in turns),
         sum(t[1] for t in turns), 0, t0 - len(turns) * 1000, t0))
    for i, (tin, tcr, tout, trea, cost) in enumerate(turns):
        data = {"model": {"id": MODEL[1], "providerID": MODEL[0]},
                "tokens": {"input": tin, "output": tout, "reasoning": trea,
                           "cache": {"read": tcr, "write": 0}},
                "cost": cost}
        ts = t0 - (len(turns) - 1 - i) * 1000
        con.execute(
            "insert into session_message values (?,?,?,?,?,?,?)",
            ("%s_%03d" % (prefix, i), sid, "assistant", i, ts, ts,
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
    """建一个「计划 + 一步 doing + 假宿主库」的临时仓库。

    顺序与时间戳都有讲究：`tg sync` 只认「这一步开始之后」的轮次，所以
    1) 先 `start`（它写下 `started_ms`），再造库；
    2) 库里的轮次落在「刚刚之前的 n 秒」——都在过去，且都在 `started_ms` 之后；
    3) 把 `started_ms` / `created` 一起挪到这些轮次之前，夹具才不依赖「跑得多快」。
    """
    print("\n== %s ==" % name)
    tmp = tempfile.mkdtemp(prefix="tgsync_")
    make_config(os.path.join(tmp, "opencode.json"))
    r = run(tmp, ["init", "对账测试"])
    if r.returncode != 0:
        print(r.stdout + r.stderr)
    run(tmp, ["add", "--title", "干活", "--out", "out/a.txt",
              "--budget", str(budget), "--rounds", str(rounds)])
    for a in (extra or []):
        run(tmp, a)
    run(tmp, ["start", "1"])
    t0 = now_ms()
    make_db(os.path.join(tmp, "fake.db"), turns, t0=t0)
    pj = os.path.join(tmp, ".plan", "plan.json")
    with open(pj, encoding="utf-8") as fh:
        plan = json.load(fh)
    plan["steps"][0]["started_ms"] = t0 - (len(turns) + 1) * 1000
    plan["created"] = iso_ms(t0 - (len(turns) + 2) * 1000)
    with open(pj, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False)
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
    expect(((d or {}).get("cursor") or {}).get("sessions") == 2,
           "换会话时旧会话的游标也留着（各记各的，回来不重记）")

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

    # ---------- 16. init --force 清账本但不清游标：既不重记也不补记 ----------
    # 这条是 references/anti-patterns.md #24 与 plan-format.md「删文件前先看这张表」
    # 里写下的承诺，所以必须被钉死：init --force 只 unlink ledger 与 gates，
    # sync.json 留着，于是新账本从「游标之后」开始记。
    tmp13 = case("16 force 重建计划", TURNS3)
    run(tmp13, ["sync"])
    before = sync_state(tmp13).get("ids") or []
    expect(len(before) == 3, "先同步出 3 条游标（实际 %d）" % len(before))
    run(tmp13, ["init", "换个目标", "--force"])
    run(tmp13, ["add", "--title", "新活", "--out", "out/b.txt",
                "--budget", "100000", "--rounds", "10"])
    run(tmp13, ["start", "1"])
    # 这一步测的是「游标还在 → 不重记」，所以刻意把归账边界挪到很早，
    # 让边界机制不参与判断（否则 fresh=0 可能只是因为轮次比计划还早）。
    pj13 = os.path.join(tmp13, ".plan", "plan.json")
    with open(pj13, encoding="utf-8") as fh:
        plan13 = json.load(fh)
    plan13["steps"][0]["started_ms"] = 1
    with open(pj13, "w", encoding="utf-8") as fh:
        json.dump(plan13, fh, ensure_ascii=False)
    expect(not os.path.isfile(os.path.join(tmp13, ".plan", "ledger.jsonl")),
           "init --force 清掉了 ledger.jsonl")
    expect(len(sync_state(tmp13).get("ids") or []) == 3,
           "但游标原样保留（3 条 id）")
    d2 = run_json(tmp13, ["sync"])
    expect(d2 is not None and d2.get("fresh") == 0,
           "重建后 sync 不重记已入账的轮次（实际 %s）" % (d2 or {}).get("fresh"))
    expect(ledger(tmp13) == [], "新账本没有被旧轮次污染")

    # ---------- 17. 同期还有别的会话在花钱：账本只是下限，必须说出来 ----------
    # 没 sync 过的会话，它的花费不在账本里。分摊账单时这个下限很实在 ——
    # 所以 sync 要给一句提示，并指出去哪补，而不是让用户自己发现。
    tmp14 = case("17 别的会话也在花钱", TURNS3)
    con = sqlite3.connect(os.path.join(tmp14, "fake.db"))
    later_ms = int(time.time() * 1000) + 1000
    con.execute(
        "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?)",
        ("ses_other", json.dumps({"id": "deepseek-v4.1-flash",
                                  "providerID": "kouxian"}),
         0.005, 40000, 900, 100, 50000, 0, 1, later_ms))
    con.commit()
    con.close()
    d3 = run_json(tmp14, ["sync"])
    others = (d3 or {}).get("others") or {}
    expect(others.get("count") == 1,
           "报出 1 个同期会话（实际 %s）" % others.get("count"))
    expect(abs((others.get("usd") or 0) - 0.005) < 1e-12,
           "并报出它的花费 0.005（实际 %s）" % others.get("usd"))
    expect(others.get("sessions") == ["ses_other"], "会话 id 也在里面")
    r = run(tmp14, ["sync"])          # 第二次：没有新轮次，但这句提示不能消失
    body = r.stdout + r.stderr
    expect("它们还没入账" in body, "没有新轮次时也提醒别的会话没入账")
    expect("tg sync --session" in body, "并指出去哪补（逐个会话各 sync 一次）")

    # 计划里读不到建立时间时，宁可不报「同期」。把全部历史会话算成同期是假数，
    # 比不报更坏 —— 这条规则要钉住，别哪天顺手改成「不知道就全算」。
    pj = os.path.join(tmp14, ".plan", "plan.json")
    with open(pj, encoding="utf-8") as fh:
        plan_obj = json.load(fh)
    plan_obj.pop("created", None)
    with open(pj, "w", encoding="utf-8") as fh:
        json.dump(plan_obj, fh, ensure_ascii=False)
    d4 = run_json(tmp14, ["sync"])
    expect(((d4 or {}).get("others") or {}).get("count") == 0,
           "计划没有 created 时不计同期会话（实际 %s）"
           % ((d4 or {}).get("others") or {}).get("count"))
    r = run(tmp14, ["sync"])
    expect("它们还没入账" not in (r.stdout + r.stderr),
           "也不打印那行提示（宁可不报，不猜）")

    # ---------- 18. 交替在两个会话里对账：不许重记 ----------
    # 这是真的会双计的路径：游标只存一份、换会话就清空，于是 A→B→A 把 A 整段重记。
    # 实测（改之前）应有 4 轮 / $0.0011，账本记成 7 轮 / $0.0014。
    tmp15 = case("18 交替会话不重记", TURNS3)
    add_session(os.path.join(tmp15, "fake.db"), "ses_B",
                [(500, 0, 50, 0, 0.0002)])
    dA1 = run_json(tmp15, ["sync"], sid="ses_test")
    expect((dA1 or {}).get("fresh") == 3, "会话 A 首次记 3 轮（实际 %s）"
           % (dA1 or {}).get("fresh"))
    dB1 = run_json(tmp15, ["sync"], sid="ses_B")
    expect((dB1 or {}).get("fresh") == 1, "会话 B 首次记 1 轮（实际 %s）"
           % (dB1 or {}).get("fresh"))
    dA2 = run_json(tmp15, ["sync"], sid="ses_test")
    expect((dA2 or {}).get("fresh") == 0, "回到 A 时一轮都不重记（实际 %s）"
           % (dA2 or {}).get("fresh"))
    rows15 = ledger(tmp15)
    expect(sum(r["reqs"] for r in rows15) == 4,
           "账本合计 4 轮（实际 %d）" % sum(r["reqs"] for r in rows15))
    expect(abs(sum(r.get("cost") or 0 for r in rows15) - 0.0011) < 1e-9,
           "账本合计 $0.0011（实际 %.6f）" % sum(r.get("cost") or 0 for r in rows15))
    st15 = sync_state(tmp15)
    expect(st15.get("version") == 2 and len(st15.get("sessions") or {}) == 2,
           "sync.json 是 v2，两个会话各有一份游标")

    # v1 游标（只有顶层 session + ids）读的时候要能升上来：老账本不该因为升级而重记
    tmp16 = case("18b v1 游标升上来", TURNS3)
    with open(os.path.join(tmp16, ".plan", "sync.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"session": "ses_test", "ids": ["msg_000", "msg_001"],
                   "updated": "2026-01-01T00:00:00"}, fh)
    d16 = run_json(tmp16, ["sync"], sid="ses_test")
    expect((d16 or {}).get("fresh") == 1, "v1 游标里那 2 轮不重记，只补剩下的 1 轮（实际 %s）"
           % (d16 or {}).get("fresh"))
    st16 = sync_state(tmp16)
    expect(st16.get("version") == 2 and len(st16.get("sessions") or {}) == 1,
           "写完就升成 v2 形状，那个会话带着 3 条 id")

    # ---------- 19. 账本行带会话来源 + report 按会话分列 ----------
    # 一份账本能装多个会话之后，"这笔钱是谁的"必须有答案。sync 写行时带上会话 id，
    # report 按会话分列；旧行（没有这个字段）单独报，不猜它属于哪个会话。
    tmp17 = case("19 账本行带来源", TURNS3)
    add_session(os.path.join(tmp17, "fake.db"), "ses_B",
                [(500, 0, 50, 0, 0.0002)])
    run_json(tmp17, ["sync"], sid="ses_test")
    run_json(tmp17, ["sync"], sid="ses_B")
    rows17 = ledger(tmp17)
    expect(rows17 and all(r.get("session") for r in rows17),
           "每一笔 sync 记录都带会话 id")
    expect({r.get("session") for r in rows17} == {"ses_test", "ses_B"},
           "两个会话各记一笔，来源不混")
    r17 = run(tmp17, ["report"])
    body17 = r17.stdout + r17.stderr
    expect("按会话" in body17, "report 里按会话分列")
    expect("ses_test" in body17 and "ses_B" in body17, "分列里有两个会话 id")
    expect("$0.000900" in body17 and "$0.000200" in body17,
           "两边的钱分别列出来（不是只报总数）")

    # 旧账本行（v0.2 之前写的，没有 session 字段）：不猜，单独一句
    tmp18 = case("19b 旧行不猜", TURNS3)
    run_json(tmp18, ["sync"], sid="ses_test")
    p18 = os.path.join(tmp18, ".plan", "ledger.jsonl")
    with open(p18, encoding="utf-8") as fh:
        old_rows = [json.loads(x) for x in fh if x.strip()]
    for x in old_rows:
        x.pop("session", None)
    with open(p18, "w", encoding="utf-8", newline="\n") as fh:
        for x in old_rows:
            fh.write(json.dumps(x, ensure_ascii=False) + "\n")
    add_session(os.path.join(tmp18, "fake.db"), "ses_B",
                [(500, 0, 50, 0, 0.0002)])
    run_json(tmp18, ["sync", "--session", "ses_B"])
    r18 = run(tmp18, ["report"])
    body18 = r18.stdout + r18.stderr
    expect("没有会话 id 的旧行" in body18, "旧行单独报一桶")
    expect("不猜" in body18, "并且明说不会替你猜是哪个会话")

    # 只有一个会话时不打这行：没信息量的行也是噪音
    tmp19 = case("19c 单会话不啰嗦", TURNS3)
    run_json(tmp19, ["sync"], sid="ses_test")
    r19 = run(tmp19, ["report"])
    expect("按会话" not in (r19.stdout + r19.stderr),
           "只有一个会话时不打「按会话」（没信息量）")

    # ---------- 20. 归账边界：只认「这一步在做」的轮次 ----------
    # 真机实测出来的坑：计划刚建立、第一步账上就被记了 1102 轮 / $0.486 ——
    # 那其实是整段对话历史。数是真的，归属是错的，比不记更坏。
    tmp20 = case("20 归账边界", TURNS3)
    # 把三轮挪到「一步开始前一小时」：它们属于计划建立之前的对话
    make_db(os.path.join(tmp20, "fake.db"), TURNS3, t0=now_ms() - 3600 * 1000)
    d20 = run_json(tmp20, ["sync"])
    expect(d20 is not None and d20["fresh"] == 0,
           "这步开始之前的轮次不进账本（实际 %s）" % (d20 or {}).get("fresh"))
    expect(((d20 or {}).get("skipped") or {}).get("rounds") == 3,
           "而且报出跳过 3 轮（不许悄悄丢）")
    expect(((d20 or {}).get("boundary") or {}).get("known") is True,
           "边界是已知的（tg start 写的 started_ms）")
    expect(ledger(tmp20) == [], "账本一笔都没多")
    r20 = run(tmp20, ["sync"])
    expect("跳过" in (r20.stdout + r20.stderr), "人读的输出里也说了跳过")

    # 老计划没有 started_ms：退到「计划建立时间」，并且说清依据是什么
    tmp21 = case("20b 老计划退到计划建立时间", TURNS3)
    pj21 = os.path.join(tmp21, ".plan", "plan.json")
    with open(pj21, encoding="utf-8") as fh:
        plan21 = json.load(fh)
    plan21["steps"][0].pop("started_ms", None)
    with open(pj21, "w", encoding="utf-8") as fh:
        json.dump(plan21, fh, ensure_ascii=False)
    d21 = run_json(tmp21, ["sync"])
    expect((d21 or {}).get("fresh") == 3,
           "没有 started_ms 时按计划建立时间划边界，窗口内的轮次照记（实际 %s）"
           % (d21 or {}).get("fresh"))
    expect("计划建立" in (((d21 or {}).get("boundary") or {}).get("why") or ""),
           "并说明边界依据是计划建立时间，不是这步开始时间")

    # 两个时间都没有：不许假装知道边界，全算进来但明说只能当上限
    tmp22 = case("20c 边界不知道就说不知道", TURNS3)
    pj22 = os.path.join(tmp22, ".plan", "plan.json")
    with open(pj22, encoding="utf-8") as fh:
        plan22 = json.load(fh)
    plan22["steps"][0].pop("started_ms", None)
    plan22.pop("created", None)
    with open(pj22, "w", encoding="utf-8") as fh:
        json.dump(plan22, fh, ensure_ascii=False)
    d22 = run_json(tmp22, ["sync"])
    expect((d22 or {}).get("fresh") == 3,
           "算不出边界时不许悄悄丢轮次：全算进来（实际 %s）" % (d22 or {}).get("fresh"))
    expect(((d22 or {}).get("boundary") or {}).get("known") is False,
           "边界标为未知")
    r22 = run(tmp22, ["sync"])
    expect("算不出边界" in (r22.stdout + r22.stderr)
           or "算不出" in (r22.stdout + r22.stderr), "并且明说算不出边界")

    # 边界之后还有新轮次：`fresh>0` 且 `skipped>0` 的那条路径。
    # 真机第一次跑就在这儿崩了（人读的输出里取了个不存在的键），所以单独钉一组。
    tmp24 = case("20a 边界两侧都有轮次", TURNS3)
    db24 = os.path.join(tmp24, "fake.db")
    con24 = sqlite3.connect(db24)
    for i in range(2):
        old_ts = now_ms() - (7200 - i) * 1000      # 两小时前，边界之外
        data = {"model": {"id": MODEL[1], "providerID": MODEL[0]},
                "tokens": {"input": 700, "output": 70, "reasoning": 0,
                           "cache": {"read": 0, "write": 0}},
                "cost": 0.0007}
        con24.execute(
            "insert into session_message values (?,?,?,?,?,?,?)",
            ("msg_old_%d" % i, "ses_test", "assistant", -1 - i,
             old_ts, old_ts, json.dumps(data)))
    con24.commit()
    con24.close()
    d24 = run_json(tmp24, ["sync", "--dry-run"])   # dry-run 不动游标，后面才能再看人读输出
    expect((d24 or {}).get("fresh") == 3,
           "边界之后的 3 轮照记（实际 %s）" % (d24 or {}).get("fresh"))
    expect(((d24 or {}).get("skipped") or {}).get("rounds") == 2,
           "边界之前的 2 轮报成跳过（实际 %s）"
           % ((d24 or {}).get("skipped") or {}).get("rounds"))
    expect(abs((((d24 or {}).get("skipped") or {}).get("usd") or 0) - 0.0014) < 1e-12,
           "跳过的钱也报出来（2 × 0.0007）")
    r24 = run(tmp24, ["sync"])
    body24 = r24.stdout + r24.stderr
    expect("跳过 2 轮" in body24, "人读的输出里也说跳过 2 轮")
    expect("归账边界" in body24, "并且先说明边界是什么")
    rows24 = ledger(tmp24)
    expect(len(rows24) == 1 and rows24[0]["reqs"] == 3,
           "账本只记边界之后的 3 轮（实际 %s 笔 / %s 轮）"
           % (len(rows24), rows24[0]["reqs"] if rows24 else "?"))

    # 跳过的那一堆里，要分清「早就入过账的」和「从没入账的」。
    # 真机上这一步占了 1213 轮 / $0.553181，其中 1190 轮 / $0.542841 其实早在账本里了；
    # 混成一个金额报，读的人会以为那是漏掉的钱。
    pj24 = os.path.join(tmp24, ".plan", "plan.json")
    with open(pj24, encoding="utf-8") as fh:
        plan24 = json.load(fh)
    plan24["steps"][0]["started_ms"] = now_ms()      # 边界挪到「现在」：全窗口都被挡
    with open(pj24, "w", encoding="utf-8") as fh:
        json.dump(plan24, fh, ensure_ascii=False)
    d24b = run_json(tmp24, ["sync"])
    sk24 = (d24b or {}).get("skipped") or {}
    expect(sk24.get("rounds") == 5, "边界挪到当下：5 轮全被挡（实际 %s）"
           % sk24.get("rounds"))
    expect(sk24.get("accounted") == 3, "其中 3 轮已经在账本里（实际 %s）"
           % sk24.get("accounted"))
    expect(sk24.get("unaccounted") == 2, "剩 2 轮从没入过账（实际 %s）"
           % sk24.get("unaccounted"))
    expect(abs((sk24.get("usd_accounted") or 0) - 0.0009) < 1e-12,
           "已在账本里的钱 = 3 轮的 cost 之和（实际 %s）" % sk24.get("usd_accounted"))
    r24b = run(tmp24, ["sync"])
    expect("已经在账本里" in (r24b.stdout + r24b.stderr),
           "人读输出里分清「已在账本里」和「没入账」")

    # 边界的一次性：重试 start 不覆盖它；reset 才重新开始计
    tmp23 = case("20d 重试不覆盖边界", TURNS3)
    pj23 = os.path.join(tmp23, ".plan", "plan.json")
    with open(pj23, encoding="utf-8") as fh:
        first_ms = json.load(fh)["steps"][0].get("started_ms")
    expect(isinstance(first_ms, int) and first_ms > 0, "tg start 记下了 started_ms")
    run(tmp23, ["start", "1"])        # 同一轮的重试
    with open(pj23, encoding="utf-8") as fh:
        retry_ms = json.load(fh)["steps"][0].get("started_ms")
    expect(retry_ms == first_ms, "重试 start 不覆盖边界（否则上一轮的钱会被切掉）")
    run(tmp23, ["reset", "1", "--rounds"])
    with open(pj23, encoding="utf-8") as fh:
        expect("started_ms" not in json.load(fh)["steps"][0],
               "reset 清掉边界：重新开始 = 重新计")

    # ---------- C1：两步同时在 doing 时，归账不许猜 ----------
    print("\n== C1 多路并发不猜归属 ==")
    tmpc = tempfile.mkdtemp(prefix="tgsync_")
    make_config(os.path.join(tmpc, "opencode.json"))
    run(tmpc, ["init", "并发归账"])
    run(tmpc, ["add", "--title", "A", "--out", "a/"])
    run(tmpc, ["add", "--title", "B", "--out", "b/"])
    run(tmpc, ["start", "1"])
    rc2 = run(tmpc, ["start", "2"])
    expect(rc2.returncode == 0, "两路可以同时在 doing（≤ max-fanout）")
    t0c = now_ms()
    make_db(os.path.join(tmpc, "fake.db"), TURNS3, t0=t0c)
    pjc = os.path.join(tmpc, ".plan", "plan.json")
    with open(pjc, encoding="utf-8") as fh:
        pj_c = json.load(fh)
    for st in pj_c["steps"]:
        st["started_ms"] = t0c - (len(TURNS3) + 1) * 1000
    pj_c["created"] = iso_ms(t0c - (len(TURNS3) + 2) * 1000)
    with open(pjc, "w", encoding="utf-8") as fh:
        json.dump(pj_c, fh, ensure_ascii=False)

    r = run(tmpc, ["sync"])
    expect(r.returncode != 0, "两步 doing 时 sync 不点名就拒（归错比不记更糟）")
    expect("归错比不记更糟" in (r.stderr or ""), "拒的时候说清为什么不猜")
    expect((r.stderr or "").count("tg sync --step") == 2,
           "拒的时候给出每路一条点名归账命令")
    expect(ledger(tmpc) == [], "拒的时候账本一个字都不许写（宁可不记）")
    expect(not os.path.isfile(os.path.join(tmpc, ".plan", "sync.json"))
           or not sync_state(tmpc)["sessions"],
           "拒的时候游标也不许动")

    d = run_json(tmpc, ["sync", "--step", "2"])
    expect(d is not None and d["step"] == 2, "点名了才记账，且记到点名的那一步")
    expect(d is not None and d["fresh"] == 3,
           "点名的这一路仍认全部 3 轮（边界是这个会话自己的）")
    expect(len(ledger(tmpc)) == 1, "只写了一笔，没把两路的钱混在一起")

    print("\n" + ("全绿。" if not FAILED else "失败 %d 项：" % len(FAILED)))
    for m in FAILED:
        print("  - " + m)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
