#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v52 单元验收 = v51 全部检查 + 「落盘/删除后主动触发飞牛扫库」。

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

sys.path.insert(0, "/tmp/v52t/proxy")
import app as A  # noqa

SB = "/tmp/v52t"
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
src = open("/tmp/v52t/proxy/app.py", "r", encoding="utf-8").read()
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

print()
for f in (os.path.join(FAV, "u1.json"),):
    if os.path.exists(f):
        os.remove(f)
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL v52 UNIT CHECKS PASSED")
