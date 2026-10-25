# -*- coding: utf-8 -*-
"""tg ctx / price 的离线回归。用构造出来的同构 sqlite 库，不碰真实宿主数据。

跑法：python tests/test_ctx.py
退出码 0 = 全绿。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TG = os.path.join(ROOT, "tg.py")

sys.stdout.reconfigure(encoding="utf-8")
FAILED = []
LOGS = []

FLASH = ("kouxian", "deepseek-v4.1-flash")
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


def make_db(path, sid="ses_test", model=FLASH, cost=0.0, turns=None,
            cumulative=None):
    """turns: [(input, cache_read, output, reasoning, cost), ...] 按时间升序。"""
    con = sqlite3.connect(path)
    con.executescript("""
        create table session_v2 (
            id text, project_id text, workspace_id text, parent_id text,
            fork_session_id text, fork_boundary text, slug text, directory text,
            path text, title text, version text, share_url text,
            summary_additions int, summary_deletions int, summary_files int,
            summary_diffs text, metadata text, cost real, tokens_input int,
            tokens_output int, tokens_reasoning int, tokens_cache_read int,
            tokens_cache_write int, revert text, permission text, agent text,
            model text, time_created int, time_updated int, time_compacting int,
            time_archived int, time_suspended int, resume_attempts int,
            time_idle int, time_viewed int, idle_outcome text);
        create table session_message (
            id text, session_id text, type text, seq int,
            time_created int, time_updated int, data text);
    """)
    tin = sum(t[0] for t in (turns or []))
    tcr = sum(t[1] for t in (turns or []))
    tout = sum(t[2] for t in (turns or []))
    trea = sum(t[3] for t in (turns or []))
    if cumulative:
        tin, tcr, tout, trea = cumulative
    con.execute(
        "insert into session_v2 (id, cost, tokens_input, tokens_output,"
        " tokens_reasoning, tokens_cache_read, tokens_cache_write, model,"
        " time_created, time_updated) values (?,?,?,?,?,?,?,?,?,?)",
        (sid, cost, tin, tout, trea, tcr, 0,
         json.dumps({"id": model[1], "providerID": model[0]}), 1, 1000))
    for i, (i_in, i_cr, i_out, i_rea, i_cost) in enumerate(turns or []):
        data = {"model": {"id": model[1], "providerID": model[0]},
                "tokens": {"input": i_in, "output": i_out,
                           "reasoning": i_rea,
                           "cache": {"read": i_cr, "write": 0}},
                "cost": i_cost}
        con.execute(
            "insert into session_message (id, session_id, type, seq,"
            " time_created, time_updated, data) values (?,?,?,?,?,?,?)",
            ("msg_%03d" % i, sid, "assistant", i, 100 + i, 100 + i,
             json.dumps(data)))
    con.commit()
    con.close()


def run_ctx(tmp, args=None, sid="ses_test", config=True):
    env = dict(os.environ)
    env["TOLLGATE_DB"] = os.path.join(tmp, "fake.db")
    env["TOLLGATE_CONFIG"] = os.path.join(tmp, "opencode.json")
    env["OPENCODE_SESSION_ID"] = sid
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, TG, "ctx", "--json"] + (args or [])
    r = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    LOGS.append("$ tg ctx --json %s  [exit %d]\n%s%s"
                % (" ".join(args or []), r.returncode, r.stdout, r.stderr))
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def case(name):
    print("\n== %s ==" % name)
    tmp = tempfile.mkdtemp(prefix="tgctx_")
    make_config(os.path.join(tmp, "opencode.json"))
    return tmp


def main():
    # ---------- 1. 逐轮口径 + 增速预测 + 消息自带 cost ----------
    tmp = case("逐轮口径 / 增速 / 本轮花费")
    make_db(os.path.join(tmp, "fake.db"), cost=0.5, turns=[
        (300, 9700, 50, 0, 0.0001),
        (400, 19600, 60, 0, 0.0002),
        (400, 29600, 60, 0, 0.0002),
        (500, 39500, 70, 0, 0.0003),
        (500, 49500, 80, 0, 0.0003),
        (500, 69500, 90, 0, 0.001234),
    ], cumulative=(196500, 69600, 410, 0))
    r = run_ctx(tmp)
    expect(r is not None, "ctx --json 正常退出")
    if r:
        expect(r["turns"] == 6, "读到 6 轮逐轮数据（实际 %s）" % r["turns"])
        expect(r["estimate"]["tokens"] == 70000,
               "本轮上下文 = input 500 + cache_read 69500（实际 %s）"
               % r["estimate"]["tokens"])
        expect(r["turn"]["usd"] == 0.001234,
               "本轮花费用消息自带 cost（实际 %s）" % r["turn"]["usd"])
        g = r["growth"] or {}
        expect(g.get("tok_per_round") == 10000,
               "增速取中位差分 10000（实际 %s）" % g.get("tok_per_round"))
        expect(g.get("rounds_left") == 4,
               "距 90%% 窗口还有 4 轮（实际 %s）" % g.get("rounds_left"))
        expect(len(r["history"]) == 6, "history 有 6 个点")
        expect(r["cumulative"]["usd_db"] == 0.5, "累计 db 值原样带出")
        expect(any("占窗口" in v for v in r["verdict"]),
               "上下文 54.7%% 触发窗口告警")
        expect(any("tok/轮" in v for v in r["verdict"]),
               "增速触发「还能撑几轮」告警")
        sv = r["savings"] or {}
        ratio = sv.get("ratio") or 0
        expect(1.4 < ratio < 1.6,
               "换手/压缩 比值落在 1.4~1.6（实际 %.3f）" % ratio)
        # 手算核对：压缩只省 2/3，换手几乎省全部
        exp_c = 70000 * (2.0 / 3.0) * PRICE["input"] / 1e6
        expect(abs(sv.get("compress_usd", 0) - exp_c) < 1e-9,
               "压缩收益与手算一致（%.9f）" % sv.get("compress_usd", -1))

    # ---------- 2. 缓存命中率过低 → 前缀漂移告警 ----------
    tmp = case("前缀漂移")
    make_db(os.path.join(tmp, "fake.db"), turns=[
        (30000, 2000, 50, 0, 0.001),
        (31000, 2100, 50, 0, 0.001),
        (32000, 2200, 50, 0, 0.001),
    ])
    r = run_ctx(tmp)
    expect(r is not None and any("命中率" in v for v in r["verdict"]),
           "低命中率触发「前缀在漂移」")

    # ---------- 3. 没有逐轮数据 → 必须说「未知」，不能拿累计冒充 ----------
    tmp = case("无逐轮数据")
    make_db(os.path.join(tmp, "fake.db"), turns=[],
            cumulative=(900000, 3000000, 5000, 800))
    r = run_ctx(tmp)
    expect(r is not None, "无逐轮数据仍能输出")
    if r:
        expect(r["estimate"]["tokens"] is None, "estimate 为 None（不冒充）")
        expect(r["turns"] == 0, "turns 为 0")
        expect(any("未知" in v for v in r["verdict"]), "明确说「未知」")
        expect(r["savings"] is None, "无从比价时不编造收益")

    # ---------- 4. 模型没写 cost → 无价目，但上下文照报 ----------
    tmp = case("模型无价目")
    make_config(os.path.join(tmp, "opencode.json"))
    cfg = json.load(open(os.path.join(tmp, "opencode.json"), encoding="utf-8"))
    del cfg["providers"]["kouxian"]["models"]["deepseek-v4.1-flash"]["cost"]
    json.dump(cfg, open(os.path.join(tmp, "opencode.json"), "w", encoding="utf-8"))
    make_db(os.path.join(tmp, "fake.db"), turns=[
        (100, 20000, 50, 0, 0.0), (100, 21000, 50, 0, 0.0),
        (100, 22000, 50, 0, 0.0)])
    r = run_ctx(tmp)
    expect(r is not None, "无价目时仍能跑")
    if r:
        expect(r["price"] is None, "price 为 None")
        expect(r["estimate"]["tokens"] == 22100, "上下文仍能算（%s）"
               % r["estimate"]["tokens"])
        expect(any("没写 cost" in v for v in r["verdict"]), "提示 cost 未配")

    # ---------- 5. db 记的钱低于重算 → 说明以 db 为准 ----------
    tmp = case("记账口径")
    make_db(os.path.join(tmp, "fake.db"), cost=0.0005, turns=[
        (1000, 50000, 500, 0, 0.001), (1000, 50000, 500, 0, 0.001),
        (1000, 60000, 500, 0, 0.001)],
        cumulative=(3000, 160000, 1500, 0))
    r = run_ctx(tmp)
    expect(r is not None and any("以 db 为准" in v for v in r["verdict"]),
           "重算高于 db 时明确标注以 db 为准")

    # ---------- 6. price 子命令 ----------
    tmp = case("price")
    make_db(os.path.join(tmp, "fake.db"), turns=[(1, 1, 1, 0, 0.0)] * 3)
    env = dict(os.environ)
    env["TOLLGATE_DB"] = os.path.join(tmp, "fake.db")
    env["TOLLGATE_CONFIG"] = os.path.join(tmp, "opencode.json")
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, TG, "price"], cwd=tmp, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    LOGS.append("$ tg price [exit %d]\n%s%s" % (r.returncode, r.stdout, r.stderr))
    expect(r.returncode == 0 and "deepseek-v4.1-flash" in r.stdout,
           "price 列出模型")
    expect("%.6f" % PRICE["input"] in r.stdout, "price 打印 input 单价")

    # ---------- 收尾 ----------
    for d in [x for x in os.listdir(tempfile.gettempdir())
              if x.startswith("tgctx_")]:
        shutil.rmtree(os.path.join(tempfile.gettempdir(), d), ignore_errors=True)

    print("")
    if FAILED:
        print("==== %d 项失败 ====" % len(FAILED))
        for f in FAILED:
            print("  - " + f)
        print("\n--- 日志 ---")
        for log in LOGS:
            print(log)
        return 1
    print("==== 全部通过 ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
