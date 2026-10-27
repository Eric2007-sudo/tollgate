# -*- coding: utf-8 -*-
"""README 里每个测试文件的项目数，必须等于该文件真跑出来的 OK 行数。

为什么要有这个文件：README 曾经写着 `test_sync.py # 56 项`，而它实际只有 53 项
—— 数漂了没人发现，因为「数字」没有人跑一遍去对。这个脚本就是那个跑一遍的人：
它不重复实现断言，只**真跑每个套件**、数 `OK ` 开头的行，然后跟 README 对。

跑法：python tests/check_doc_counts.py
退出码 0 = README 的数与实测一致。它本身不计入 README 的项目数。
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
README = os.path.join(ROOT, "README.md")

sys.stdout.reconfigure(encoding="utf-8")
FAILED = []


def ok(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg)
    if not cond:
        FAILED.append(msg)


def run_suite(rel):
    """真跑一个套件，返回（退出码, OK 行数）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, os.path.join(ROOT, rel)],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    ok_lines = sum(1 for ln in (p.stdout or "").splitlines()
                   if ln.startswith("OK "))
    return p.returncode, ok_lines


def main():
    with open(README, encoding="utf-8-sig") as fh:
        text = fh.read()

    # README 支持两种写法，两种都认：
    #   ① 代码块注释：python tests/test_tg.py       # 213 项
    #   ② 表格行：    | `tests/test_tg.py` | 213 | ...
    # 为什么加 ②：表格比注释好读，但校验器只认 ①，于是改成表格就会「找到 0 个」——
    # 那时它不是发现了不一致，而是**什么都没检查**，却照样报错，容易被误读成 README 有问题。
    claims = re.findall(r"python\s+(tests/[\w.]+\.py)\s*#\s*(\d+)\s*项", text)
    if not claims:
        claims = re.findall(r"^\|\s*`?(tests/[\w.]+\.py)`?\s*\|\s*(\d+)\s*\|", text, re.M)
    ok(bool(claims), "README 里能读到各套件的项目数（找到 %d 个）" % len(claims))

    total = 0
    for rel, n in claims:
        want = int(n)
        total += want
        code, got = run_suite(rel)
        ok(code == 0, "%s 跑通（退出码 %d）" % (rel, code))
        ok(got == want, "%s：README 写 %d 项，实测 %d 项" % (rel, want, got))

    m = re.search(r"合计\s*\*\*(\d+)\s*项\*\*", text)
    if not m:
        m = re.search(r"^\|\s*\*{0,2}合计\*{0,2}\s*\|\s*\*{0,2}(\d+)\*{0,2}\s*\|",
                      text, re.M)
    ok(bool(m), "README 里有一行「合计 N 项」")
    if m:
        ok(int(m.group(1)) == total,
           "合计：README 写 %s 项，各套件相加 %d 项" % (m.group(1), total))

    print("\n实测合计 %d 项。" % total)
    if FAILED:
        print("==== %d 项不一致 ====" % len(FAILED))
        for f in FAILED:
            print("  -", f)
        return 1
    print("==== README 的数与实测一致 ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
