#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v54 单元验收 = v52/v53 全部检查 + 「歌词永远跟着音频走」。

覆盖：
  · library_guid() 正确读 db shared_library.guid
  · _has_app_auth / remember_auth_headers / _recent_auth_headers（含时效）
  · request_library_scan 排队 + _scan_soon 窗口合并 + 凭证过期不发
  · consume_pending_scan 中间件兜底消费（含透传 cookie/authx）
  · _call_library_scan 真实请求构造（scan{guid} / scan-all / 401 / 异常）
  · 三个触发点已接线（源码级）
"""
import os
import sys
import json
import time
import asyncio

sys.path.insert(0, "/tmp/v54t/proxy")
import app as A  # noqa

SB = "/tmp/v54t"
LIB = SB + "/lib"
CACHE = SB + "/home/cache"
FAV = SB + "/home/online_favorites"
PROBE = SB + "/home/access_probe.log"
GUID = "test-guid-0001"

fails = []


def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  >> " + str(extra)) if not cond else ""))
    if not cond:
        fails.append(name)


class _Req:
    """够用的 Request 替身：只用 .headers。"""

    def __init__(self, headers):
        self.headers = headers


class _Resp:
    def __init__(self, status_code=200, payload=None, text=None, bad_json=False):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self._bad = bad_json
        self.text = text if text is not None else json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    def __init__(self, resp=None):
        self.resp = resp or _Resp(200, {"code": 0})
        self.last_path = None
        self.last_json = None
        self.last_headers = None
        self.calls = 0

    async def post(self, path, **kw):
        self.calls += 1
        self.last_path = path
        self.last_json = kw.get("json", None)
        self.last_headers = kw.get("headers")
        return self.resp


print("=== 0) 配置 / 曲库目录 / guid ===")
ck("library_dir = 沙箱曲库", A.detect_library_dir() == LIB, A.detect_library_dir())
ck("library_guid = %s" % GUID, A.library_guid() == GUID, A.library_guid())
print("   auto_scan=%s delay=%ss ttl=%ss scan_all=%s" % (
    A.CONF["auto_scan"], A.CONF["auto_scan_delay_s"],
    A.CONF["auto_scan_auth_ttl_s"], A.CONF["auto_scan_scan_all"]))

print("=== 1) range_starts_at_zero ===")
for hdr, want in [(None, True), ("", True), ("bytes=0-", True), ("bytes=0-1048575", True),
                  ("bytes=0-1", True), ("bytes=1048576-2097151", False),
                  ("bytes=5242880-6291455", False), ("Bytes=0-", True)]:
    ck("hdr=%r -> %s" % (hdr, A.range_starts_at_zero(hdr)), A.range_starts_at_zero(hdr) == want)

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

print("=== 6) library_media_path 曲库目录一致性（v51 修复）===")
g3 = "online:lx:kw:333"
A.remember_media_path(g3, "/vol2/1000/music/旧曲库 - 老歌.mp3")
p = A.library_media_path(g3, "歌名X", "mp3", artist="歌手Y", directory=LIB)
ck("忽略陈旧映射，落到当前曲库", p.startswith(LIB + "/"), p)
ck("文件名 = 歌手 - 歌名", os.path.basename(p) == "歌手Y - 歌名X.mp3", os.path.basename(p))
A.remember_media_path(g3, os.path.join(LIB, "歌手Y - 歌名X.mp3"))
p2 = A.library_media_path(g3, "别的", "mp3", artist="别的", directory=LIB)
ck("同目录映射仍复用（幂等）", p2 == os.path.join(LIB, "歌手Y - 歌名X.mp3"), p2)
p3 = A.library_media_path(g3, "别的", "mp3", artist="别的")
ck("directory=None 时同样一致", p3 == os.path.join(LIB, "歌手Y - 歌名X.mp3"), p3)

print("=== 7) v52 鉴权头识别 ===")
ck("空 dict 无鉴权", A._has_app_auth({}) is False)
ck("仅 host 无鉴权", A._has_app_auth({"host": "localhost"}) is False)
ck("空白值不算鉴权", A._has_app_auth({"cookie": "   "}) is False)
ck("cookie 算鉴权", A._has_app_auth({"cookie": "music-token=abc"}) is True)
ck("authorization 算鉴权", A._has_app_auth({"authorization": "Bearer x"}) is True)
ck("x-trim-music-temp-token 算鉴权", A._has_app_auth({"x-trim-music-temp-token": "t"}) is True)
ck("authx 算鉴权", A._has_app_auth({"authx": "sig"}) is True)

A._LAST_AUTH["ts"] = 0.0
A._LAST_AUTH["headers"] = None
A.remember_auth_headers(_Req({"host": "localhost"}))
ck("匿名请求不记鉴权", A._recent_auth_headers() is None)
A.remember_auth_headers(_Req({"host": "localhost", "cookie": "c1"}))
got = A._recent_auth_headers()
ck("带鉴权请求被记住", isinstance(got, dict) and got.get("cookie") == "c1", got)
A._LAST_AUTH["ts"] = time.monotonic() - 1e6
ck("超 TTL 视为过期", A._recent_auth_headers() is None)
A._LAST_AUTH["ts"] = time.monotonic()
ck("TTL 内仍有效", A._recent_auth_headers() is not None)


print("=== 8) 待办扫库：排队 / 合并 / 兜底 / 真实请求构造 ===")


async def _scan_tests():
    orig_call = A._call_library_scan
    orig_client = A.get_upstream_client
    A.CONF["auto_scan"] = True
    A.CONF["auto_scan_delay_s"] = 0.0
    A.CONF["auto_scan_auth_ttl_s"] = 90.0
    A.CONF["auto_scan_scan_all"] = False

    def _reset():
        A._PENDING_SCAN["count"] = 0
        A._LAST_AUTH["ts"] = 0.0
        A._LAST_AUTH["headers"] = None

    calls = []

    async def fake_call(headers):
        calls.append(dict(headers))
        return True

    _reset()
    A._call_library_scan = fake_call

    # 8.1 无凭证：排队但不发
    A.request_library_scan("unit-anon")
    ck("排队后 pending=1", A._PENDING_SCAN["count"] == 1, A._PENDING_SCAN)
    await asyncio.sleep(0.15)
    ck("无凭证时不自作主张发扫库", calls == [] and A._PENDING_SCAN["count"] == 1,
       (calls, A._PENDING_SCAN))

    # 8.2 带上鉴权的 App 请求到达 → 中间件兜底消费
    H_AUTH = {"host": "localhost", "cookie": "music-token=abc", "authx": "sig",
              "accept-encoding": "identity"}
    ok = await A.consume_pending_scan(_Req(dict(H_AUTH)))
    ck("中间件消费成功", ok is True and len(calls) == 1, (ok, calls))
    ck("消费后待办清零", A._PENDING_SCAN["count"] == 0, A._PENDING_SCAN)
    ck("透传了 cookie", calls[0].get("cookie") == "music-token=abc", calls[0])
    ck("透传了 authx", calls[0].get("authx") == "sig", calls[0])

    # 8.3 无待办时中间件是空操作（不打扰上游）
    ok = await A.consume_pending_scan(_Req(dict(H_AUTH)))
    ck("无待办时不调用", ok is False and len(calls) == 1, (ok, calls))

    # 8.4 待办 + 匿名请求 → 不消费（保留待办等下一个带鉴权请求）
    A._PENDING_SCAN["count"] = 1
    ok = await A.consume_pending_scan(_Req({"host": "localhost"}))
    ck("匿名请求不消耗待办", ok is False and A._PENDING_SCAN["count"] == 1, (ok, A._PENDING_SCAN))

    # 8.5 凭证新鲜 → 窗口结束后自动发出
    A.remember_auth_headers(_Req(dict(H_AUTH)))
    A._PENDING_SCAN["count"] = 0
    A._PENDING_SCAN["last"] = 0.0
    A.request_library_scan("unit-fresh")
    await asyncio.sleep(0.2)
    ck("凭证新鲜时自动发出并清零", len(calls) == 2 and A._PENDING_SCAN["count"] == 0,
       (calls, A._PENDING_SCAN))

    # 8.6 凭证过期 → 不发，保留待办交给兜底
    A._LAST_AUTH["ts"] = time.monotonic() - 1e6
    A.request_library_scan("unit-stale")
    await asyncio.sleep(0.2)
    ck("凭证过期时不发、待办保留", len(calls) == 2 and A._PENDING_SCAN["count"] == 1,
       (calls, A._PENDING_SCAN))
    ok = await A.consume_pending_scan(_Req(dict(H_AUTH)))
    ck("随后带鉴权请求可兜底消化", ok is True and len(calls) == 3 and A._PENDING_SCAN["count"] == 0,
       (ok, calls, A._PENDING_SCAN))

    # 8.7 多次触发合并（pending 累加，发出后一次清零）
    _reset()
    A.remember_auth_headers(_Req(dict(H_AUTH)))
    for _ in range(4):
        A.request_library_scan("unit-merge")
    ck("多次触发合并计数=4", A._PENDING_SCAN["count"] == 4, A._PENDING_SCAN)
    await asyncio.sleep(0.2)
    ck("一次发出后整体清零", A._PENDING_SCAN["count"] == 0 and len(calls) == 4,
       (calls, A._PENDING_SCAN))

    # 8.8 auto_scan=False → 完全不排队
    _reset()
    A.CONF["auto_scan"] = False
    A.request_library_scan("unit-off")
    ck("auto_scan=False 不排队", A._PENDING_SCAN["count"] == 0, A._PENDING_SCAN)
    ok = await A.consume_pending_scan(_Req(dict(H_AUTH)))
    ck("auto_scan=False 中间件也不动", ok is False, ok)
    A.CONF["auto_scan"] = True

    # 8.9 真实 _call_library_scan —— 走 scan + guid body
    A._call_library_scan = orig_call
    fake = _FakeClient(_Resp(200, {"code": 0}))
    A.get_upstream_client = lambda _app: fake
    ok = await A._call_library_scan({"cookie": "c"})
    ck("scan 成功返回 True", ok is True, ok)
    ck("path = /shared-library/scan", fake.last_path == "/music/api/v1/shared-library/scan",
       fake.last_path)
    ck("body = {guid: 共享库 guid}", fake.last_json == {"guid": GUID}, fake.last_json)
    ck("鉴权头透传", fake.last_headers == {"cookie": "c"}, fake.last_headers)

    # 8.10 上游 401 INVALID TOKEN → False
    fake.resp = _Resp(401, {"code": 99999, "msg": "INVALID TOKEN"})
    ok = await A._call_library_scan({"cookie": "c"})
    ck("401 视为失败", ok is False, ok)

    # 8.11 200 但 code!=0 → False
    fake.resp = _Resp(200, {"code": 1, "msg": "busy"})
    ok = await A._call_library_scan({"cookie": "c"})
    ck("code!=0 视为失败", ok is False, ok)

    # 8.12 非 JSON 响应不崩
    fake.resp = _Resp(200, bad_json=True, text="<html>oops</html>")
    ok = await A._call_library_scan({"cookie": "c"})
    ck("非 JSON 安全降级", ok is False, ok)

    # 8.13 scan_all 模式 → 无 body 打 scan-all
    A.CONF["auto_scan_scan_all"] = True
    fake.resp = _Resp(200, {"code": 0})
    ok = await A._call_library_scan({"cookie": "c"})
    ck("scan_all 走 scan-all", ok is True and fake.last_path == "/music/api/v1/shared-library/scan-all",
       (ok, fake.last_path))
    ck("scan_all 不带 body", fake.last_json is None, fake.last_json)
    A.CONF["auto_scan_scan_all"] = False

    # 8.14 guid 读不到 → 自动降级 scan-all
    A._LIB_DIR_CACHE["guid"] = ""
    A.CONF["library_dir"] = LIB          # 有显式 library_dir 时 detect 直接返回，guid 保持空
    fake.resp = _Resp(200, {"code": 0})
    ok = await A._call_library_scan({"cookie": "c"})
    ck("guid 缺失时降级 scan-all", ok is True and fake.last_path.endswith("/scan-all"),
       (ok, fake.last_path))
    A.CONF["library_dir"] = ""
    A._LIB_DIR_CACHE["exp"] = 0.0
    ck("恢复后 guid 可再读出", A.library_guid() == GUID, A.library_guid())

    # 8.15 上游异常不外抛
    def _boom(_app):
        raise RuntimeError("no upstream")

    A.get_upstream_client = _boom
    ok = await A._call_library_scan({"cookie": "c"})
    ck("上游异常返回 False（不抛）", ok is False, ok)

    A.get_upstream_client = orig_client
    A._call_library_scan = orig_call
    for t in list(A._BG_TASKS):
        t.cancel()


asyncio.run(_scan_tests())

print("=== 9) 探针落盘 ===")
ck("access_probe.log 已生成", os.path.exists(PROBE), PROBE)
if os.path.exists(PROBE):
    body = open(PROBE, "r", encoding="utf-8", errors="replace").read()
    ck("含 [scanreq] queue", "[scanreq] queue" in body)
    ck("含 [scanreq] call", "[scanreq] call" in body)
    ck("含 [scanreq] fail（异常路径）", "[scanreq] fail" in body)
    ck("未泄漏 cookie 值", "music-token=abc" not in body and "c1" not in body)

print("=== 10) 三个触发点已接线（源码级） ===")
src = open("/tmp/v54t/proxy/app.py", "r", encoding="utf-8").read()
ck("tee 成功 → request_library_scan(\"tee\")",
   'request_library_scan("tee")' in src, src.count('request_library_scan("tee")'))
ck("favdl 成功 → request_library_scan(\"favdl\")",
   'request_library_scan("favdl")' in src)
ck("取消收藏删除 → request_library_scan(\"unfav\")",
   'request_library_scan("unfav")' in src)
ck("favdl 触发点在 ok 探针之后",
   src.index('request_library_scan("favdl")') > src.index('_probe_write("[favdl] ok guid=%s dest=%s'))
ck("unfav 触发点受 removed 守卫",
   "if removed:" in src and src.index('request_library_scan("unfav")') > src.index("if removed:"))
ck("中间件已挂兜底钩子", 'if _PENDING_SCAN["count"]:' in src)
ck("中间件内已捕获鉴权头", "remember_auth_headers(request)" in src)

print("=== 11) v53 歌词归属：无音频时绝不写曲库 ===")
G_LIB = "online:netease:8000001"     # 音频真在曲库
G_CACHE = "online:netease:8000002"   # 音频只在滚动缓存
G_NONE = "online:netease:8000003"    # 完全没有音频
G_NONE2 = "online:lx:kw:8000004"     # 完全没有音频（另一音源）

lib_audio = os.path.join(LIB, "歌手A - 歌名A.mp3")
with open(lib_audio, "wb") as f:
    f.write(b"w" * 2048)
A.remember_media_path(G_LIB, lib_audio)
ck("materialized_library_file 认得曲库音频",
   A.materialized_library_file(G_LIB) == lib_audio, A.materialized_library_file(G_LIB))
p = A.lyric_cache_path(G_LIB, title="歌名A", artist="歌手A")
ck("音频在曲库 → 歌词写同名 sidecar", p == os.path.join(LIB, "歌手A - 歌名A.lrc"), p)

cache_audio = os.path.join(CACHE, A.cache_safe_guid(G_CACHE) + ".flac")
with open(cache_audio, "wb") as f:
    f.write(b"z" * 4096)
p = A.lyric_cache_path(G_CACHE, title="歌名C", artist="歌手C")
ck("音频只在缓存 → 歌词写缓存同名 sidecar",
   p == os.path.join(CACHE, A.cache_safe_guid(G_CACHE) + ".lrc"), p)

p = A.lyric_cache_path(G_NONE, title="孤儿歌名", artist="孤儿歌手")
ck("无音频 → 歌词落 cache（不是曲库）", p.startswith(CACHE + "/"), p)
ck("无音频 → 绝不会出现「歌手 - 歌名.lrc」于曲库",
   not (p.startswith(LIB + "/") or "孤儿歌手" in p), p)
p2 = A.lyric_cache_path(G_NONE2)
ck("title/artist 缺省同样落 cache", p2 == os.path.join(CACHE, A.cache_safe_guid(G_NONE2) + ".lrc"), p2)

print("=== 12) write_lyric_cache 的映射归属 ===")
A.write_lyric_cache(G_NONE, "[00:01.00]孤词\n", title="孤儿歌名", artist="孤儿歌手")
wrote = os.path.join(CACHE, A.cache_safe_guid(G_NONE) + ".lrc")
ck("文件落在 cache/", os.path.isfile(wrote), wrote)
ck("曲库未新增任何 .lrc", not os.path.exists(os.path.join(LIB, "孤儿歌手 - 孤儿歌名.lrc")))
ck("记下了 .lyricref", os.path.isfile(A.lyric_ref_path(G_NONE)))
ck("lyricref 内容 = 缓存词干",
   open(A.lyric_ref_path(G_NONE), encoding="utf-8").read().strip()
   == os.path.join(CACHE, A.cache_safe_guid(G_NONE)),
   open(A.lyric_ref_path(G_NONE), encoding="utf-8").read().strip())
ck("没有污染音频 .ref（应为不存在）", not os.path.exists(A.media_ref_path(G_NONE)))
ck("recalled_lyric_path 读得回", A.recalled_lyric_path(G_NONE) == wrote, A.recalled_lyric_path(G_NONE))
ck("find_lyric_file 优先命中 pinned", A.find_lyric_file(G_NONE) == wrote)
ck("read_lyric_cache 内容正确", A.read_lyric_cache(G_NONE) == "[00:01.00]孤词")

A.write_lyric_cache(G_LIB, "[00:02.00]有主词\n", title="歌名A", artist="歌手A")
side = os.path.join(LIB, "歌手A - 歌名A.lrc")
ck("音频在曲库 → sidecar 写在曲库", os.path.isfile(side), side)
ck("sidecar 与音频同词干 → 更新 media .ref",
   os.path.isfile(A.media_ref_path(G_LIB))
   and open(A.media_ref_path(G_LIB), encoding="utf-8").read().strip() == os.path.join(LIB, "歌手A - 歌名A"),
   open(A.media_ref_path(G_LIB), encoding="utf-8").read().strip())
A.write_lyric_cache(G_NONE, "[00:01.00]孤词\n")   # 同内容 → 应跳过重写
ck("同内容不重复落盘（幂等）", A.read_lyric_cache(G_NONE) == "[00:01.00]孤词")

print("=== 13) sweep_orphan_lyrics 孤儿歌词自愈 ===")


def mk(path, text="x"):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def age(path, seconds=600):
    t = time.time() - seconds
    os.utime(path, (t, t))


# 孤儿：.ref 指向曲库，只有 .lrc，没有音频
orphan_stem = os.path.join(LIB, "孤儿甲 - 无音频")
orphan_lrc = mk(orphan_stem + ".lrc")
age(orphan_lrc)
mk(A.media_ref_path("online:netease:8100001"),
   os.path.join(LIB, "孤儿甲 - 无音频"))

# 正常：音频 + 歌词都在
ok_stem = os.path.join(LIB, "正常乙 - 有音频")
with open(ok_stem + ".mp3", "wb") as f:
    f.write(b"a" * 2048)
mk(ok_stem + ".lrc")
mk(A.media_ref_path("online:netease:8100002"), ok_stem)

# 飞牛自己的歌词（没有任何 .ref / .lyricref 记录）→ 绝不能动
fnnas_lrc = mk(os.path.join(LIB, "online_netease_9999999.lrc"))

# cache 内的孤儿 → 不归本函数管（交给 purge_rolling）
cc_stem = os.path.join(CACHE, "online_netease_8100003")
mk(cc_stem + ".lrc")
mk(A.media_ref_path("online:netease:8100003"), cc_stem)

# 刚落盘的孤儿（mtime 很新）→ min_age 应放过
fresh_stem = os.path.join(LIB, "刚写 - 别急")
fresh_lrc = mk(fresh_stem + ".lrc")
mk(A.media_ref_path("online:netease:8100004"), fresh_stem)

removed = A.sweep_orphan_lyrics()
ck("清掉孤儿歌词", orphan_lrc in removed, removed)
ck("孤儿文件已消失", not os.path.exists(orphan_lrc))
ck("音频在则不误删", os.path.exists(ok_stem + ".lrc"))
ck("飞牛自己的歌词毫发无伤", os.path.exists(fnnas_lrc))
ck("cache 内孤儿不由本函数处理", os.path.exists(cc_stem + ".lrc"))
ck("过新文件被放过（min_age 生效）", os.path.exists(fresh_lrc))
ck("返回值只含被删项", all(str(x).endswith(".lrc") for x in removed), removed)

# min_age_s=0 → 新的也清
removed0 = A.sweep_orphan_lyrics(min_age_s=0.0)
ck("min_age_s=0 时清掉新孤儿", fresh_lrc in removed0 and not os.path.exists(fresh_lrc), removed0)

# 开关关闭 → 空操作
orphan2 = mk(os.path.join(LIB, "孤儿丙 - 无音频") + ".lrc")
age(orphan2)
mk(A.media_ref_path("online:netease:8100005"), os.path.join(LIB, "孤儿丙 - 无音频"))
A.CONF["lyric_orphan_gc"] = False
ck("开关关闭时不动手", A.sweep_orphan_lyrics() == [] and os.path.exists(orphan2))
A.CONF["lyric_orphan_gc"] = True
ck("开关打开后能清", orphan2 in A.sweep_orphan_lyrics() and not os.path.exists(orphan2))

# lyricref 指向已消失的曲库歌词 → 一并清掉映射
gone_stem = os.path.join(LIB, "孤儿丁 - 无音频")
gone_lrc = mk(gone_stem + ".lrc")
age(gone_lrc)
mk(A.lyric_ref_path("online:netease:8100006"), gone_stem)
A.sweep_orphan_lyrics(min_age_s=0.0)
ck("lyricref 型孤儿也会被清", not os.path.exists(gone_lrc))
ck("对应 .lyricref 一并移除", not os.path.exists(A.lyric_ref_path("online:netease:8100006")))

print("=== 14) delete_materialized_media 带上歌词映射 ===")
G_DEL = "online:netease:8100007"
dlib = os.path.join(LIB, "待删戊 - 有音频.mp3")
with open(dlib, "wb") as f:
    f.write(b"d" * 2048)
mk(os.path.join(LIB, "待删戊 - 有音频.lrc"))
A.remember_media_path(G_DEL, dlib)
A.remember_lyric_path(G_DEL, os.path.join(LIB, "待删戊 - 有音频.lrc"))
dremoved = A.delete_materialized_media(G_DEL)
ck("删掉音频", not os.path.exists(dlib), dremoved)
ck("删掉歌词 sidecar", not os.path.exists(os.path.join(LIB, "待删戊 - 有音频.lrc")), dremoved)
ck("删掉 .ref", not os.path.exists(A.media_ref_path(G_DEL)))
ck("删掉 .lyricref", not os.path.exists(A.lyric_ref_path(G_DEL)))

print("=== 15) 源码级：曲库不再是歌词兜底 ===")
src2 = open("/tmp/v54t/proxy/app.py", "r", encoding="utf-8").read()
ck("lyric_cache_path 内无 library_basename(...).lrc 兜底",
   'return os.path.join(d, f"{library_basename(title, artist)}.lrc")' not in src2)
ck("无音频一律落 CONF[cache_dir]",
   'return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.lrc")' in src2)
ck("存在孤儿歌词自愈函数", "def sweep_orphan_lyrics(" in src2)
ck("启动时挂自愈循环", "_spawn_bg_task(_lyric_orphan_loop())" in src2)
ck("取消收藏后也复扫一次（另有周期循环一处）",
   src2.count("await asyncio.to_thread(sweep_orphan_lyrics)") == 2,
   src2.count("await asyncio.to_thread(sweep_orphan_lyrics)"))

print("=== 16) v54 歌词落点：音频在曲库时必须贴身（回归守卫）===")
G_P = "online:netease:8200001"
libp = os.path.join(LIB, "回归甲 - 有音频.mp3")
with open(libp, "wb") as f:
    f.write(b"p" * 2048)
A.remember_media_path(G_P, libp)
# 制造回归场景：cache/ 里先存在一份「收藏前播放过」留下的歌词副本
shadow_p = mk(os.path.join(CACHE, A.cache_safe_guid(G_P) + ".lrc"), "[00:01.00]缓存副本\n")
ck("前提：cache 里已有旧歌词副本", os.path.isfile(shadow_p))
p = A.lyric_cache_path(G_P)
ck("音频在曲库 + cache 有旧副本 → 目标仍是曲库 sidecar",
   p == os.path.join(LIB, "回归甲 - 有音频.lrc"), p)
A.write_lyric_cache(G_P, "[00:01.00]缓存副本\n")
side_p = os.path.join(LIB, "回归甲 - 有音频.lrc")
ck("歌词落到曲库 sidecar", os.path.isfile(side_p), side_p)
ck("cache 影子副本被清掉", not os.path.exists(shadow_p), shadow_p)
ck(".lyricref 指向曲库 sidecar", A.recalled_lyric_path(G_P) == side_p, A.recalled_lyric_path(G_P))
ck("find_lyric_file 命中曲库 sidecar", A.find_lyric_file(G_P) == side_p, A.find_lyric_file(G_P))

# 无音频 + cache 有副本 → 目标仍是 cache（绝不因「已有歌词」而改判到曲库）
G_Q = "online:netease:8200002"
shadow_q = mk(os.path.join(CACHE, A.cache_safe_guid(G_Q) + ".lrc"), "[00:02.00]无主词\n")
p = A.lyric_cache_path(G_Q, title="无主歌", artist="无主歌手")
ck("无音频 + cache 有副本 → 目标仍是 cache", p == shadow_q, p)
ck("无音频 → 曲库不出现「歌手 - 歌名.lrc」",
   not os.path.exists(os.path.join(LIB, "无主歌手 - 无主歌.lrc")))
A.write_lyric_cache(G_Q, "[00:02.00]无主词\n", title="无主歌", artist="无主歌手")
ck("无音频写入后曲库仍无新增 .lrc",
   not os.path.exists(os.path.join(LIB, "无主歌手 - 无主歌.lrc")))

print("=== 17) v54 存量自愈：promote_one_lyric / promote_library_lyrics ===")
G_R = "online:netease:8200003"
libr = os.path.join(LIB, "自愈丙 - 有音频.flac")
with open(libr, "wb") as f:
    f.write(b"r" * 4096)
A.remember_media_path(G_R, libr)
src_r = mk(os.path.join(CACHE, A.cache_safe_guid(G_R) + ".lrc"), "[00:09.00]存量词\n")
side_r = os.path.join(LIB, "自愈丙 - 有音频.lrc")
ck("前提：曲库还没有 sidecar", not os.path.exists(side_r))
got = A.promote_one_lyric(G_R)
ck("提升返回目标路径", got == side_r, got)
ck("曲库 sidecar 已生成", os.path.isfile(side_r), side_r)
ck("内容原样搬运", open(side_r, encoding="utf-8").read().strip() == "[00:09.00]存量词",
   open(side_r, encoding="utf-8").read())
ck("cache 源文件已移走", not os.path.exists(src_r), src_r)
ck(".lyricref 指向曲库 sidecar", A.recalled_lyric_path(G_R) == side_r, A.recalled_lyric_path(G_R))
ck("无残留 .part", not [n for n in os.listdir(LIB) if n.endswith(".part")])

# 音频不在曲库 → 一根汗毛都不动
G_S = "online:netease:8200004"
src_s = mk(os.path.join(CACHE, A.cache_safe_guid(G_S) + ".lrc"), "[00:11.00]不该动\n")
ck("无曲库音频 → 不提升", A.promote_one_lyric(G_S) is None)
ck("无曲库音频 → cache 源文件仍在", os.path.exists(src_s))

# 曲库已有 sidecar → 只清影子副本，不覆盖
G_T = "online:netease:8200005"
libt = os.path.join(LIB, "自愈戊 - 有音频.mp3")
with open(libt, "wb") as f:
    f.write(b"t" * 2048)
A.remember_media_path(G_T, libt)
side_t = mk(os.path.join(LIB, "自愈戊 - 有音频.lrc"), "[00:10.00]曲库已有\n")
src_t = mk(os.path.join(CACHE, A.cache_safe_guid(G_T) + ".lrc"), "[00:10.00]曲库已有\n")
A.promote_one_lyric(G_T)
ck("曲库已有歌词 → 影子副本被清", not os.path.exists(src_t), src_t)
ck("曲库 sidecar 未被覆盖",
   open(side_t, encoding="utf-8").read().strip() == "[00:10.00]曲库已有",
   open(side_t, encoding="utf-8").read())

# 批量扫：曲库外的 .ref 绝不碰
G_SAFE = "online:netease:8200006"
outside_stem = os.path.join(SB, "outside", "越界歌")
mk(outside_stem + ".lrc", "[00:12.00]越界\n")
mk(A.media_ref_path(G_SAFE), outside_stem)
src_safe = mk(os.path.join(CACHE, A.cache_safe_guid(G_SAFE) + ".lrc"), "[00:12.00]越界\n")

# 再造两条错位的，验证批量扫
G_U1, G_U2 = "online:netease:8200007", "online:lx:kw:8200008"
u_pairs = []
for g, name, ext in ((G_U1, "批量己 - 有音频", "mp3"), (G_U2, "批量庚 - 有音频", "flac")):
    lp = os.path.join(LIB, f"{name}.{ext}")
    with open(lp, "wb") as f:
        f.write(b"u" * 2048)
    A.remember_media_path(g, lp)
    sp = mk(os.path.join(CACHE, A.cache_safe_guid(g) + ".lrc"), f"[00:13.00]{name}\n")
    u_pairs.append((os.path.join(LIB, f"{name}.lrc"), sp))

moved = A.promote_library_lyrics()
ck("批量提升返回非空", len(moved) >= 2, moved)
for dest, srcp in u_pairs:
    ck("批量：%s 已贴身" % os.path.basename(dest), os.path.isfile(dest), dest)
    ck("批量：%s 源已移走" % os.path.basename(srcp), not os.path.exists(srcp))
ck("越界词干未被搬动（曲库外）", os.path.exists(outside_stem + ".lrc"))
ck("越界 guid 的 cache 副本仍在", os.path.exists(src_safe))

print("=== 18) v54 开关 lyric_promote ===")
G_V = "online:netease:8200009"
libv = os.path.join(LIB, "开关辛 - 有音频.mp3")
with open(libv, "wb") as f:
    f.write(b"v" * 2048)
A.remember_media_path(G_V, libv)
src_v = mk(os.path.join(CACHE, A.cache_safe_guid(G_V) + ".lrc"), "[00:14.00]开关\n")
A.CONF["lyric_promote"] = False
ck("开关关闭 → 单个不提升", A.promote_one_lyric(G_V) is None)
ck("开关关闭 → 批量空操作", A.promote_library_lyrics() == [])
ck("开关关闭 → 源文件仍在", os.path.exists(src_v))
A.CONF["lyric_promote"] = True
ck("开关打开 → 能提升", A.promote_one_lyric(G_V) == os.path.join(LIB, "开关辛 - 有音频.lrc"))

print("=== 19) 源码级：v54 接线与回归守卫 ===")
src3 = open("/tmp/v54t/proxy/app.py", "r", encoding="utf-8").read()
ck("write_lyric_cache 已弃用「任意位置内容一致即返回」",
   "if text == read_lyric_cache(guid):" not in src3)
_ib = src3.split("def lyric_cache_path(", 1)[1].split("\ndef ", 1)[0]
ck("lyric_cache_path 中「音频在曲库」排在「已有歌词」之前",
   _ib.index("lib_audio = materialized_library_file(guid)") < _ib.index("found = find_lyric_file(guid)"))
ck("存在 promote_one_lyric", "def promote_one_lyric(" in src3)
ck("存在 promote_library_lyrics", "def promote_library_lyrics(" in src3)
ck("存在 _drop_shadow_lyric", "def _drop_shadow_lyric(" in src3)
ck("存在 _promote_lyric_file", "def _promote_lyric_file(" in src3)
ck("存在提升循环", "async def _lyric_promote_loop(" in src3)
ck("启动时挂提升循环", "_spawn_bg_task(_lyric_promote_loop())" in src3)
ck("整轨落盘后补位", "await asyncio.to_thread(promote_one_lyric, guid)" in src3)
ck("歌词读取命中缓存时幂等纠正",
   "await asyncio.to_thread(write_lyric_cache, guid, cached)" in src3)
ck("v53 孤儿自愈仍在", "def sweep_orphan_lyrics(" in src3
   and "_spawn_bg_task(_lyric_orphan_loop())" in src3)
ck("v53 曲库兜底确已移除",
   'return os.path.join(d, f"{library_basename(title, artist)}.lrc")' not in src3)
ck("v52 自动扫库仍在", 'request_library_scan("favdl")' in src3)
ck("v51 收藏整轨下载仍在", "async def _download_favorite_media(" in src3)

print("=== 20) v54 探针落盘 ===")
if os.path.exists(PROBE):
    body3 = open(PROBE, "r", encoding="utf-8", errors="replace").read()
    ck("含 [lyricpromo]", "[lyricpromo]" in body3)
    ck("含 [lyricshadow]（影子副本清理）", "[lyricshadow]" in body3)

print()
for f in (os.path.join(FAV, "u1.json"),):
    if os.path.exists(f):
        os.remove(f)
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL v54 UNIT CHECKS PASSED")
