"""增量式补丁构建器（v54 -> v55）：搜索结果「有海报 + 高音质」优先排序。

输入：nas_src/app_v54.py
输出：nas_src/app_v55.py

===== 用户诉求（2026-09-18）=====
「搜索歌曲能否优先将有海报并且高质量音源排在前面显示」

===== 真机侦查（NAS，2026-09-18）=====
搜索 "周杰伦" 走 `/music/api/v1/search/track?q=` 拿到 28 条在线结果，全部来自 lx：
    src=lx  ext=mp3/aac  cover=- (27/28)  size 多为 0
进一步问三个音源后端：
  · lx(:8772)  30 条，几乎全是 lx_source=wy（网易云）；cover_url 恒空（wx 搜索响应里没有 albumpic）
  · musicbox(:8770) 只有 1 条（免登录网易搜索结果极少），但有 songs/detail → album_pic_url + has_sq
  · musicdl(:8768) Migu/Kuwo 双双 timeout 3.0s → 0 条
⇒ 当前搜索页**几乎 100% 是「无海报 + 标称 mp3」**：用户诉求命中的正就是这个痛点。

而网易云官方详情一次**批量**请求就同时给出封面与音质：
    GET https://music.163.com/api/v3/song/detail?c=[{"id":1},{"id":2},...]
    25 首耗时 0.171s；al.picUrl 有封面 25/25；sq(无损) 20/25；h.br=320000 25/25
    封面 300x300 缩略图 26KB / 0.072s
酷我 musicInfo 同样带 pic / albumpic / hasLossless（kw 分支兜底用）。

===== v55 做法 =====
1) **补全**（`_enrich_search_items`）：搜索结果一成型就按 guid 解析「封面 + 音质档」，
   网易云走**一次批量请求**（0.2s 级），并把结果写进 meta_cache 持久缓存，
   与剩余音源的网络等待重叠，不占第一页等待预算。失败静默降级 = 保持 v54 行为。
2) **排序**（`rank_search_items`）：稳定重排在线块，档位 有海报+无损 → 有海报+320k → 有海报 → 无损 → …，
   本地曲库结果不受影响（它们由上游 JSON 提供，永远在最前）。
3) **封面预热**（`search_track` 里调 `_prefetch_online_covers`）：与每日推荐同款做法。
   此前搜索页缺这一步 ⇒ App 首次拉 /static/cover 要走 lx(~4s) ⇒ 超时 ⇒ 只剩占位图。
4) `build_online_track` 封面兜底读 meta_cache，让任意入口都能拿到 coverUrl。

开关（.env，全部可选）：FNMUSIC_SEARCH_RANK=cover_quality|quality_cover|off（默认 cover_quality）
                          FNMUSIC_SEARCH_ENRICH=true|false（默认 true）
                          FNMUSIC_SEARCH_ENRICH_LIMIT（默认 30，只补第一屏）
                          FNMUSIC_SEARCH_ENRICH_WAIT_S（默认 1.5）
"""
import io
import os

# 构建机本地路径已脱敏：默认读同目录下的 app_v54.py，产物写 app_v55.py；
# 也可用环境变量覆盖（见 patches/README.md）。
BASE = os.environ.get("FNMUSIC_BASE_APP", "app_v54.py")
OUT = os.environ.get("FNMUSIC_OUT_APP", "app_v55.py")

src = io.open(BASE, "r", encoding="utf-8").read()
repls = []

# ================================================================ 1) CONF 新增
OLD = '''    "lyric_promote": os.environ.get("FNMUSIC_LYRIC_PROMOTE", "true").lower() in ("true", "1", "yes"),
'''
assert src.count(OLD) == 1, "CONF v54 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    "lyric_promote": os.environ.get("FNMUSIC_LYRIC_PROMOTE", "true").lower() in ("true", "1", "yes"),
    # v55 搜索结果按「有海报 + 高音质」优先排序
    "search_rank": os.environ.get("FNMUSIC_SEARCH_RANK", "cover_quality"),
    "search_enrich": os.environ.get("FNMUSIC_SEARCH_ENRICH", "true").lower() in ("true", "1", "yes"),
    "search_enrich_limit": int(os.environ.get("FNMUSIC_SEARCH_ENRICH_LIMIT", "30")),
    "search_enrich_wait_s": float(os.environ.get("FNMUSIC_SEARCH_ENRICH_WAIT_S", "1.5")),
    "search_rank_wait_s": float(os.environ.get("FNMUSIC_SEARCH_RANK_WAIT_S", "1.5")),
'''))

# ================================================================ 2) 新增 v55 模块
V55_BLOCK = '''# === v55: 搜索结果「有海报 + 高音质」优先 ===
#
# 用户诉求：「搜索歌曲优先把有海报并且高质量音源的排在前面显示」。
#
# 真机实测（2026-09-18）：搜索命中的在线曲目几乎全部来自 lx→wy（网易云），
#   · lx 搜索响应里 cover_url 恒空、ext 恒 "mp3" ⇒ 列表「无海报 + 无音质信息」
#   · 而网易云官方详情一次批量请求（25 首 0.17s）同时给出 al.picUrl 与 sq/hr/h 质量对象
# 于是 v55 先**补全**（封面 + 音质档），再**排序**（有海报且无损/高码率 → 前排）。
# 补全只针对第一屏（`search_enrich_limit`），结果写进 meta_cache 持久缓存，
# 与剩余音源的网络等待重叠，失败静默降级为 v54 原行为。

_QUALITY_LOSSLESS_EXTS = ("flac", "wav", "ape", "wv", "aiff", "aif", "alac", "dsf", "dff", "tta", "tak")
_QUALITY_HQ_EXTS = ("m4a", "aac", "opus", "ogg", "mp4")

_SEARCH_RANK_OFF = ("off", "none", "no", "0", "false", "disable", "disabled")


def _search_rank_enabled() -> bool:
    return str(CONF.get("search_rank") or "").strip().lower() not in _SEARCH_RANK_OFF


def _ext_quality_rank(ext) -> int:
    """扩展名 → 音质档：3=无损 / 2=高码率有损 / 1=普通有损 / 0=未知。"""
    e = str(ext or "").strip().lower().lstrip(".")
    if e.startswith("audio/"):
        e = e.split("/", 1)[-1]
    if e in _QUALITY_LOSSLESS_EXTS:
        return 3
    if e in _QUALITY_HQ_EXTS:
        return 2
    if e:
        return 1
    return 0


def _search_item_quality_rank(item: dict) -> int:
    """条目音质档：补全结果 → 扩展名 → 码率。"""
    if not isinstance(item, dict):
        return 0
    q = item.get("_quality_rank")
    if not (isinstance(q, int) and q > 0):
        q = 0
    rank = _ext_quality_rank(item.get("ext"))
    # v55：扩展名（源声明、且会决定实际取流容器）与实际可用档位取**较大者**。
    # 只信补全档位会出现「列表显示 AAC/FLAC，却被当成 mp3 排在后面」的自相矛盾
    # （真机踩到：ext=aac 的曲目被 NetEase 的 l 档标成 1，排在 mp3 之后）。
    # 取 max 只影响排序，不改 ext，因此对取流零风险。
    if rank > q:
        q = rank
    if q > 1:
        return q
    for key in ("br", "bitrate", "audioSpec"):
        v = item.get(key)
        if isinstance(v, dict):
            v = v.get("bitrate")
        try:
            br = int(float(v or 0))
        except (TypeError, ValueError):
            br = 0
        if br >= 900000:
            return 3
        if br >= 256000:
            return max(q, 2)
        if br > 0:
            return max(q, 1)
    return max(q, rank)


def _search_item_has_poster(item: dict) -> bool:
    """「有海报」判据（**排序专用**）：条目自带 cover_url，或 meta 里已有封面 URL。

    ★ 刻意**不看磁盘封面缓存**。磁盘只代表「本机预热进度」，会随着
    `_prefetch_online_covers` 陆续落盘、以及缓存过期（`.exp`）而变化；
    把它当排序键，首屏顺序就会随预热进度反复抖动
    （真机实测：重启后第一次搜索与几秒后的结果顺序不同，同一关键词连搜两次不一致）。
    封面 URL 才是曲目的固有属性，而且与 `build_online_track` 真正返回给 App 的
    coverUrl 判据完全一致 —— 响应里有封面 ⇔ 排序认为有海报。
    """
    if not isinstance(item, dict):
        return False
    if str(item.get("cover_url") or "").strip():
        return True
    try:
        guid = online_guid_from_item(item)
    except Exception:
        return False
    if not guid:
        return False
    try:
        return bool(str((_meta_get(guid) or {}).get("cover_url") or "").strip())
    except Exception:
        return False


def rank_search_items(items: list[dict], mode: str | None = None) -> list[dict]:
    """稳定重排在线搜索结果：有海报 + 高音质优先。

    mode=cover_quality（默认）海报优先，同档再比音质；
    mode=quality_cover 音质优先，同档再比海报；
    mode=off 不排序。同档保持原始源顺序（idx 兜底 ⇒ 完全稳定）。
    """
    mode = str(mode or CONF.get("search_rank") or "cover_quality").strip().lower()
    if mode in _SEARCH_RANK_OFF or not isinstance(items, list) or len(items) < 2:
        return items

    def sort_key(pair):
        idx, it = pair
        poster = 1 if _search_item_has_poster(it) else 0
        quality = _search_item_quality_rank(it)
        if mode == "quality_cover":
            return (-quality, -poster, idx)
        return (-poster, -quality, idx)

    return [it for _idx, it in sorted(enumerate(items), key=sort_key)]


def _netease_quality_from_detail(s: dict) -> tuple:
    """网易云详情 → (音质档, 是否无损)。sq/hr=无损；h=320k；m=192k；l=128k。"""
    if not isinstance(s, dict):
        return 0, False
    if s.get("sq") or s.get("hr"):
        return 3, True
    h = s.get("h")
    if isinstance(h, dict) and h.get("br"):
        return 2, False
    for key in ("m", "l", "b"):
        v = s.get(key)
        if isinstance(v, dict) and v.get("br"):
            return 1, False
    return 0, False


async def _netease_detail_bulk(ids: list, timeout: float = 6.0) -> dict:
    """**一次请求**解析多首网易云详情：封面 + 音质档（v55 搜索补全的主力）。

    返回 {song_id: {"cover_url", "quality_rank", "lossless", "title", "artist", "album", "duration_s"}}。
    结果写进 `_NE_DETAIL_CACHE`（与单曲 `_netease_detail()` 共用）并打 `_bulk` 标记，
    使同一 keyword 的再次聚合**零网络请求**；请求成功但服务端未返回的 id
    （已下架等）也做**负缓存**，不会每次搜索都重问一遍。
    """
    out: dict = {}
    uniq = [i for i in dict.fromkeys(str(x).strip() for x in (ids or []) if str(x).strip().isdigit())]
    if not uniq:
        return out
    missing = []
    for sid in uniq:
        cached = _NE_DETAIL_CACHE.get(sid)
        if not (isinstance(cached, dict) and cached.get("_bulk")):
            missing.append(sid)
        elif cached.get("cover_url") or cached.get("quality_rank"):
            out[sid] = cached
        # 负缓存（请求过、服务端没这首）既不再请求，也不放进结果
    if not missing:
        return out
    songs = []
    ok = False
    try:
        payload = quote(json.dumps([{"id": int(s)} for s in missing], separators=(",", ":")))
        url = "https://music.163.com/api/v3/song/detail?c=" + payload
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                   "Referer": "https://music.163.com/"}
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
        if r.status_code == 200:
            raw = (r.json() or {}).get("songs")
            if isinstance(raw, list):
                songs = raw
                ok = True
    except Exception as e:
        logger.debug("v55 netease bulk detail failed: %s", e)
    for s in songs:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "")
        if not sid:
            continue
        al = s.get("al") if isinstance(s.get("al"), dict) else {}
        pic = str(al.get("picUrl") or "")
        if pic:
            pic = pic + ("&param=300y300" if "?" in pic else "?param=300y300")
        qrank, lossless = _netease_quality_from_detail(s)
        try:
            dt = float(s.get("dt") or s.get("duration") or 0)
        except (TypeError, ValueError):
            dt = 0.0
        info = {
            "title": str(s.get("name") or ""),
            "artist": "、".join(str(a.get("name") or "") for a in (s.get("ar") or []) if isinstance(a, dict) and a.get("name")),
            "album": str(al.get("name") or ""),
            "cover_url": pic,
            "quality_rank": qrank,
            "lossless": lossless,
            "duration_s": (dt / 1000.0) if dt > 3600 else dt,
            "_bulk": True,
        }
        _NE_DETAIL_CACHE[sid] = info
        out[sid] = info
    if ok:
        # 负缓存：只在请求**成功**时才标记（失败留白，下次仍会重试）
        for sid in missing:
            if sid not in out:
                try:
                    _NE_DETAIL_CACHE.setdefault(sid, {})["_bulk"] = True
                except Exception:
                    pass
    return out


async def _kw_poster_quality(rid: str) -> tuple:
    """酷我单曲详情：封面 + 是否无损。返回 (是否问到, cover_url, has_lossless)。

    kw 搜索响应里既没有封面也没有音质信息（web_albumpic_short 常为空），
    只能靠 musicInfo 兜底。实测 0.11s/首、并行即可。
    """
    url = "https://wapi.kuwo.cn/api/www/music/musicInfo?mid=%s" % rid
    headers = {"Referer": "https://www.kuwo.cn/",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            return False, "", False
        data = (r.json() or {}).get("data")
        if not isinstance(data, dict) or not data:
            return False, "", False
    except Exception:
        return False, "", False
    cover = str(data.get("pic") or data.get("albumpic") or "")
    raw = data.get("hasLossless")
    has_lossless = raw is True or str(raw).strip().lower() in ("1", "true")
    return True, cover, has_lossless


async def _enrich_search_items(items: list, cap: int = 30) -> int:
    """给搜索结果补「封面 + 音质档」，并写进 meta_cache 供 /static/cover 秒回。

    只处理前 cap 条（用户第一屏）。全部失败也只是「不补」，绝不抛错。
    返回补全成功的条数。
    """
    if not items or not CONF.get("search_enrich"):
        return 0
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = 30
    if cap <= 0:
        return 0

    # ---- 0) 先用已有的 meta 缓存回填（老结果 / 上一次搜索留下的） ----
    pending = []
    done: set = set()
    for it in items[:cap]:
        if not isinstance(it, dict):
            continue
        try:
            guid = online_guid_from_item(it)
        except Exception:
            guid = ""
        if not guid:
            continue
        try:
            meta = _meta_get(guid) or {}
        except Exception:
            meta = {}
        applied = False
        mcover = str(meta.get("cover_url") or "")
        if mcover and not str(it.get("cover_url") or "").strip():
            it["cover_url"] = mcover
            applied = True
        mq = meta.get("quality_rank")
        if isinstance(mq, int) and mq > 0 and not isinstance(it.get("_quality_rank"), int):
            it["_quality_rank"] = mq
            applied = True
        if meta.get("lossless") and _ext_quality_rank(it.get("ext")) < 3:
            it["ext"] = "flac"
            applied = True
        if applied:
            done.add(guid)
        # no_cover 是「问过、对方就是没有封面」的负缓存标记，
        # 否则这类条目每次都算「缺封面」，每轮搜索都要重问一遍。
        need_cover = (not str(it.get("cover_url") or "").strip()) and not meta.get("no_cover")
        need_quality = not isinstance(it.get("_quality_rank"), int)
        if need_cover or need_quality:
            pending.append((guid, it))
    if not pending:
        # 全部命中缓存（含 meta 回填）时也要留痕：否则探针看起来「没跑过」，
        # 排查时无法区分「已缓存完成」与「根本没执行」。
        _probe_write("[searchenrich] cap=%d pending=0 ne=0 kw=0 resolved=%d" % (cap, len(done)))
        return len(done)

    # ---- 1) 网易云批量详情（覆盖 lx→wy / netease 两个源，一次请求） ----
    ne_rows: dict = {}
    for guid, it in pending:
        try:
            sid = _netease_song_id(guid)
        except Exception:
            sid = ""
        if sid:
            ne_rows.setdefault(sid, []).append((guid, it))
    if ne_rows:
        try:
            bulk = await _netease_detail_bulk(list(ne_rows))
        except Exception as e:
            logger.debug("v55 bulk enrich failed: %s", e)
            bulk = {}
        for sid, rows in ne_rows.items():
            info = bulk.get(sid) or {}
            cover = str(info.get("cover_url") or "")
            qrank = info.get("quality_rank") or 0
            lossless = bool(info.get("lossless"))
            if not cover and not qrank:
                continue
            for guid, it in rows:
                patch = {}
                if cover and not str(it.get("cover_url") or "").strip():
                    it["cover_url"] = cover
                    patch["cover_url"] = cover
                elif not cover:
                    patch["no_cover"] = 1
                if isinstance(qrank, int) and qrank > 0:
                    it["_quality_rank"] = qrank
                    patch["quality_rank"] = qrank
                if lossless:
                    patch["lossless"] = True
                    if _ext_quality_rank(it.get("ext")) < 3:
                        it["ext"] = "flac"
                if patch:
                    try:
                        _meta_set(guid, patch)
                    except Exception:
                        pass
                    done.add(guid)

    # ---- 2) 酷我（lx→kw）单曲详情：musicInfo 带 pic + hasLossless（并发、限流） ----
    kw_rows = [(g, it) for g, it in pending if str(g).startswith("online:lx:kw:")][:cap]
    if kw_rows:
        sem = asyncio.Semaphore(6)

        async def _one_kw(guid, it):
            rid = str(guid).rsplit(":", 1)[-1]
            if not rid.isdigit():
                return 0
            async with sem:
                ok, cover, has_lossless = await _kw_poster_quality(rid)
            if not ok:
                return 0
            patch = {}
            cover = _normalize_cover_url(cover)
            if cover and not str(it.get("cover_url") or "").strip():
                it["cover_url"] = cover
                patch["cover_url"] = cover
            elif not cover:
                patch["no_cover"] = 1
            # 无论是否无损都记下档位：否则条目永远「缺音质」，每次搜索都要重问一遍
            qrank = 3 if has_lossless else 1
            it["_quality_rank"] = qrank
            patch["quality_rank"] = qrank
            if has_lossless:
                patch["lossless"] = True
                if _ext_quality_rank(it.get("ext")) < 3:
                    it["ext"] = "flac"
            try:
                _meta_set(guid, patch)
            except Exception:
                pass
            return 1

        try:
            got = await asyncio.wait_for(
                asyncio.gather(*[_one_kw(g, it) for g, it in kw_rows], return_exceptions=True),
                timeout=max(1.0, float(CONF["search_enrich_wait_s"]) + 1.5),
            )
            for (guid, _it), x in zip(kw_rows, got):
                if isinstance(x, int) and x > 0:
                    done.add(guid)
        except Exception as e:
            logger.debug("v55 kw enrich failed: %s", e)

    resolved = len(done)
    _probe_write("[searchenrich] cap=%d pending=%d ne=%d kw=%d resolved=%d" % (
        cap, len(pending), len(ne_rows), len(kw_rows), resolved))
    return resolved


'''
OLD = '''@app.get("/music/api/v1/search/track")
@app.get("/music/api/v1/search/track/{subpath:path}")
async def search_track(request: Request):'''
assert src.count(OLD) == 1, "search_track 锚点 count=%d" % src.count(OLD)
repls.append((OLD, V55_BLOCK + OLD))

# ================================================================ 3) _aggregate_search：补全 + 排序
OLD = '''    tasks = [asyncio.create_task(coro) for coro in sources]
    pending = set(tasks)
    partial = False
    results: dict[asyncio.Task, list] = {}
    try:'''
assert src.count(OLD) == 1, "_aggregate_search 头部锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    tasks = [asyncio.create_task(coro) for coro in sources]
    pending = set(tasks)
    partial = False
    results: dict[asyncio.Task, list] = {}
    enrich_task: asyncio.Task | None = None
    try:'''))

OLD = '''                entry["items"] = deduplicate_online_items(ordered)[:2000]
        entry["partial"] = partial
        entry["ts"] = time.time()
    finally:'''
assert src.count(OLD) == 1, "_aggregate_search 循环锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''                entry["items"] = deduplicate_online_items(ordered)[:2000]
                # v55: 每拿到一批源结果就**就地重排一次**。这样即便 netease_wait_s
                # 到点时聚合还没跑完（真机常态），第一页分配的也已经是「有海报 +
                # 高音质」优先的顺序；等源到齐后再用完整信息重排 + resync，
                # 顺序只会更准。排序幂等，多排几次无副作用。
                if _search_rank_enabled():
                    entry["items"] = rank_search_items(entry["items"])
                    _resync_published_pages(entry)
                # v55: 一拿到结果就补封面/音质，和剩余音源的网络等待重叠，
                # 不把补全耗时加到「第一页」的等待预算上。
                if _search_rank_enabled() and CONF.get("search_enrich") and (enrich_task is None or enrich_task.done()):
                    enrich_task = asyncio.create_task(
                        _enrich_search_items(entry["items"], CONF["search_enrich_limit"])
                    )
        entry["partial"] = partial
        # v55: 结果集定型 → 补齐（缓存命中即毫秒级）→ 按「有海报 + 高音质」重排。
        # 分页已改为「按 guid 去重分配 + 已发布页重排后对齐」，因此这里可以
        # 无条件重排：既不会跨页重复，也不会出现「排序了但首屏没变」。
        if _search_rank_enabled() and CONF.get("search_enrich"):
            if enrich_task is not None and not enrich_task.done():
                await asyncio.wait({enrich_task}, timeout=float(CONF["search_enrich_wait_s"]))
            try:
                await _enrich_search_items(entry["items"], CONF["search_enrich_limit"])
            except Exception as e:
                logger.debug("v55 enrich pass failed: %s", e)
                _probe_write("[searchenrich] FAILED %s" % e)
        if _search_rank_enabled():
            pool = entry["items"]
            p_before = sum(1 for x in pool[:30] if _search_item_has_poster(x))
            entry["items"] = rank_search_items(pool)
            # v55：池子重排后，把**已发布页**的 guid 顺序重新对齐（只换序，不增删）。
            # 第一页常常在「补全封面/音质 + 重排」完成前就按 guid 固定，
            # 不对齐的话 App 首屏拿到的仍是原始顺序 ⇒ 排序看不见。
            # 对齐后任何一次拉取 / 下拉刷新即为排好序的顺序。
            moved = _resync_published_pages(entry)
            p_after = sum(1 for x in entry["items"][:30] if _search_item_has_poster(x))
            q_top = sum(1 for x in entry["items"][:10] if _search_item_quality_rank(x) >= 3)
            _probe_write("[searchrank] n=%d poster(top30) %d->%d lossless(top10)=%d moved=%d head=%s" % (
                len(entry["items"]), p_before, p_after, q_top, moved,
                str((entry["items"][0] or {}).get("title") or "")[:24] if entry["items"] else ""))
            _probe_write("[poolrank] kw=%s " % keyword + " ".join("%d%d:%s" % (
                1 if _search_item_has_poster(x) else 0, _search_item_quality_rank(x),
                str((x or {}).get("title") or "")[:8]) for x in entry["items"][:14]))
        entry["ts"] = time.time()
    finally:'''))

# ================================================================ 3.5) search_track：首屏等「补全+重排」落定
# 真机踩坑：netease_wait_s=3.0s 到点时聚合还差几十毫秒才结束，
# 于是 page=1 用**补全前的原始顺序**分配并固定（页码按 guid 固定），
# 之后无论怎么重排都改不了首屏 ⇒ [searchrank] 探针为空、排序等于没生效。
OLD = '''        else:
            await asyncio.wait({task}, timeout=float(CONF["late_page_wait_s"]))
    local_list = ensure_search_list(upstream_json)'''
assert src.count(OLD) == 1, "search_track 首屏等待锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''        else:
            await asyncio.wait({task}, timeout=float(CONF["late_page_wait_s"]))
    if _search_rank_enabled() and task is not None and not task.done() and entry["items"] and not entry["pages"]:
        # v55: 首页再多等一小会儿，让「补全封面/音质 → 重排」在页码分配之前完成。
        # 页码一旦分配就按 guid 固定，之后重排也改不了首屏顺序。
        # 实测聚合只比 netease_wait_s 晚几十毫秒结束，这次等待通常瞬间返回。
        await asyncio.wait({task}, timeout=float(CONF["search_rank_wait_s"]))
    local_list = ensure_search_list(upstream_json)'''))

# ================================================================ 3.6) 分页对「重排」免疫
# 真机踩坑：netease_wait_s 到点时聚合还差几十~几百毫秒，page=1 用**补全前的原始顺序**
# 分配并按 guid 固定，之后无论如何重排都改不了首屏 ⇒ 排序白做（[searchrank] 探针为空）。
# 修法：① 分配改为按 guid 去重（池子重排后仍「取最好的剩余条目」，不跨页重复）；
#       ② 已发布页在重排后**重新对齐顺序**（只换序不增删），下次拉取即为排好序的。
OLD = '''def _session_page(entry: dict, page: int, size: int) -> list[dict]:'''
assert src.count(OLD) == 1, "_session_page 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''def _resync_published_pages(entry: dict) -> int:
    """把**已发布页**的 guid 顺序对齐到最新排序（只换序，不增删 ⇒ 不会跨页重复）。

    第一页常常在「补全封面/音质 + 重排」完成前就被分配并固定下来，
    于是 App 首屏拿到的是原始顺序。这里把已发布页的 guid 顺序重排成新的池顺序，
    随后任何一次拉取 / 下拉刷新 / 再次进入搜索，看到的就是「有海报 + 高音质」优先的顺序。
    """
    moved = 0
    try:
        order = {}
        for idx, item in enumerate(entry.get("items") or []):
            order.setdefault(online_guid_from_item(item), idx)
        for _page, guids in (entry.get("pages") or {}).items():
            if not guids:
                continue
            before = list(guids)
            guids.sort(key=lambda g: order.get(g, 1 << 30))
            if before != guids:
                moved += 1
    except Exception:
        pass
    return moved


def _session_page(entry: dict, page: int, size: int) -> list[dict]:'''))

OLD = '''    if page == max(pages):
        start = entry["cursor"]
        allocated = entry["items"][start:start + max(0, count - len(pages[page]))]
        pages[page].extend(online_guid_from_item(item) for item in allocated)
        entry["cursor"] += len(allocated)'''
assert src.count(OLD) == 1, "_session_page 分配锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    if page == max(pages):
        # v55: 按 **guid 去重** 分配，而不是按下标切片 —— 池子会在「补全」后被重排，
        # 下标会漂移；按 guid 去重才能既「永远取最好的剩余条目」又不跨页重复。
        taken = entry.get("taken")
        if not isinstance(taken, set):
            taken = set(taken or [])
            entry["taken"] = taken
        want = max(0, count - len(pages[page]))
        if want:
            for item in entry["items"]:
                if want <= 0:
                    break
                guid = online_guid_from_item(item)
                if not guid or guid in taken:
                    continue
                taken.add(guid)
                pages[page].append(guid)
                want -= 1
        entry["cursor"] = len(taken)'''))

# --- 诊断：把「已发布页」的实际顺序与档位写进探针，便于对齐 pool 与 page ---
OLD = '''    by_guid = {online_guid_from_item(item): item for item in entry["items"]}
    return [by_guid[guid] for guid in pages[page] if guid in by_guid and _source_enabled(guid)]'''
assert src.count(OLD) == 1, "_session_page 返回锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    by_guid = {online_guid_from_item(item): item for item in entry["items"]}
    try:
        _probe_write("[pagealloc] kw=%s p=%d n=%d %s" % (
            str(entry.get("keyword") or ""), page, len(pages[page]),
            " ".join("%d%d:%s" % (
                1 if _search_item_has_poster(by_guid[g]) else 0,
                _search_item_quality_rank(by_guid[g]),
                str(by_guid[g].get("title") or "")[:8])
                for g in pages[page][:12] if g in by_guid)))
    except Exception:
        pass
    return [by_guid[guid] for guid in pages[page] if guid in by_guid and _source_enabled(guid)]'''))

# ================================================================ 4) search_track：封面预热 + 排序兜底
OLD = '''    selected = _session_page(entry, page, size)
    merged = merge_online_tracks(upstream_json, selected, page=1, size=size, selected=True)'''
assert src.count(OLD) == 1, "search_track 预热锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    if _search_rank_enabled():
        # v55 兜底：聚合若仍未结束（例如某个音源卡住），就用当下已知信息先排一次，
        # 保证首屏顺序已经是「有海报 + 高音质」优先。重复排序是幂等的；
        # 已发布页同步对齐，避免「排序了但首屏没变」。
        try:
            entry["items"] = rank_search_items(entry["items"])
            _resync_published_pages(entry)
        except Exception:
            pass
    selected = _session_page(entry, page, size)
    merged = merge_online_tracks(upstream_json, selected, page=1, size=size, selected=True)
    # v55: 搜索结果封面预热（与每日推荐同款做法）。缺这一步时 App 首次拉
    # /static/cover/<guid> 要先问 lx 容器（约 4 秒）→ 手机端超时 → 只剩占位图，
    # 表现就是「搜索结果全都没有海报」。
    try:
        _prefetch_online_covers((merged.get("data") or {}).get("list") or [])
    except Exception:
        pass'''))

# ================================================================ 5) build_online_track：封面兜底读 meta
OLD = '''    cover = str(item.get("cover_url") or "")
'''
assert src.count(OLD) == 1, "build_online_track cover 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    cover = str(item.get("cover_url") or "")
    if not cover:
        # v55: 搜索结果已在外层解析过封面并写进 meta_cache —— 这里兜底回填，
        # 让 App 直接拿到 coverUrl，而不是等 /static/cover 现抓（3~4 秒必超时）。
        try:
            cover = str((_meta_get(guid) or {}).get("cover_url") or "")
        except Exception:
            cover = ""
'''))

# ================================================================ 应用
for old_s, new_s in repls:
    assert src.count(old_s) == 1, "替换锚点 count=%d: %r" % (src.count(old_s), old_s[:60])
    src = src.replace(old_s, new_s, 1)

io.open(OUT, "w", encoding="utf-8", newline="").write(src)
print("WROTE", OUT, "bytes=", len(src.encode("utf-8")))

# ================================================================ 自检
checks = {
    # v55 新增
    "def _search_rank_enabled(": 1,
    "def _ext_quality_rank(": 1,
    "def _search_item_quality_rank(": 1,
    "def _search_item_has_poster(": 1,
    "def rank_search_items(": 1,
    "def _netease_quality_from_detail(": 1,
    "async def _netease_detail_bulk(": 1,
    "async def _kw_poster_quality(": 1,
    "async def _enrich_search_items(": 1,
    "def _resync_published_pages(": 1,
    "_resync_published_pages(entry)": 3,
    "entry[\"cursor\"] = len(taken)": 1,
    "taken = entry.get(\"taken\")": 1,
    '"search_rank": os.environ.get(': 1,
    '"search_enrich": os.environ.get(': 1,
    '"search_enrich_limit": int(os.environ.get(': 1,
    '"search_enrich_wait_s": float(os.environ.get(': 1,
    '"search_rank_wait_s": float(os.environ.get(': 1,
    "enrich_task: asyncio.Task | None = None": 1,
    "_enrich_search_items(entry[\"items\"], CONF[\"search_enrich_limit\"])": 2,
    "entry[\"items\"] = rank_search_items(pool)": 1,
    "entry[\"items\"] = rank_search_items(entry[\"items\"])": 2,
    "_probe_write(\"[searchrank] n=%d": 1,
    "_probe_write(\"[poolrank] kw=%s \"": 1,
    "_probe_write(\"[pagealloc] kw=%s p=%d": 1,
    "_probe_write(\"[searchenrich] cap=%d": 2,
    "_probe_write(\"[searchenrich] FAILED %s\" % e)": 1,
    "await asyncio.wait({task}, timeout=float(CONF[\"search_rank_wait_s\"]))": 1,
    "_prefetch_online_covers((merged.get(\"data\") or {}).get(\"list\") or [])": 1,
    "cover = str((_meta_get(guid) or {}).get(\"cover_url\") or \"\")": 1,
    # v54 修复必须保持
    "def _drop_shadow_lyric(": 1,
    "def promote_one_lyric(": 1,
    "async def _lyric_promote_loop(": 1,
    '"lyric_promote": os.environ.get(': 1,
    # v53 / v52 / v50 历史补丁不得被覆盖
    "def sweep_orphan_lyrics(": 1,
    "async def _download_favorite_media(": 1,
    "def delete_materialized_media(": 1,
    "def request_library_scan(": 1,
    "def materialized_library_file(": 1,
    # 搜索主链路保持
    "async def _aggregate_search(": 1,
    "def _session_page(": 1,
    "def _prefetch_online_covers(": 1,
    "def _cover_by_guid_get(": 1,
    "def _cover_guid_path(": 1,
    "def _netease_song_id(": 1,
    "def _kw_title_via_musicinfo(": 1,
    "async def _netease_detail(": 1,
    "_NE_DETAIL_CACHE: dict = {}": 1,
    "def deduplicate_online_items(": 1,
    "def build_online_track(": 1,
}
for needle, want in checks.items():
    got = src.count(needle)
    assert got == want, "%s count=%d want=%d" % (needle, got, want)

# ★ 回归守卫 1：排序必须只动在线块，且必须在 enrich 之后
agg = src.split("async def _aggregate_search(", 1)[1].split("\nasync def ", 1)[0]
i_enrich = agg.index("await _enrich_search_items(entry[\"items\"], CONF[\"search_enrich_limit\"])")
i_rank = agg.index("entry[\"items\"] = rank_search_items(pool)")
i_resync = agg.index("moved = _resync_published_pages(entry)")
assert i_enrich < i_rank < i_resync, "排序必须在补全之后，且重排后必须对齐已发布页"

# ★ 回归守卫 2：首屏必须在页码分配**之前**拿到排序结果（真机踩坑点）
st = src.split("async def search_track(", 1)[1].split("\nasync def ", 1)[0]
i_wait = st.index("timeout=float(CONF[\"search_rank_wait_s\"])")
i_fb = st.index("entry[\"items\"] = rank_search_items(entry[\"items\"])")
i_alloc = st.index("selected = _session_page(entry, page, size)")
assert i_wait < i_alloc and i_fb < i_alloc, "首屏排序必须在 _session_page 分配之前"

# ★ 回归守卫 3：排序维度必须是 (海报, 音质)，且稳定
body = src.split("def rank_search_items(", 1)[1].split("\ndef ", 1)[0]
assert "return (-poster, -quality, idx)" in body, "默认模式排序键错误"
assert "return (-quality, -poster, idx)" in body, "quality_cover 模式排序键错误"
assert "sorted(enumerate(items), key=sort_key)" in body, "排序必须带原始下标（稳定）"

# ★ 回归守卫 4：不得破坏「本地结果永远在前」
assert "target_list.append(build_online_track(it))" in src
assert "rank_search_items(upstream_json" not in src, "不得对含本地曲目的整表排序"

compile(src, OUT, "exec")
print("OK v55 self-check passed (compile clean)")
print("lines:", src.count(chr(10)) + 1)
