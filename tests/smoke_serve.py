# -*- coding: utf-8 -*-
"""工作台的联网自检：起真服务、打真请求、验 Host 头、采样端点与 /api/reviews。

跑法：python tests/smoke_serve.py
退出码 0 = 全过。它不碰 .plan/ 的计划，只往 .plan/ctx.jsonl 追一条采样；
/api/reviews 的造数据用例全在临时目录里起服务，绝不写真实 .plan/。
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 默认测本仓库的 tg.py；TOLLGATE_TG 可指向安装后的副本，用来验证交付物自足
TG = os.environ.get("TOLLGATE_TG") or os.path.join(ROOT, "tg.py")
TG_ROOT = os.path.dirname(os.path.abspath(TG))
PORT = 7799
# /api/reviews 的六种形状要在临时仓库里造数据，那得另起一个服务，不能混进真实仓库
REVIEWS_PORT = 7801

sys.stdout.reconfigure(encoding="utf-8")
FAILED = []


def expect(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def get(path, host=None, port=None):
    conn = http.client.HTTPConnection("127.0.0.1", port or PORT, timeout=5)
    headers = {"Host": host} if host else {}
    conn.request("GET", path, headers=headers)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, body


def start(cwd, port, what="服务"):
    """起一个真服务并等它应答；起不来就记一条失败、返回 None。"""
    proc = subprocess.Popen(
        [sys.executable, TG, "serve", "--port", str(port), "--no-open"],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(40):
        time.sleep(0.15)
        try:
            status, _ = get("/api/state", port=port)
            if status == 200:
                return proc
        except OSError:
            continue
    stop(proc)
    expect(False, "%s在 6 秒内没起来（端口 %d）" % (what, port))
    return None


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def reviews_in(files):
    """在一个临时仓库里造 .plan/review-*.json，起服务打 /api/reviews。

    数据只写临时目录，绝不往真实仓库的 .plan/ 里写东西。返回 (status, payload)，
    服务起不来返回 (None, None)。
    """
    tmp = tempfile.mkdtemp(prefix="tg-reviews-")
    try:
        plan = os.path.join(tmp, ".plan")
        os.makedirs(plan)
        for name, body in files.items():
            text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            with open(os.path.join(plan, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        proc = start(tmp, REVIEWS_PORT, "临时仓库的服务")
        if proc is None:
            return None, None
        try:
            status, raw = get("/api/reviews", port=REVIEWS_PORT)
            payload = json.loads(raw.decode("utf-8")) if status == 200 else None
            return status, payload
        finally:
            stop(proc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_reviews():
    """/api/reviews：人确认进度只读，null 是「没有逐条数据」，不是「0 条确认」。"""
    # 1) 全绿形状：六键一个不少。
    #    数据必须**自己造**在临时仓库里 —— 早先这里打的是真实仓库根，
    #    依赖 `.plan/review-*.json` 存在；而 `.plan/` 是 `.gitignore` 掉的，
    #    于是全新 clone 下来这条必然红（实测 55 项变 54 项、退出码 1）。
    #    测试不该依赖「我这台机器上恰好有那个目录」。
    status, doc = reviews_in({
        "review-1.json": {"step": 1, "reviewer": "jev", "verdict": "pass",
                          "human_verified": True,
                          "human_confirmed_claims": {"confirmed": 2, "total": 2}},
    })
    expect(status == 200, "/api/reviews 返回 200")
    if status != 200:
        return
    expect(doc.get("cmd") == "reviews", "/api/reviews.cmd == reviews")
    rows = doc.get("reviews") or []
    expect(len(rows) >= 1, "/api/reviews.reviews 非空（%d 条）" % len(rows))
    for k in ("file", "step", "reviewer", "verdict", "human_verified", "human_claims"):
        expect(all(k in r for r in rows), "reviews[] 每项都含 %s" % k)
    expect(all(isinstance(r.get("human_claims"), dict)
               and set(("confirmed", "total")) <= set(r["human_claims"]) for r in rows)
           if rows else True,
           "human_claims 里是 confirmed / total 两个键")
    note = doc.get("note") or ""
    expect(bool(note) and "exit code" in note,
           "note 非空且含「exit code」（verdict 不是验收结论这句要跟着数据出去）")

    # 2) 无逐条数据 + 5) 坏文件：同一个临时仓库里一次打掉
    status, doc = reviews_in({
        "review-2.json": {"step": 2, "reviewer": "local-4b", "verdict": "pass"},
        "review-5.json": "{不是 json",
    })
    expect(status == 200, "有坏文件时 /api/reviews 仍然 200（不 500、不静默跳过）")
    if status == 200:
        rows = {r["file"]: r for r in (doc.get("reviews") or [])}
        one = rows.get("review-2.json") or {}
        claims = one.get("human_claims") or {}
        expect(claims.get("total") is None,
               "没有 checks / human_confirmed_claims 时 human_claims.total 是 null（不是 0）")
        expect((doc.get("summary") or {}).get("without_claims", 0) >= 1,
               "summary.without_claims >= 1")
        bad = doc.get("bad_files") or []
        hit = [b for b in bad if b.get("file") == "review-5.json"]
        expect(len(hit) == 1 and bool(hit[0].get("error")),
               "解析不了的文件出现在 bad_files[] 里（带 file 和 error）")
        expect((doc.get("summary") or {}).get("bad", 0) >= 1, "summary.bad >= 1")

    # 3) 部分确认：一条确认三条 claim，不算验证通过
    status, doc = reviews_in({
        "review-6.json": {"step": 6, "reviewer": "local-4b", "verdict": "pass",
                          "human_confirmed_claims": {"confirmed": 1, "total": 3}},
    })
    if status == 200:
        one = ((doc.get("reviews") or [{}])[0]).get("human_claims") or {}
        sm = doc.get("summary") or {}
        expect(one.get("confirmed") == 1 and one.get("total") == 3,
               "human_confirmed_claims 1/3 → confirmed=1, total=3")
        expect(sm.get("all_confirmed") is False,
               "部分确认时 summary.all_confirmed 是 false")
    else:
        expect(False, "部分确认形状的 /api/reviews 该返回 200，实际 %s" % status)

    # 4) 全部经人确认：只有这种情况 all_confirmed 才能是 true
    status, doc = reviews_in({
        "review-7.json": {"step": 7, "reviewer": "jev", "verdict": "pass",
                          "human_confirmed_claims": {"confirmed": 2, "total": 2}},
    })
    if status == 200:
        sm = doc.get("summary") or {}
        expect(sm.get("all_confirmed") is True,
               "全部 2/2 都经人确认时 summary.all_confirmed 是 true")
        expect(sm.get("claims_total") == 2, "全绿时 summary.claims_total == 2")
    else:
        expect(False, "全绿形状的 /api/reviews 该返回 200，实际 %s" % status)


def main():
    proc = start(TG_ROOT, PORT)
    if proc is None:
        return 1
    try:
        status, body = get("/api/state")
        expect(status == 200, "/api/state 返回 200")
        state = json.loads(body.decode("utf-8"))
        for k in ("goal", "steps", "totals", "ctx", "ts"):
            expect(k in state, "/api/state 含字段 %s" % k)
        expect("session" in state.get("ctx", {}), "/api/state.ctx 含 session")

        # 钱与对账：看板得能看见账本里的真实 cost，以及还有几轮没入账
        for k in ("money", "sync"):
            expect(k in state, "/api/state 含字段 %s" % k)
        money = state.get("money") or {}
        for k in ("ledger_usd", "rows", "rounds", "per_round_usd", "host_usd",
                  "other_sessions", "other_usd", "by_session"):
            expect(k in money, "/api/state.money 含 %s" % k)
        expect(isinstance(money.get("by_session"), list),
               "money.by_session 是列表（按会话分列；没有 cost 时是空表，不编数）")
        expect(all(isinstance(g, dict) and set(("session", "usd", "rows", "reqs")) <= set(g)
                   for g in (money.get("by_session") or [])),
               "by_session 每项含 session / usd / rows / reqs")
        syncing = state.get("sync") or {}
        for k in ("session", "synced_ids", "updated", "same_session",
                  "pending_in_window"):
            expect(k in syncing, "/api/state.sync 含 %s" % k)
        expect(money.get("ledger_usd") is None or money["ledger_usd"] >= 0,
               "money.ledger_usd 要么是金额要么是 null（不编数）")

        status, js = get("/app.js")
        js = js.decode("utf-8", "replace")
        expect("pending_in_window" in js, "前端用上了对账状态字段")
        expect("账本花费" in js, "前端有「账本花费」卡片")
        expect("同期在花" in js, "前端有「同期在花」提示（账本只是下限）")

        for path, ctype in (("/", "text/html"), ("/app.js", "javascript"),
                            ("/style.css", "text/css")):
            status, body = get(path)
            expect(status == 200 and len(body) > 200,
                   "%s 返回 200 且非空（%d 字节）" % (path, len(body)))

        status, _ = get("/nope")
        expect(status == 404, "未知路径返回 404")

        status, _ = get("/api/state", host="evil.example.com")
        expect(status == 403, "非本机 Host 头被拒（防 DNS rebinding）")

        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
        conn.request("POST", "/api/sample")
        r = conn.getresponse()
        payload = json.loads(r.read().decode("utf-8"))
        conn.close()
        expect(r.status in (200, 400), "POST /api/sample 有明确响应")
        if r.status == 200:
            expect(payload.get("ok") is True, "采样端点返回 ok")

        # 人确认进度：看板上要能看见验到哪了，且「没有数据」不许写成「0/0」
        check_reviews()
    finally:
        stop(proc)

    print("")
    if FAILED:
        print("==== %d 项失败 ====" % len(FAILED))
        for f in FAILED:
            print("  - " + f)
        return 1
    print("==== 全部通过 ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
