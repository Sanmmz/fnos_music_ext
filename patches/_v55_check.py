#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v55 单元验收 = v54 关键回归 + 搜索「有海报 + 高音质」优先排序。

覆盖：
  · CONF 新键与 _search_rank_enabled（含 off）
  · _ext_quality_rank / _search_item_quality_rank（扩展名 / 码率 / 补全结果）
  · _search_item_has_poster（确定性判据：不看磁盘预热，避免排序抖动）
  · rank_search_items（cover_quality / quality_cover / off + 稳定性 + 不改本地）
  · _netease_quality_from_detail（sq/hr/h/m/l）
  · _netease_detail_bulk（一次批量请求 / 命中缓存不再请求 / 异常降级）
  · _enrich_search_items（补全封面+音质、写 meta、cap、幂等、开关、失败降级）
  · _session_page（guid 去重分配）/ _resync_published_pages（已发布页对齐）
  · build_online_track 封面兜底读 meta
  · 端到端：真实侦查形状（25 首 wy 全无封面）→ 补全后 top10 全部有海报且无损
"""
import asyncio
import json
import os
import sys
import urllib.parse

sys.path.insert(0, "/tmp/v55t/proxy")
import app as A  # noqa

SB = "/tmp/v55t"
HOME = SB + "/home"
COVER = HOME + "/cover_cache"
PROBE = HOME + "/access_probe.log"

fails = []


def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  >> " + str(extra)) if not cond else ""))
    if not cond:
        fails.append(name)


_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


# ------------------------------------------------------------------ 假 httpx
class _FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


class _FakeAsyncClient:
    calls = []
    payload = {}
    kw_payload = {}
    fail = False

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        type(self).calls.append(url)
        if type(self).fail:
            raise RuntimeError("boom")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "kuwo" in url:
            mid = (qs.get("mid") or [""])[0]
            d = type(self).kw_payload.get(mid)
            if d is None:
                return _FakeResp({"code": -1, "data": None})
            return _FakeResp({"code": 200, "data": d})
        raw = (qs.get("c") or ["[]"])[0]
        ids = [str(x.get("id")) for x in json.loads(raw)]
        return _FakeResp({"songs": [type(self).payload[i] for i in ids if i in type(self).payload]})


def _ne_song(i, lossless):
    d = {
        "id": i,
        "name": "歌%d" % i,
        "ar": [{"name": "歌手A"}, {"name": "歌手B"}],
        "al": {"name": "专辑%d" % i, "picUrl": "https://p1.music.126.net/abc%d==/x.jpg" % i},
        "dt": 231000,
        "h": {"br": 320000, "size": 9000000},
        "m": {"br": 192000, "size": 5000000},
        "l": {"br": 128000, "size": 3000000},
    }
    if lossless:
        d["sq"] = {"br": 1619359, "size": 64579972}
    return d


# 真实侦查形状：25 首，1001..1020 有无损(sq)，1021..1025 只有 320k
_FAKE = {str(i): _ne_song(i, i <= 1020) for i in range(1001, 1026)}
_FAKE["2001"] = _ne_song(2001, True)
_FakeAsyncClient.payload = _FAKE
_FakeAsyncClient.kw_payload = {
    "555": {"pic": "https://img1.kuwo.cn/star/albumcover/500/x.jpg", "hasLossless": False},
    "556": {"pic": None, "albumpic": None, "hasLossless": True},
    "557": {"pic": "https://img2.kuwo.cn/star/albumcover/500/y.jpg", "hasLossless": 1},
}

_REAL_HTTPX_CLIENT = A.httpx.AsyncClient


def _install_fake():
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.fail = False
    A.httpx.AsyncClient = _FakeAsyncClient


_install_fake()

# ================================================================ 0) 配置
print("=== 0) 配置 ===")
ck("search_rank 默认 cover_quality", A.CONF["search_rank"] == "cover_quality", A.CONF["search_rank"])
ck("search_enrich 默认开", A.CONF["search_enrich"] is True, A.CONF["search_enrich"])
ck("search_enrich_limit=30", A.CONF["search_enrich_limit"] == 30, A.CONF["search_enrich_limit"])
ck("search_enrich_wait_s=1.5", abs(A.CONF["search_enrich_wait_s"] - 1.5) < 1e-9, A.CONF["search_enrich_wait_s"])
ck("_search_rank_enabled() = True", A._search_rank_enabled() is True)
_keep = A.CONF["search_rank"]
for off in ("off", "OFF", "none", "0", "false", "disable", "disabled", " off "):
    A.CONF["search_rank"] = off
    ck("search_rank=%r -> 关闭" % off, A._search_rank_enabled() is False)
A.CONF["search_rank"] = _keep
ck("恢复 cover_quality", A._search_rank_enabled() is True)

# ================================================================ 1) 音质档位
print("=== 1) _ext_quality_rank ===")
for ext, want in [("flac", 3), ("FLAC", 3), (".flac", 3), ("audio/flac", 3), ("wav", 3), ("ape", 3),
                  ("wv", 3), ("aiff", 3), ("alac", 3), ("dsf", 3), ("dff", 3), ("tta", 3), ("tak", 3),
                  ("m4a", 2), ("aac", 2), ("opus", 2), ("ogg", 2), ("mp4", 2),
                  ("mp3", 1), ("MP3", 1), (".Mp3", 1), ("xyz", 1),
                  ("", 0), (None, 0)]:
    ck("ext=%r -> %d" % (ext, want), A._ext_quality_rank(ext) == want, A._ext_quality_rank(ext))

print("=== 2) _search_item_quality_rank ===")
for it, want in [
    ({"_quality_rank": 3}, 3),
    ({"_quality_rank": 0, "ext": "flac"}, 3),
    ({"ext": "flac"}, 3),
    ({"ext": "mp3"}, 1),
    ({"ext": "m4a"}, 2),
    ({"ext": "", "br": 1000000}, 3),
    ({"ext": "", "br": 320001}, 2),
    ({"ext": "", "br": 128001}, 1),
    ({"ext": "", "br": 0}, 0),
    ({"ext": "", "bitrate": 1411000}, 3),
    ({"ext": "", "audioSpec": {"bitrate": 1411000}}, 3),
    ({"ext": "", "audioSpec": {"bitrate": 320000}}, 2),
    ({"ext": "flac", "br": 128000}, 3),
    ({}, 0),
    # v55：扩展名与实际可用档位取 max —— 不得出现「显示 aac/flac 却按 mp3 排序」
    ({"_quality_rank": 1, "ext": "aac"}, 2),
    ({"_quality_rank": 1, "ext": "flac"}, 3),
    ({"_quality_rank": 2, "ext": "m4a"}, 2),
    ({"_quality_rank": 2, "ext": "mp3"}, 2),
    ({"_quality_rank": 3, "ext": "mp3"}, 3),
    ({"_quality_rank": 1, "ext": "aac", "br": 96000}, 2),
]:
    ck("quality(%r) -> %d" % (it, want), A._search_item_quality_rank(it) == want, A._search_item_quality_rank(it))

# ================================================================ 3) 封面判据
print("=== 3) 封面判据（确定性 / 不看磁盘预热）===")
os.makedirs(COVER, exist_ok=True)
g_disk = "online:lx:wy:7001"          # 只有磁盘缓存，没有 cover_url / meta
g_expired = "online:lx:wy:7002"
p = A._cover_guid_path(g_disk, 300)
with open(p, "wb") as f:
    f.write(b"\xff\xd8" + b"x" * 300)
p2 = A._cover_guid_path(g_expired, 300)
with open(p2, "wb") as f:
    f.write(b"\x89PNG" + b"x" * 300)
with open(p2 + ".exp", "w") as f:
    f.write("9999999999")
ck("item 自带 cover_url -> True",
   A._search_item_has_poster({"id": "lx:kw:1", "source": "lx", "cover_url": "http://x/a.jpg"}) is True)
ck("item 只有空格 cover_url -> 再看别处",
   A._search_item_has_poster({"id": "lx:wy:7004", "source": "lx", "cover_url": "   "}) is False)
ck("meta 无封面时 -> False",
   A._search_item_has_poster({"id": "lx:wy:7005", "source": "lx"}) is False)
A._meta_set("online:lx:wy:7005", {"cover_url": "https://p1.music.126.net/m==/x.jpg?param=300y300"})
ck("meta 写入后 -> True",
   A._search_item_has_poster({"id": "lx:wy:7005", "source": "lx"}) is True)
# ★ 核心：磁盘预热进度**不得**影响排序判据，否则首屏顺序会随手搜几次而抖动
ck("★ 只有磁盘缓存 -> 排序判据仍为 False（顺序必须与预热进度无关）",
   A._search_item_has_poster({"id": "lx:wy:7001", "source": "lx"}) is False)
ck("★ 占位图(.exp) 同样为 False",
   A._search_item_has_poster({"id": "lx:wy:7002", "source": "lx"}) is False)
ck("同一条目连续判定结果恒定",
   len({A._search_item_has_poster({"id": "lx:wy:7001", "source": "lx"}) for _ in range(5)}) == 1)
ck("非 online guid 不误判", A._search_item_has_poster({"guid": "8e8527a5651f487fbcddca5d61a60b6c"}) is False)
ck("非 dict -> False", A._search_item_has_poster("nope") is False)
ck("空 id -> False", A._search_item_has_poster({"source": "lx"}) is False)
ck("_has_cached_poster 已删除（不再作为排序输入）", not hasattr(A, "_has_cached_poster"))

# ================================================================ 4) 排序
print("=== 4) rank_search_items ===")


def _pool(with_poster=(), lossless=()):
    out = []
    for i in range(25):
        it = {"id": "lx:wy:%d" % (6000 + i), "source": "lx",
              "title": "t%d" % i, "artist": "a", "ext": "flac" if i in lossless else "mp3"}
        if i in with_poster:
            it["cover_url"] = "https://p1.music.126.net/%d==/x.jpg" % i
        out.append(it)
    return out


pool = _pool(with_poster=(3, 7, 20), lossless=(1, 2, 5, 8, 20))
orig = [x["title"] for x in pool]
ranked = A.rank_search_items(pool)
got = [x["title"] for x in ranked]
want = (["t20", "t3", "t7", "t1", "t2", "t5", "t8"]
        + ["t%d" % i for i in (0, 4, 6, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 24)])
ck("cover_quality: 海报+无损 → 有海报 → 无损 → 其余", got == want, got[:12])
ck("原列表未被就地打乱", [x["title"] for x in pool] == orig, [x["title"] for x in pool][:8])
ck("返回新列表", ranked is not pool)

ranked_q = A.rank_search_items(pool, mode="quality_cover")
got_q = [x["title"] for x in ranked_q]
# 音质优先：flac 组 {1,2,5,8,20} 全部在前（同档内 20 因为有海报再优先），其后 mp3 组里
# 有海报的 3/7 排在无海报的 0/4/6/… 之前。
want_q = (["t20", "t1", "t2", "t5", "t8", "t3", "t7"]
          + ["t%d" % i for i in (0, 4, 6, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 24)])
ck("quality_cover: 无损优先，同档海报优先", got_q == want_q, got_q[:12])

ck("off 原样返回", A.rank_search_items(pool, mode="off") is pool)
flat = [{"id": "lx:wy:%d" % (9000 + i), "source": "lx", "title": "f%d" % i,
         "artist": "a", "ext": "mp3", "cover_url": ""} for i in range(25)]
ck("权重全同 -> 顺序完全不变",
   [x["title"] for x in A.rank_search_items(flat)] == ["f%d" % i for i in range(25)],
   [x["title"] for x in A.rank_search_items(flat)][:8])
ck("单条原样", A.rank_search_items(pool[:1]) == pool[:1])
ck("空列表原样", A.rank_search_items([]) == [])
ck("非列表原样", A.rank_search_items(None) is None)

# ================================================================ 5) 网易云音质解析
print("=== 5) _netease_quality_from_detail ===")
ck("sq → (3,True)", A._netease_quality_from_detail({"sq": {"br": 1619359}}) == (3, True))
ck("hr → (3,True)", A._netease_quality_from_detail({"hr": {"br": 2000000}}) == (3, True))
ck("sq 优先于 h", A._netease_quality_from_detail({"sq": {"br": 1619359}, "h": {"br": 320000}}) == (3, True))
ck("空 sq 不算无损", A._netease_quality_from_detail({"sq": {}, "h": {"br": 320000}}) == (2, False))
ck("h → (2,False)", A._netease_quality_from_detail({"h": {"br": 320001}}) == (2, False))
ck("m → (1,False)", A._netease_quality_from_detail({"m": {"br": 192001}}) == (1, False))
ck("l → (1,False)", A._netease_quality_from_detail({"l": {"br": 128001}}) == (1, False))
ck("b → (1,False)", A._netease_quality_from_detail({"b": {"br": 96000}}) == (1, False))
ck("空 → (0,False)", A._netease_quality_from_detail({}) == (0, False))
ck("非 dict → (0,False)", A._netease_quality_from_detail(None) == (0, False))

# ================================================================ 6) 批量详情
print("=== 6) _netease_detail_bulk ===")
A._NE_DETAIL_CACHE.clear()
_install_fake()
out = run(A._netease_detail_bulk(["1001", "1021"]))
ck("一次请求拿到 2 首", len(out) == 2, list(out))
ck("只发 1 个 HTTP 请求", len(_FakeAsyncClient.calls) == 1, _FakeAsyncClient.calls)
ck("1001 无损", out["1001"]["quality_rank"] == 3 and out["1001"]["lossless"] is True, out["1001"])
ck("1021 非无损", out["1021"]["quality_rank"] == 2 and out["1021"]["lossless"] is False, out["1021"])
ck("封面带 300y300 缩略参数", out["1001"]["cover_url"].endswith("?param=300y300"), out["1001"]["cover_url"])
ck("标题/歌手/专辑/时长", (out["1001"]["title"] == "歌1001"
                     and out["1001"]["artist"] == "歌手A、歌手B"
                     and out["1001"]["album"] == "专辑1001"
                     and abs(out["1001"]["duration_s"] - 231.0) < 1e-6), out["1001"])
ck("写进 _NE_DETAIL_CACHE", "1001" in A._NE_DETAIL_CACHE)
_install_fake()
out2 = run(A._netease_detail_bulk(["1001", "1021"]))
ck("命中缓存：不再发请求", _FakeAsyncClient.calls == [] and len(out2) == 2, _FakeAsyncClient.calls)
_install_fake()
ck("非数字 id 被忽略", run(
    A._netease_detail_bulk(["abc", "", None])) == {}, )
ck("空列表不请求", _FakeAsyncClient.calls == [], _FakeAsyncClient.calls)
_install_fake()
_FakeAsyncClient.fail = True
ck("网络异常 → 空 dict 不抛",
   run(A._netease_detail_bulk(["3001"])) == {})
_FakeAsyncClient.fail = False
_install_fake()
run(A._netease_detail_bulk(["5005"]))
ck("失败不写负缓存（下次仍会重试）", len(_FakeAsyncClient.calls) == 1, _FakeAsyncClient.calls)
_install_fake()
r1 = run(A._netease_detail_bulk(["4004"]))
ck("首次问「服务端没有的 id」会请求", len(_FakeAsyncClient.calls) == 1 and r1 == {}, _FakeAsyncClient.calls)
_install_fake()
r2 = run(A._netease_detail_bulk(["4004"]))
ck("负缓存后不再请求（幂等）", _FakeAsyncClient.calls == [] and r2 == {}, _FakeAsyncClient.calls)

# ================================================================ 6.5) 酷我单曲详情
print("=== 6.5) _kw_poster_quality ===")
_install_fake()
ok1, cov1, ll1 = run(A._kw_poster_quality("555"))
ck("kw 详情：问到 + 封面 + 非无损",
   ok1 is True and cov1.startswith("https://img1.kuwo.cn/") and ll1 is False, (ok1, cov1, ll1))
ok2, cov2, ll2 = run(A._kw_poster_quality("556"))
ck("kw 详情：无封面但有无损", ok2 is True and cov2 == "" and ll2 is True, (ok2, cov2, ll2))
ok3, cov3, ll3 = run(A._kw_poster_quality("557"))
ck("kw 详情：hasLossless=1 视为无损",
   ok3 is True and ll3 is True and cov3.startswith("https://img2.kuwo.cn/"), (ok3, cov3, ll3))
ok4, _, _ = run(A._kw_poster_quality("999"))
ck("kw 详情：查不到 -> ok=False（下次仍会重试）", ok4 is False, ok4)
_install_fake()
_FakeAsyncClient.fail = True
ck("kw 详情：异常 -> ok=False 不抛", run(A._kw_poster_quality("555"))[0] is False)
_FakeAsyncClient.fail = False

# ================================================================ 7) 搜索补全
print("=== 7) _enrich_search_items ===")


def _mk(i, guid=None, **kw):
    it = {"id": guid or ("lx:wy:%d" % i), "source": "lx", "title": "歌%d" % i,
          "artist": "歌手A", "ext": "mp3", "duration_s": 231.0}
    it.update(kw)
    return it


A._NE_DETAIL_CACHE.clear()
_install_fake()
items = [_mk(1001), _mk(1021), _mk(9999), _mk(0, guid="8e8527a5651f487fbcddca5d61a60b6c"),
         _mk(0, guid="lx:kw:555", cover_url="https://img1.kuwo.cn/star/albumcover/s3s94/93/1.jpg")]
n = run(A._enrich_search_items(items, 30))
ck("补全 3 条（wy 两条详情 + kw 一条音质；9999 详情缺失，本地跳过）", n == 3, n)
ck("1001 拿到封面", str(items[0].get("cover_url")).startswith("https://p1.music.126.net/"), items[0].get("cover_url"))
ck("1001 无损 → ext 升级 flac", items[0]["ext"] == "flac", items[0]["ext"])
ck("1001 音质档 3", items[0]["_quality_rank"] == 3, items[0].get("_quality_rank"))
ck("1021 有封面但非无损 → ext 仍 mp3", items[1]["ext"] == "mp3", items[1]["ext"])
ck("1021 音质档 2", items[1]["_quality_rank"] == 2, items[1].get("_quality_rank"))
ck("详情缺失不动", "cover_url" not in items[2] or not items[2].get("cover_url"), items[2])
ck("本地 32hex 不动", "_quality_rank" not in items[3], items[3])
ck("已带封面不动", items[4]["cover_url"].startswith("https://img1.kuwo.cn/"), items[4]["cover_url"])
ck("kw 条目补上音质档（非无损 → 1）", items[4].get("_quality_rank") == 1, items[4].get("_quality_rank"))
ck("meta 已落盘 1001", A._meta_get("online:lx:wy:1001").get("quality_rank") == 3)
ck("meta 已落盘 1021 封面", bool(A._meta_get("online:lx:wy:1021").get("cover_url")))

_install_fake()
n2 = run(A._enrich_search_items(items, 30))
ck("幂等：第二次 0 条且不发请求", n2 == 0 and _FakeAsyncClient.calls == [], (n2, _FakeAsyncClient.calls))

# meta 回填（模拟进程重启后 meta 还在、条目是新的）
A._NE_DETAIL_CACHE.clear()
_install_fake()
fresh = [_mk(1001), _mk(1021)]
n3 = run(A._enrich_search_items(fresh, 30))
ck("新条目从 meta 回填，0 请求", n3 == 2 and _FakeAsyncClient.calls == [], (n3, _FakeAsyncClient.calls))
ck("回填封面", str(fresh[0].get("cover_url")).startswith("https://p1.music.126.net/"))
ck("回填无损 → flac", fresh[0]["ext"] == "flac")
ck("回填音质档", fresh[0].get("_quality_rank") == 3 and fresh[1].get("_quality_rank") == 2)

# cap 生效
A._NE_DETAIL_CACHE.clear()
_install_fake()
many = [_mk(i) for i in range(1001, 1026)]
nc = run(A._enrich_search_items(many, 3))
ck("cap=3 只处理前 3 条", nc == 3, nc)
ck("第 4 条未处理", "_quality_rank" not in many[3], many[3].get("_quality_rank"))
ck("cap 内请求仍只有 1 次", len(_FakeAsyncClient.calls) == 1, len(_FakeAsyncClient.calls))

# 开关 / 边界
A.CONF["search_enrich"] = False
_install_fake()
off_items = [_mk(1001)]
ck("search_enrich=False → 0 条不请求",
   run(A._enrich_search_items(off_items, 30)) == 0
   and _FakeAsyncClient.calls == [])
A.CONF["search_enrich"] = True
ck("cap=0 → 0", run(A._enrich_search_items([_mk(1001)], 0)) == 0)
ck("空列表 → 0", run(A._enrich_search_items([], 30)) == 0)

# 网络全挂时不得抛
A._NE_DETAIL_CACHE.clear()
_install_fake()
_FakeAsyncClient.fail = True
bad = [_mk(1101)]
ck("详情接口全挂 → 返回 0 不抛",
   run(A._enrich_search_items(bad, 30)) == 0)
ck("失败时条目未被污染", "cover_url" not in bad[0] or not bad[0].get("cover_url"))
_FakeAsyncClient.fail = False

# ================================================================ 7.5) kw 条目补全封面 + 音质
print("=== 7.5) kw 条目补全封面 + 音质（幂等）===")
A._NE_DETAIL_CACHE.clear()
_install_fake()
kw_items = [
    {"id": "lx:kw:555", "source": "lx", "title": "kw有封面无无损", "ext": "mp3"},
    {"id": "lx:kw:556", "source": "lx", "title": "kw无封面有无损", "ext": "mp3"},
    {"id": "lx:kw:557", "source": "lx", "title": "kw有封面有无损(1)", "ext": "mp3"},
]
n_kw = run(A._enrich_search_items(kw_items, 30))
ck("kw 三条全部补上", n_kw == 3, n_kw)
ck("555 封面解析成功", kw_items[0]["cover_url"].startswith("https://img1.kuwo.cn/"), kw_items[0].get("cover_url"))
ck("555 非无损 → 音质档 1", kw_items[0]["_quality_rank"] == 1, kw_items[0].get("_quality_rank"))
ck("556 无封面 → 音质档 3 + ext 升级 flac",
   kw_items[1]["_quality_rank"] == 3 and kw_items[1]["ext"] == "flac",
   (kw_items[1].get("_quality_rank"), kw_items[1]["ext"]))
ck("557 hasLossless=1 → 音质档 3", kw_items[2]["_quality_rank"] == 3, kw_items[2].get("_quality_rank"))
ck("kw 结果落 meta",
   bool(A._meta_get("online:lx:kw:555").get("cover_url"))
   and A._meta_get("online:lx:kw:556").get("quality_rank") == 3)
ck("556 的 no_cover 负标记落 meta", A._meta_get("online:lx:kw:556").get("no_cover") == 1,
   A._meta_get("online:lx:kw:556"))
_install_fake()
n_kw2 = run(A._enrich_search_items(kw_items, 30))
ck("kw 幂等：第二次 0 条 0 请求", n_kw2 == 0 and _FakeAsyncClient.calls == [], (n_kw2, _FakeAsyncClient.calls))
# 排序效果：(有海报, 音质档) 必须是单调不增 —— 有海报的 557/555 在前，
# 有损但无海报的 556 即使无损也只能排最后（海报优先于音质）。
kw_ranked = A.rank_search_items(kw_items)
kw_seq = [(1 if x.get("cover_url") else 0, x.get("_quality_rank") or 0) for x in kw_ranked]
ck("kw 排序：有海报优先、同档再比音质", kw_seq == [(1, 3), (1, 1), (0, 3)], kw_seq)
ck("kw 排序：序列单调不增", all(kw_seq[i] >= kw_seq[i + 1] for i in range(len(kw_seq) - 1)), kw_seq)

# ================================================================ 7.7) 分页对「重排」免疫（v55 关键修复）
print("=== 7.7) 分页重排免疫（_session_page guid 去重 + _resync_published_pages）===")


def _paged(n, **kw):
    out = []
    for i in range(1, n + 1):
        it = {"id": "lx:wy:%d" % (3000 + i), "source": "lx", "title": "P%d" % i,
              "artist": "A", "ext": "mp3"}
        it.update(kw)
        out.append(it)
    return out


# --- a) 先按原始顺序分配 page1（模拟 netease_wait_s 超时、补全还没跑完）
pool = _paged(40)
entry = {"items": list(pool), "pages": {}, "cursor": 0, "ts": 0, "keyword": "t"}
A.CONF["online_limit"] = 10
p1_raw = A._session_page(entry, 1, 20)
ck("page1 首次分配 10 条（online_limit）", len(p1_raw) == 10, len(p1_raw))
ck("page1 首次 = 原始顺序前 10", [x["title"] for x in p1_raw] == ["P%d" % i for i in range(1, 11)],
   [x["title"] for x in p1_raw])
ck("taken 已记录 10 个 guid", len(entry["taken"]) == 10, len(entry["taken"]))
ck("cursor = len(taken)", entry["cursor"] == 10, entry["cursor"])

# --- b) 补全 + 重排：只让 page1 内的 P5/P8 变成「有海报 + 无损」，
#        其余（含 page1 里其它 8 条）一律无海报 mp3 ⇒ 重排会**改变 page1 内部相对顺序**
for it in entry["items"]:
    if it["title"] in ("P5", "P8"):
        it["cover_url"] = "https://p1.music.126.net/x==/y.jpg"
        it["ext"] = "flac"
        it["_quality_rank"] = 3
    else:
        it["_quality_rank"] = 1
entry["items"] = A.rank_search_items(entry["items"])
ck("重排后池首变成有海报无损（P5）",
   entry["items"][0]["title"] == "P5"
   and A._search_item_has_poster(entry["items"][0])
   and A._search_item_quality_rank(entry["items"][0]) >= 3, entry["items"][0]["title"])

# --- c) 已发布页重新对齐：page1 的 guid 集合不变，但顺序换成池顺序
before_set = set(entry["pages"][1])
before_order = list(entry["pages"][1])
moved = A._resync_published_pages(entry)
ck("_resync_published_pages 报告有页被重排", moved == 1, moved)
ck("page1 guid 集合不变（不增删）", set(entry["pages"][1]) == before_set, entry["pages"][1])
ck("page1 顺序确实变了", entry["pages"][1] != before_order, entry["pages"][1][:4])
ck("page1 顺序已对齐到池顺序",
   entry["pages"][1] == [A.online_guid_from_item(x) for x in entry["items"]
                         if A.online_guid_from_item(x) in before_set],
   entry["pages"][1])
p1_after = A._session_page(entry, 1, 20)
ck("再次拉 page1：可见顺序已按池顺序（首条 = 池首 P5）",
   p1_after[0]["title"] == "P5"
   and A.online_guid_from_item(p1_after[0]) == A.online_guid_from_item(entry["items"][0]),
   p1_after[0]["title"])

# --- d) guid 去重分配：page2 不与 page1 重复，且取「最好的剩余」
p2 = A._session_page(entry, 2, 20)
g1 = {A.online_guid_from_item(x) for x in p1_after}
g2 = {A.online_guid_from_item(x) for x in p2}
ck("page2 分配 20 条", len(p2) == 20, len(p2))
ck("page1 ∩ page2 = ∅（不跨页重复）", not (g1 & g2), len(g1 & g2))
ck("page1 ∪ page2 = 30 条（不重不漏）", len(g1 | g2) == 30, len(g1 | g2))
ck("taken = 30", len(entry["taken"]) == 30, len(entry["taken"]))

# --- e) 再重排（幂等）：重复 resync 不再报 moved
entry["items"] = A.rank_search_items(entry["items"])
ck("重复 resync 幂等", A._resync_published_pages(entry) == 0)
p1_again = A._session_page(entry, 1, 20)
ck("page1 再次拉取仍不重复且集合一致",
   {A.online_guid_from_item(x) for x in p1_again} == g1, len(p1_again))

# --- f) guids 缺失 / 空 entry 不炸
ck("空 entry resync 返回 0", A._resync_published_pages({"items": [], "pages": {}}) == 0)
ck("pages 为空 resync 返回 0", A._resync_published_pages({"items": pool, "pages": {}}) == 0)
A.CONF["online_limit"] = 30

# ================================================================ 8) build_online_track 封面兜底
print("=== 8) build_online_track 封面兜底 ===")
A._meta_set("online:lx:wy:8001", {"cover_url": "https://p1.music.126.net/z==/y.jpg?param=300y300"})
t = A.build_online_track({"id": "lx:wy:8001", "source": "lx", "title": "兜底歌", "artist": "甲", "ext": "flac"})
ck("cover_url 从 meta 兜底", t["cover_url"].startswith("https://p1.music.126.net/z=="), t["cover_url"])
ck("coverUrl / coverURL 同步", t["coverUrl"] == t["cover_url"] and t["coverURL"] == t["cover_url"])
ck("coverId = guid", t["coverId"] == "online:lx:wy:8001", t["coverId"])
t2 = A.build_online_track({"id": "lx:wy:8002", "source": "lx", "title": "无封面", "ext": "mp3",
                           "cover_url": "http://own/a.jpg"})
ck("条目自带封面优先", t2["cover_url"] == "http://own/a.jpg", t2["cover_url"])
ck("无 meta 时不报错", A.build_online_track({"id": "lx:wy:8003", "source": "lx", "title": "x"})["cover_url"] == "")

# ================================================================ 9) 端到端（真实侦查形状）
print("=== 9) 端到端：25 首 wy 无封面 → 补全 → 排序 ===")
A._NE_DETAIL_CACHE.clear()
_install_fake()
real = [{"id": "lx:wy:%d" % i, "source": "lx", "title": "歌%d" % i, "artist": "周杰伦",
         "album": "", "duration_s": 0.0, "ext": "mp3", "cover_url": "", "file_size": 0,
         "lyric": "", "verified": False} for i in range(1001, 1026)]
before_cov = sum(1 for x in real if x.get("cover_url"))
before_ll = sum(1 for x in real if A._search_item_quality_rank(x) >= 3)
ck("补全前：0 条有封面", before_cov == 0, before_cov)
ck("补全前：0 条无损", before_ll == 0, before_ll)
n_e2e = run(A._enrich_search_items(real, 30))
ranked_e2e = A.rank_search_items(real)
after_cov = sum(1 for x in ranked_e2e if x.get("cover_url"))
after_ll = sum(1 for x in ranked_e2e if A._search_item_quality_rank(x) >= 3)
ck("补全后：25/25 有封面", after_cov == 25, after_cov)
ck("补全后：20/25 无损（与真机 20/25 一致）", after_ll == 20, after_ll)
top10 = ranked_e2e[:10]
ck("top10 全部「有封面 + 无损」",
   all(x.get("cover_url") and A._search_item_quality_rank(x) >= 3 for x in top10),
   [(x["title"], bool(x.get("cover_url")), A._search_item_quality_rank(x)) for x in top10])
ck("top10 内保持原始源顺序（稳定）",
   [x["title"] for x in top10] == ["歌%d" % i for i in range(1001, 1011)], [x["title"] for x in top10])
ck("320k 的 5 首排在无损之后",
   [x["title"] for x in ranked_e2e[20:]] == ["歌%d" % i for i in range(1021, 1026)],
   [x["title"] for x in ranked_e2e[20:]])
ck("补全请求只发 1 次", len(_FakeAsyncClient.calls) == 1, len(_FakeAsyncClient.calls))
tr = A.build_online_track(ranked_e2e[0])
ck("排序后首条可直接给 App 展示海报", tr["cover_url"].startswith("https://p1.music.126.net/"), tr["cover_url"])

# ================================================================ 10) 源码级守卫
print("=== 10) 源码级守卫 ===")
src = open("/tmp/v55t/proxy/app.py", encoding="utf-8").read()
agg = src.split("async def _aggregate_search(", 1)[1].split("\nasync def ", 1)[0]
ck("_aggregate_search 里先补全后排序",
   agg.index('await _enrich_search_items(entry["items"], CONF["search_enrich_limit"])')
   < agg.index('entry["items"] = rank_search_items(pool)'))
ck("重排后必须对齐已发布页（resync）",
   "moved = _resync_published_pages(entry)" in agg
   and "def _resync_published_pages(" in src)
ck("分页改为 guid 去重分配（池子重排免疫）",
   'taken = entry.get("taken")' in src and 'entry["cursor"] = len(taken)' in src)
ck("排序只作用于在线块（entry['items']）", 'rank_search_items(upstream_json' not in src)
ck("search_track 已接封面预热",
   '_prefetch_online_covers((merged.get("data") or {}).get("list") or [])' in src)
ck("[searchrank] 探针存在", '_probe_write("[searchrank] n=%d' in src)
ck("[searchenrich] 探针存在", '_probe_write("[searchenrich] cap=%d' in src)
ck("首屏等待重排落定（search_rank_wait_s）",
   'timeout=float(CONF["search_rank_wait_s"])' in src)
_st = src.split("async def search_track(", 1)[1].split("\nasync def ", 1)[0]
ck("排序兜底在 _session_page 分配之前",
   _st.index('entry["items"] = rank_search_items(entry["items"])')
   < _st.index("selected = _session_page(entry, page, size)"))
ck("本地曲库结果仍在最前（append 到 target_list）", "target_list.append(build_online_track(it))" in src)
ck("v54 歌词提升补丁仍在", "def promote_one_lyric(" in src and "def _drop_shadow_lyric(" in src)
ck("v53 孤儿歌词清理仍在", "def sweep_orphan_lyrics(" in src)
ck("v52 扫库触发仍在", "def request_library_scan(" in src)
ck("v50 仅收藏落盘仍在", "def _download_favorite_media(" in src)

A.httpx.AsyncClient = _REAL_HTTPX_CLIENT

print()
print("=== 探针 ===")
try:
    with open(PROBE, encoding="utf-8", errors="replace") as f:
        lines = [l.rstrip() for l in f if "[searchrank]" in l]
    print("  " + ("\n  ".join(lines) if lines else "（本沙箱未走到聚合，正常）"))
except Exception as e:
    print("  （无探针：%s）" % e)

print()
print("=" * 60)
print("FAILS = %d" % len(fails))
for f in fails:
    print("  -", f)
print("=" * 60)
sys.exit(1 if fails else 0)
