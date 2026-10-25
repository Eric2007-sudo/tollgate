# -*- coding: utf-8 -*-
"""工作台的联网自检：起真服务、打真请求、验 Host 头与采样端点。

跑法：python tests/smoke_serve.py
退出码 0 = 全过。它不碰 .plan/ 的计划，只往 .plan/ctx.jsonl 追一条采样。
"""
import http.client
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 默认测本仓库的 tg.py；TOLLGATE_TG 可指向安装后的副本，用来验证交付物自足
TG = os.environ.get("TOLLGATE_TG") or os.path.join(ROOT, "tg.py")
TG_ROOT = os.path.dirname(os.path.abspath(TG))
PORT = 7799

sys.stdout.reconfigure(encoding="utf-8")
FAILED = []


def expect(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def get(path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    headers = {"Host": host} if host else {}
    conn.request("GET", path, headers=headers)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, body


def main():
    proc = subprocess.Popen(
        [sys.executable, TG, "serve", "--port", str(PORT), "--no-open"],
        cwd=TG_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            time.sleep(0.15)
            try:
                status, _ = get("/api/state")
                if status == 200:
                    break
            except OSError:
                continue
        else:
            expect(False, "服务在 6 秒内没起来")
            return 1

        status, body = get("/api/state")
        expect(status == 200, "/api/state 返回 200")
        state = json.loads(body.decode("utf-8"))
        for k in ("goal", "steps", "totals", "ctx", "ts"):
            expect(k in state, "/api/state 含字段 %s" % k)
        expect("session" in state.get("ctx", {}), "/api/state.ctx 含 session")

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
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

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
