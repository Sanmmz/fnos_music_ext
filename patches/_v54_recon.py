#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v54 E2E 前置侦查：token 表结构、在线历史模式、当前收藏。"""
import os
import sqlite3

HOME = "/home/<user>/fnmusic_ext"
DB = "/usr/local/apps/@appdata/trim.music/db/music.db"

print("=== ONLINE_HISTORY_MODE ===")
p = os.path.join(HOME, "ONLINE_HISTORY_MODE")
print(" ", open(p).read().strip() if os.path.exists(p) else "(缺)")

print("=== .env 开关 ===")
for line in open(os.path.join(HOME, ".env"), encoding="utf-8", errors="replace"):
    if "FNMUSIC_" in line and any(k in line for k in ("FAV", "TEE", "LYRIC", "AUTO")):
        print("  " + line.rstrip())

print("=== 当前收藏（本地 JSON）===")
fav_dir = os.path.join(HOME, "online_favorites")
tot = 0
for n in sorted(os.listdir(fav_dir)):
    if not n.endswith(".json"):
        continue
    import json as _j
    try:
        obj = _j.load(open(os.path.join(fav_dir, n), encoding="utf-8"))
        items = obj.get("items") or []
        gs = [i.get("guid") for i in items]
        tot += len(items)
        print("  %s -> %d 条 %s" % (n, len(items), gs[:6]))
    except Exception as e:
        print("  %s -> 读取失败 %s" % (n, e))
print("  合计:", tot)

print("=== music.db token 相关表 ===")
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
tabs = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("  all tables:", tabs)
for t in tabs:
    if "token" in t.lower() or "user" in t.lower():
        cols = [d[1] for d in con.execute("PRAGMA table_info(%s)" % t)]
        n = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        print("  -- %s rows=%d cols=%s" % (t, n, cols))
        for row in con.execute("SELECT * FROM %s LIMIT 3" % t):
            d = dict(row)
            print("     ", {k: (str(v)[:12] + "…" if isinstance(v, str) and len(str(v)) > 12 else v)
                            for k, v in d.items()})
con.close()
