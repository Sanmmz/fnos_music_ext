#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v51 单元验收 = v50 全部检查 + 曲库目录一致性（陈旧 .ref 不得复活旧目录）。"""
import os, sys, json, asyncio

sys.path.insert(0, "/tmp/v50t/proxy")
import app as A  # noqa

SB = "/tmp/v50t"
LIB = SB + "/lib"
CACHE = SB + "/home/cache"
FAV = SB + "/home/online_favorites"

fails = []


def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  >> " + str(extra)) if not cond else ""))
    if not cond:
        fails.append(name)


print("=== 0) 配置 ===")
ck("library_dir = 沙箱曲库", A.detect_library_dir() == LIB, A.detect_library_dir())
print("   tee_favorites_only=%s" % A.CONF["tee_favorites_only"])

print("=== 1) range_starts_at_zero ===")
for hdr, want in [(None, True), ("", True), ("bytes=0-", True), ("bytes=0-1048575", True),
                  ("bytes=0-1", True), ("bytes=1048576-2097151", False),
                  ("bytes=5242880-6291455", False), ("Bytes=0-", True)]:
    ck("hdr=%r -> %s" % (hdr, got if False else A.range_starts_at_zero(hdr)),
       A.range_starts_at_zero(hdr) == want)

print("=== 2) 收藏集合 ===")
g1, g2 = "online:lx:kw:111", "online:netease:222"
LOCAL = "8e8527a5651f487fbcddca5d61a60b6c"
with open(os.path.join(FAV, "u1.json"), "w", encoding="utf-8") as f:
    json.dump({"items": [{"guid": g1, "track": {}}, {"guid": LOCAL}]}, f)
with open(os.path.join(FAV, "u2.json"), "w", encoding="utf-8") as f:
    json.dump({"items": [{"guid": g2}]}, f)
ck("并集 = {g1,g2}", A.all_online_favorite_guids() == frozenset([g1, g2]), A.all_online_favorite_guids())
ck("g1 是收藏", A.is_favorite_online_guid(g1) is True)
ck("未收藏为 False", A.is_favorite_online_guid("online:lx:kw:999") is False)
ck("本地 32hex 为 False", A.is_favorite_online_guid(LOCAL) is False)
A.invalidate_favorites_cache()
os.remove(os.path.join(FAV, "u2.json"))
ck("失效后不再含 g2", g2 not in A.all_online_favorite_guids())

print("=== 3) materialized_library_file ===")
libfile = os.path.join(LIB, "歌手A - 歌名A.mp3")
with open(libfile, "wb") as f:
    f.write(b"x" * 2048)
A.remember_media_path(g1, libfile)
ck("命中曲库音频", A.materialized_library_file(g1) == libfile)
rolling = os.path.join(CACHE, A.cache_safe_guid(g2) + ".mp3")
with open(rolling, "wb") as f:
    f.write(b"y" * 2048)
A.remember_media_path(g2, rolling)
ck("滚动缓存不算已落曲库", A.materialized_library_file(g2) is None)

print("=== 4) 删除安全边界 ===")
outside = os.path.join(SB, "outside", "keep.txt")
with open(outside, "w") as f:
    f.write("keep")
ck("拒绝删 media dirs 之外", A._safe_unlink_in_media_dirs(outside) is False and os.path.exists(outside))
ck("拒绝删 /etc/hostname", A._safe_unlink_in_media_dirs("/etc/hostname") is False)

print("=== 5) delete_materialized_media ===")
with open(os.path.join(LIB, "歌手A - 歌名A.lrc"), "w", encoding="utf-8") as f:
    f.write("[00:01.00]x\n")
removed = A.delete_materialized_media(g1)
ck("已删曲库音频", not os.path.exists(libfile), removed)
ck("已删同名词", not os.path.exists(os.path.join(LIB, "歌手A - 歌名A.lrc")), removed)
ck("已删 .ref", not os.path.exists(A.media_ref_path(g1)))
ck("越界文件无恙", os.path.exists(outside))
removed2 = A.delete_materialized_media(g2)
ck("取消收藏清掉滚动缓存", not os.path.exists(rolling), removed2)

print("=== 6) 非在线 guid / inflight 去重 ===")
ck("本地 guid 拒绝", asyncio.run(A._download_favorite_media(None, LOCAL)) is False)
A._FAV_DL_INFLIGHT.add(g1)
ck("inflight 去重", asyncio.run(A._download_favorite_media(None, g1)) is False)
A._FAV_DL_INFLIGHT.discard(g1)

print("=== 7) library_media_path 曲库目录一致性（v51 核心修复）===")
g3 = "online:lx:kw:333"
A.remember_media_path(g3, "/vol2/1000/music/旧曲库 - 老歌.mp3")   # 陈旧映射：旧目录已不存在
p = A.library_media_path(g3, "歌名X", "mp3", artist="歌手Y", directory=LIB)
ck("忽略陈旧映射，落到当前曲库", p.startswith(LIB + "/"), p)
ck("文件名 = 歌手 - 歌名", os.path.basename(p) == "歌手Y - 歌名X.mp3", os.path.basename(p))
A.remember_media_path(g3, os.path.join(LIB, "歌手Y - 歌名X.mp3"))
p2 = A.library_media_path(g3, "别的", "mp3", artist="别的", directory=LIB)
ck("同目录映射仍复用（幂等）", p2 == os.path.join(LIB, "歌手Y - 歌名X.mp3"), p2)
p3 = A.library_media_path(g3, "别的", "mp3", artist="别的")   # directory=None 走 detect
ck("directory=None 时同样一致", p3 == os.path.join(LIB, "歌手Y - 歌名X.mp3"), p3)

print()
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL v51 UNIT CHECKS PASSED")
