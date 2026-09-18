#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比上游 fnos_music_ext 与本分支的 proxy/app.py 结构差异（函数级）。

用法（在仓库根目录执行）：

    # ① 下载上游源码并解压，假设在 /tmp/upstream/fnos_music_ext-1.6.0
    python patches/_diff_upstream.py --upstream /tmp/upstream/fnos_music_ext-1.6.0/proxy/app.py

    # ② 也可以直接指向上游仓库的工作副本
    python patches/_diff_upstream.py --upstream ../fnos_music_ext/proxy/app.py

    # ③ 不传参数则尝试用环境变量 UPSTREAM_APP
    UPSTREAM_APP=/path/to/upstream/proxy/app.py python patches/_diff_upstream.py

输出：规模对照、本分支新增的函数、被移除的函数（正常应为 0）、路由与配置项差异。

设计意图：让「本分支是纯增量补丁」这个结论**可被任何人复现**，
而不是只写在文档里。上游发新版本后，把 --upstream 指向新版本再跑一次即可。
"""
import argparse
import io
import os
import re
import sys

DEF_RE = re.compile(r"^(?:async )?def ([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.M)
ROUTE_RE = re.compile(r"^@app\.(get|post|put|delete|patch)\(\"([^\"]+)\"", re.M)
CONF_RE = re.compile(r"^\s{4}\"([a-z0-9_]+)\":", re.M)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def load(path):
    src = io.open(path, "r", encoding="utf-8", errors="replace").read()
    lines = src.count("\n") + 1
    defs = DEF_RE.findall(src)
    routes = ROUTE_RE.findall(src)
    conf = []
    if "CONF = {" in src:
        block = src.split("CONF = {", 1)[1].split("\n}", 1)[0]
        conf = CONF_RE.findall(block)
    return src, lines, defs, routes, conf


def main():
    ap = argparse.ArgumentParser(description="对比上游与本分支的 proxy/app.py")
    ap.add_argument("--upstream", dest="up",
                    default=os.environ.get("UPSTREAM_APP", ""),
                    help="上游 proxy/app.py 路径")
    ap.add_argument("--local", dest="loc",
                    default=os.path.join(REPO, "proxy", "app.py"),
                    help="本分支 proxy/app.py 路径（默认仓库内）")
    args = ap.parse_args()

    if not args.up:
        print("错误：请用 --upstream <上游 proxy/app.py> 或设置 UPSTREAM_APP 环境变量")
        return 2
    for p in (args.up, args.loc):
        if not os.path.isfile(p):
            print("错误：文件不存在 %s" % p)
            return 2

    su, nu, du, ru, cu = load(args.up)
    sm, nm, dm, rm, cm = load(args.loc)

    print("=== 规模 ===")
    print("  上游  : %5d 行, %3d 函数, %2d 路由, %2d 配置项" % (nu, len(du), len(ru), len(cu)))
    print("  本分支: %5d 行, %3d 函数, %2d 路由, %2d 配置项" % (nm, len(dm), len(rm), len(cm)))
    print("  差值  : %+5d 行, %+3d 函数, %+2d 路由, %+2d 配置项"
          % (nm - nu, len(dm) - len(du), len(rm) - len(ru), len(cm) - len(cu)))
    print()

    su_n, sm_n = set(du), set(dm)
    added = sorted(sm_n - su_n)
    removed = sorted(su_n - sm_n)

    print("=== 本分支新增函数 %d 个 ===" % len(added))
    for i in range(0, len(added), 4):
        print("  " + "  ".join("%-40s" % x for x in added[i:i + 4]))
    print()

    print("=== 上游有、本分支移除的函数 %d 个 %s ==="
          % (len(removed), "（纯增量补丁 ✓）" if not removed else "← 请确认是有意删除"))
    for x in removed:
        print("  " + x)
    print()

    ru_s = {p for _, p in ru}
    rm_s = {p for _, p in rm}
    print("=== 路由差异 ===")
    print("  新增:", sorted(rm_s - ru_s) or "（无）")
    print("  移除:", sorted(ru_s - rm_s) or "（无）")
    print()

    print("=== 配置项差异 ===")
    only_local = sorted(set(cm) - set(cu))
    only_up = sorted(set(cu) - set(cm))
    print("  本分支新增 %d 个：" % len(only_local))
    for i in range(0, len(only_local), 3):
        print("   " + "  ".join("%-32s" % x for x in only_local[i:i + 3]))
    print("  上游有、本分支没有 %d 个：%s" % (len(only_up), only_up or "（无）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
