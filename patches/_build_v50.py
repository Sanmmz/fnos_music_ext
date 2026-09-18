"""增量式补丁构建器（v49 -> v50）：只对「收藏的在线曲目」落盘。

输入：nas_src/app_v49.py
输出：nas_src/app_v50.py

===== 已定稿的根因（v49 实测）=====
`stream_tee_response()` 的落盘代码位于生成器尾部，而真实播放器按 1MB 定长窗口取流
（`bytes=0-1048575` → `1048576-2097151` → …）。两重门同时挡住落盘：
  · 门1 `should_cache(range_header)` 只认「无 Range」或 `bytes=0-`（开区间）；
  · 门2 即便开了整段拉取，尾部 `os.replace` 也可能因客户端提前断开而被取消。
⇒ 现有"边听边存"在真机上几乎不产生文件，且不产生文件时**仍会写 .lrc**（55 个孤儿歌词）。

===== v50 设计（用户 2026-09-18 决策）=====
触发：收藏即下载 + 播放补漏；清理：取消收藏即删对应文件。
  1. 收藏集合：读 `online_favorites/*.json` 的**并集**（纯本地读盘 + mtime 短缓存），
     热路径绝不打上游 `user/me`（authx 是逐请求签名头，后台任务复用会失效）。
  2. `favorite_track_create`（在线 guid）→ 后台整轨下载：`_open_online_stream(guid, None)`
     （无 Range = 整轨）+ 信号量限流 + inflight 去重 → `.part` → 校验字节数 → `os.replace`。
  3. `stream_track` 在「播放起点」（无 Range / `bytes=0-*`）且是收藏时触发同一下载 = 播放补漏。
  4. `favorite_track_delete` → 删 `.ref` 映射 + 曲库/缓存内该 guid 的音频与 `.lrc`
     （仅限 media dirs 内的绝对路径；且并集里已无人收藏才删）。
  5. tee 兜底：`FNMUSIC_TEE_FAVORITES_ONLY=true` 时，非收藏曲目只进本地滚动缓存，
     绝不写入曲库（曲库是 rclone 云盘挂载，写进去等于上传云盘）。
  6. 命名：不再出现 `unknown.mp3` —— 标题解析不出来就**放弃本次落盘**。
  7. `.lrc` 只在音频落盘成功后写，杜绝新的孤儿歌词。
  8. `detect_library_dir()` 加 30s 缓存（此前每次 /stream 都开一次 SQLite）。
"""
import io

BASE = r"<workspace>\nas_src\app_v49.py"
OUT = r"<workspace>\nas_src\app_v50.py"

src = io.open(BASE, "r", encoding="utf-8").read()
repls = []

# ================================================================ 1) CONF 新增
OLD = '''    "fav_dir": os.environ.get(
        "FNMUSIC_FAV_DIR", os.path.join(_HOME, "online_favorites")
    ),
    "llm_base_url": (os.environ.get("FNMUSIC_LLM_BASE_URL") or "").strip().rstrip("/"),'''
assert src.count(OLD) == 1, "CONF fav_dir 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    "fav_dir": os.environ.get(
        "FNMUSIC_FAV_DIR", os.path.join(_HOME, "online_favorites")
    ),
    # v50 仅收藏落盘
    "tee_favorites_only": os.environ.get("FNMUSIC_TEE_FAVORITES_ONLY", "false").lower() in ("true", "1", "yes"),
    "fav_dl_on_favorite": os.environ.get("FNMUSIC_FAV_DL_ON_FAVORITE", "true").lower() in ("true", "1", "yes"),
    "fav_dl_on_play": os.environ.get("FNMUSIC_FAV_DL_ON_PLAY", "true").lower() in ("true", "1", "yes"),
    "fav_dl_delete_on_unfav": os.environ.get("FNMUSIC_FAV_DELETE_ON_UNFAV", "true").lower() in ("true", "1", "yes"),
    "fav_dl_concurrency": int(os.environ.get("FNMUSIC_FAV_DL_CONCURRENCY", "2")),
    "fav_dl_timeout_s": float(os.environ.get("FNMUSIC_FAV_DL_TIMEOUT_S", "240")),
    "fav_dl_max_bytes": int(os.environ.get("FNMUSIC_FAV_DL_MAX_BYTES", str(300 * 1024 * 1024))),
    "llm_base_url": (os.environ.get("FNMUSIC_LLM_BASE_URL") or "").strip().rstrip("/"),'''))

# ================================================================ 2) detect_library_dir 加 30s 缓存
OLD = '''def detect_library_dir() -> str:
    """优先环境变量，否则读飞牛 music.db 的共享库路径，最后回退到仓库 cache/。"""
    explicit = str(CONF.get("library_dir") or "").strip()
    if explicit:
        return explicit
    db = str(CONF.get("music_db") or "")
    if db and os.path.exists(db):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute("SELECT path FROM shared_library ORDER BY id").fetchall()
            finally:
                con.close()
            for (path,) in rows:
                if path and os.path.isdir(path):
                    return path
        except Exception as e:
            logger.warning("Failed to read shared_library path: %s", e)
    return CONF["cache_dir"]'''
assert src.count(OLD) == 1, "detect_library_dir count=%d" % src.count(OLD)
repls.append((OLD, '''_LIB_DIR_CACHE: dict = {"exp": 0.0, "val": ""}


def detect_library_dir() -> str:
    """优先环境变量，否则读飞牛 music.db 的共享库路径，最后回退到仓库 cache/。

    v50: 带 30s 缓存 —— 此前每个 /stream 请求都会开一次 SQLite 并对
    rclone 云盘挂载点做 stat，是播放首字节延迟里很可观的一块开销。
    """
    explicit = str(CONF.get("library_dir") or "").strip()
    if explicit:
        return explicit
    now = time.monotonic()
    cached = _LIB_DIR_CACHE
    if cached["val"] and cached["exp"] > now:
        return cached["val"]
    db = str(CONF.get("music_db") or "")
    if db and os.path.exists(db):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute("SELECT path FROM shared_library ORDER BY id").fetchall()
            finally:
                con.close()
            for (path,) in rows:
                if path and os.path.isdir(path):
                    cached["val"] = path
                    cached["exp"] = now + 30.0
                    return path
        except Exception as e:
            logger.warning("Failed to read shared_library path: %s", e)
    cached["val"] = CONF["cache_dir"]
    cached["exp"] = now + 30.0
    return CONF["cache_dir"]'''))

# ================================================================ 3) 新模块：仅收藏落盘
NEW_SECTION = '''
# === v50 仅收藏落盘：收藏集合 / 收藏即下载 / 播放补漏 / 取消收藏即删 ===

_ALL_FAV_CACHE: dict = {"exp": 0.0, "sig": None, "guids": frozenset()}
_FAV_DL_SEM = None
_FAV_DL_INFLIGHT: set = set()


def invalidate_favorites_cache() -> None:
    """收藏列表变动后立即失效（下次调用重新读盘）。"""
    _ALL_FAV_CACHE["exp"] = 0.0


def all_online_favorite_guids() -> frozenset:
    """所有 online_favorites/*.json 里在线曲目 guid 的并集。

    热路径（每次 /stream 都可能被调）绝不能打上游 user/me：一是延迟，二是
    authx 是逐请求签名头，后台任务复用会失效。因此只读本地 JSON，
    用 (文件名, mtime_ns, size) 做 3s 短缓存。单用户场景与"当前用户收藏"等价。
    """
    fav_dir = CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites")
    try:
        names = sorted(n for n in os.listdir(fav_dir) if n.endswith(".json"))
    except Exception:
        names = []
    sig = []
    for n in names:
        try:
            st = os.stat(os.path.join(fav_dir, n))
            sig.append((n, st.st_mtime_ns, st.st_size))
        except Exception:
            pass
    now = time.monotonic()
    if _ALL_FAV_CACHE["exp"] > now and _ALL_FAV_CACHE["sig"] == tuple(sig):
        return _ALL_FAV_CACHE["guids"]
    guids = set()
    for n, _m, _s in sig:
        try:
            with open(os.path.join(fav_dir, n), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        for it in items:
            g = str(it.get("guid") or "").strip() if isinstance(it, dict) else ""
            if is_online_guid(g):
                guids.add(g)
    frozen = frozenset(guids)
    _ALL_FAV_CACHE["exp"] = now + 3.0
    _ALL_FAV_CACHE["sig"] = tuple(sig)
    _ALL_FAV_CACHE["guids"] = frozen
    return frozen


def is_favorite_online_guid(guid: str) -> bool:
    if not is_online_guid(guid):
        return False
    try:
        return guid in all_online_favorite_guids()
    except Exception:
        return False


def range_starts_at_zero(range_header: str | None) -> bool:
    """播放起点判定：无 Range 或 bytes=0-*（含手机端 1MB 窗口的第一段）。"""
    if not range_header:
        return True
    return bool(re.match(r"^\\s*bytes\\s*=\\s*0\\s*-", range_header.strip(), re.I))


def _fav_dl_sem() -> asyncio.Semaphore:
    global _FAV_DL_SEM
    if _FAV_DL_SEM is None:
        _FAV_DL_SEM = asyncio.Semaphore(max(1, int(CONF.get("fav_dl_concurrency") or 2)))
    return _FAV_DL_SEM


class _ReqShim:
    """冻结后台任务需要的请求字段（响应结束后活 Request 不可靠）。"""

    __slots__ = ("app", "headers", "url", "query_params", "method", "scope")

    def __init__(self, request: Any):
        self.app = request.app
        self.headers = request.headers
        self.url = request.url
        self.query_params = request.query_params
        self.method = "GET"
        self.scope = getattr(request, "scope", {})


def materialized_library_file(guid: str) -> str | None:
    """本插件已把该 guid 落到曲库的音频（只认我们写下的 .ref 映射，排除滚动缓存）。"""
    stem = recalled_media_stem(guid)
    if not stem or _is_rolling_cache_stem(stem, guid) or not os.path.isabs(stem):
        return None
    for ext in CACHE_EXTS:
        p = f"{stem}.{ext}"
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        except Exception:
            continue
    return None


async def _download_favorite_media(request: Any, guid: str) -> bool:
    """整轨下载一首「收藏的在线曲目」到曲库；标题解析不出就放弃，绝不写 unknown。"""
    if not is_online_guid(guid) or guid in _FAV_DL_INFLIGHT:
        return False
    _FAV_DL_INFLIGHT.add(guid)
    part = None
    resp = None
    owned = None
    try:
        async with _fav_dl_sem():
            if materialized_library_file(guid):
                _probe_write("[favdl] skip guid=%s already-in-library" % guid)
                return True
            info = await _online_info(request, guid)
            title = str((info or {}).get("title") or "").strip()
            artist = str((info or {}).get("artist") or "").strip()
            if not title:
                try:
                    rm = await _resolve_record_meta(request, guid)
                except Exception:
                    rm = None
                if isinstance(rm, dict):
                    info = dict(info or {})
                    for k, v in rm.items():
                        if v and not info.get(k):
                            info[k] = v
                    title = str(info.get("title") or "").strip()
                    artist = str(info.get("artist") or "").strip()
            if not title:
                _probe_write("[favdl] abort guid=%s reason=no-title" % guid)
                return False
            invalidate_favorites_cache()
            if not is_favorite_online_guid(guid):
                _probe_write("[favdl] abort guid=%s reason=unfavorited" % guid)
                return False
            opened = await _open_online_stream(request, guid, None)
            if not opened:
                _probe_write("[favdl] abort guid=%s reason=open-failed" % guid)
                return False
            resp, owned, ext, info2, chunks, first = opened
            if isinstance(info2, dict) and info2:
                title = str(info2.get("title") or title).strip() or title
                artist = str(info2.get("artist") or artist).strip()
                info = {**(info or {}), **info2}
            length = resp.headers.get("content-length", "")
            expected = int(length) if length.isdigit() else None
            ext = str(ext or (info or {}).get("ext")
                      or ext_from_content_type(resp.headers.get("content-type", "")) or "mp3")
            ext = ext.lstrip(".").lower() or "mp3"
            directory = tee_save_dir()
            os.makedirs(directory, exist_ok=True)
            part = os.path.join(directory, f"{cache_safe_guid(guid)}.{uuid4().hex}.part")
            written = 0
            max_bytes = int(CONF.get("fav_dl_max_bytes") or 0)
            deadline = asyncio.get_running_loop().time() + float(CONF.get("fav_dl_timeout_s") or 240.0)
            with open(part, "wb") as fp:
                if first:
                    fp.write(first)
                    written += len(first)
                async for chunk in chunks:
                    if not chunk:
                        continue
                    fp.write(chunk)
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise ValueError("oversize:%d" % written)
                    if asyncio.get_running_loop().time() > deadline:
                        raise TimeoutError("dl-timeout")
            if written < 1024 or (expected is not None and written != expected):
                _probe_write("[favdl] abort guid=%s reason=short written=%d exp=%s" % (guid, written, expected))
                return False
            dest = library_media_path(guid, title, ext, artist=artist, directory=directory)
            os.replace(part, dest)
            part = None
            remember_media_path(guid, dest)
            adopt_library_perms(dest)
            write_audio_tags(dest, title, artist, str((info or {}).get("album") or ""))
            # 音频落盘成功后再写歌词：此前"有词无曲"的孤儿 .lrc 就是这里漏掉的判断
            lyric = str((info or {}).get("lyric") or (info or {}).get("lrc") or "").strip()
            if lyric:
                write_lyric_cache(guid, lyric, title, artist)
            _probe_write("[favdl] ok guid=%s dest=%s bytes=%d exp=%s ext=%s lyric=%s" % (
                guid, dest, written, expected, ext, bool(lyric)))
            return True
    except Exception as e:
        _probe_write("[favdl] fail guid=%s err=%s: %s" % (guid, type(e).__name__, str(e)[:200]))
        return False
    finally:
        _FAV_DL_INFLIGHT.discard(guid)
        if part and os.path.exists(part):
            try:
                os.remove(part)
            except Exception:
                pass
        for closer in (resp, owned):
            if closer is not None:
                try:
                    await closer.aclose()
                except Exception:
                    pass


def spawn_favorite_download(request: Any, guid: str) -> None:
    """后台整轨落盘，不阻塞 App 响应；重复触发由 _FAV_DL_INFLIGHT 去重。"""
    if not is_online_guid(guid) or guid in _FAV_DL_INFLIGHT:
        return
    try:
        shim = _ReqShim(request)
    except Exception:
        return
    _spawn_bg(_download_favorite_media(shim, guid))


def _safe_unlink_in_media_dirs(path: str) -> bool:
    """只允许删曲库目录或滚动缓存目录内的文件。"""
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    roots = []
    for d in iter_media_dirs():
        try:
            roots.append(os.path.realpath(d).rstrip("/"))
        except Exception:
            pass
    if not any(r and (rp == r or rp.startswith(r + "/")) for r in roots):
        return False
    try:
        os.remove(rp)
        return True
    except Exception:
        return False


def delete_materialized_media(guid: str) -> list:
    """取消收藏：删除本插件为该 guid 落盘的音频 / 歌词 / .ref 映射。"""
    removed = []
    stems = set()
    stem = recalled_media_stem(guid)
    if stem:
        stems.add(stem)
    cached = find_cache_file(guid)
    if cached:
        stems.add(os.path.splitext(cached)[0])
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        try:
            if os.path.isdir(d):
                stems.add(os.path.join(d, safe))
        except Exception:
            pass
    for s in sorted(x for x in stems if x):
        for ext in list(CACHE_EXTS) + ["lrc"]:
            p = f"{s}.{ext}"
            try:
                if os.path.isfile(p) and _safe_unlink_in_media_dirs(p):
                    removed.append(p)
            except Exception:
                continue
    ref = media_ref_path(guid)
    if os.path.isfile(ref):
        try:
            os.remove(ref)
            removed.append(ref)
        except Exception:
            pass
    _probe_write("[favdel] guid=%s removed=%d %s" % (guid, len(removed), "|".join(removed) or "-"))
    return removed


'''

OLD = "async def _probe_upstream_auth(request: Request, client: httpx.AsyncClient) -> tuple[bool, str, Response | None]:"
assert src.count(OLD) == 1, "_probe_upstream_auth 锚点 count=%d" % src.count(OLD)
repls.append((OLD, NEW_SECTION.lstrip("\n") + OLD))

# ================================================================ 4) stream_tee_response 签名
OLD = """    chunks: Any = None,
    first_chunk: bytes = b"",
) -> Response:"""
assert src.count(OLD) == 1, "stream_tee_response 签名 count=%d" % src.count(OLD)
repls.append((OLD, """    chunks: Any = None,
    first_chunk: bytes = b"",
    favorites_only: bool = False,
) -> Response:"""))

# ================================================================ 5) tee 落盘门槛：仅收藏写曲库
OLD = """            tee_enabled = bool(CONF.get("tee_save_enabled"))
            _cache_ok = should_cache(range_header)
            if _cache_ok and full_resource:
                directory = tee_save_dir() if tee_enabled else CONF["cache_dir"]"""
assert src.count(OLD) == 1, "tee 落盘入口 count=%d" % src.count(OLD)
repls.append((OLD, """            tee_enabled = bool(CONF.get("tee_save_enabled"))
            # v50: 「写曲库」与「写滚动缓存」拆开判定。
            # favorites_only 时非收藏只进本地 cache/，不碰 rclone 云盘挂载的曲库。
            _library_ok = tee_enabled and not (favorites_only and not is_favorite_online_guid(guid))
            _cache_ok = should_cache(range_header)
            if _cache_ok and full_resource:
                directory = tee_save_dir() if _library_ok else CONF["cache_dir"]"""))

OLD = '''                _probe_write("[tee] start guid=%s range=%s status=%s full=%s tee=%s exp=%s dir=%s" % (
                    guid, range_header, resp.status_code, full_resource, tee_enabled, expected, directory))'''
assert src.count(OLD) == 1, "tee start 探针 count=%d" % src.count(OLD)
repls.append((OLD, '''                _probe_write("[tee] start guid=%s range=%s status=%s full=%s tee=%s lib=%s fav=%s exp=%s dir=%s" % (
                    guid, range_header, resp.status_code, full_resource, tee_enabled, _library_ok,
                    is_favorite_online_guid(guid), expected, directory))'''))

OLD = """            if _ok:
                title, artist, album = (str((info or {}).get(k) or "") for k in ("title", "artist", "album"))
                if tee_enabled:
                    dest = library_media_path(guid, title, ext, artist=artist, directory=tee_save_dir())"""
assert src.count(OLD) == 1, "tee 判定块 count=%d" % src.count(OLD)
repls.append((OLD, """            if _ok:
                title, artist, album = (str((info or {}).get(k) or "") for k in ("title", "artist", "album"))
                # v50: 曲库文件名必须是「歌手 - 歌名」；标题解析不出就退到滚动缓存，
                # 绝不再落下 unknown.mp3（shutil.move 兼容曲库与 cache 跨文件系统）
                if _library_ok and not title.strip() and not artist.strip():
                    dest = os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.{ext}")
                    os.makedirs(CONF["cache_dir"], exist_ok=True)
                    shutil.move(part, dest)
                    saved = True
                    _probe_write("[tee] demote guid=%s dest=%s reason=no-title" % (guid, dest))
                elif _library_ok:
                    dest = library_media_path(guid, title, ext, artist=artist, directory=tee_save_dir())"""))

OLD = """                else:
                    # 边听边存关闭：只写滚动缓存（cache_safe_guid 命名，find_cache_file 精确名可命中）"""
assert src.count(OLD) == 1, "tee 滚动分支注释 count=%d" % src.count(OLD)
repls.append((OLD, """                else:
                    # 非收藏（或关闭边听边存）：只写本地滚动缓存（cache_safe_guid 命名，精确名可命中）"""))

OLD = """                lyric = str((info or {}).get("lyric") or (info or {}).get("lrc") or "")
                if lyric.strip():
                    write_lyric_cache(guid, lyric, title, artist)
                if not tee_enabled:"""
assert src.count(OLD) == 1, "tee 歌词块 count=%d" % src.count(OLD)
repls.append((OLD, """                lyric = str((info or {}).get("lyric") or (info or {}).get("lrc") or "")
                # v50: 音频没落地就不写 .lrc —— 55 个孤儿歌词全部来自这里
                if saved and lyric.strip():
                    write_lyric_cache(guid, lyric, title, artist)
                if not _library_ok:"""))

# ================================================================ 6) stream_track：播放补漏 + tee 传参
OLD = """    item, entry = _retained_track(request, guid)
    candidates = [guid]"""
assert src.count(OLD) == 1, "stream_track 锚点 count=%d" % src.count(OLD)
repls.append((OLD, """    # v50 播放补漏：收藏曲目在「播放起点」触发一次后台整轨落盘
    # （真实播放器按 1MB 定长窗口取流，tee 尾部落盘永远轮不到）
    if CONF.get("fav_dl_on_play") and range_starts_at_zero(range_header) and is_favorite_online_guid(guid):
        spawn_favorite_download(request, guid)
    item, entry = _retained_track(request, guid)
    candidates = [guid]"""))

OLD = """                return stream_tee_response(resp, candidate, range_header,
                    coro_factory=lambda: _online_info(request, candidate), client_to_close=owned,
                    resolved_ext=ext, pre_info=info, chunks=chunks, first_chunk=first)"""
assert src.count(OLD) == 1, "stream_tee_response 调用点 count=%d" % src.count(OLD)
repls.append((OLD, """                return stream_tee_response(resp, candidate, range_header,
                    coro_factory=lambda: _online_info(request, candidate), client_to_close=owned,
                    resolved_ext=ext, pre_info=info, chunks=chunks, first_chunk=first,
                    favorites_only=bool(CONF.get("tee_favorites_only")))"""))

# ================================================================ 7) 收藏即下载
OLD = """            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error updating online favorites for user %s: %s", user_guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})"""
assert src.count(OLD) == 1, "favorite create 锚点 count=%d" % src.count(OLD)
repls.append((OLD, """            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error updating online favorites for user %s: %s", user_guid, e)

    # v50 收藏即下载：后台整轨落盘，立刻返回不拖慢 App
    invalidate_favorites_cache()
    if CONF.get("fav_dl_on_favorite"):
        spawn_favorite_download(request, guid)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})"""))

# ================================================================ 8) 取消收藏即删文件
OLD = """            items = [it for it in items if it.get("guid") != guid]
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error deleting from online favorites for user %s: %s", user_guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})"""
assert src.count(OLD) == 1, "favorite delete 锚点 count=%d" % src.count(OLD)
repls.append((OLD, """            items = [it for it in items if it.get("guid") != guid]
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error deleting from online favorites for user %s: %s", user_guid, e)

    # v50 取消收藏即删对应文件；并集里还有人收藏则保留
    invalidate_favorites_cache()
    if CONF.get("fav_dl_delete_on_unfav") and not is_favorite_online_guid(guid):
        try:
            await asyncio.to_thread(delete_materialized_media, guid)
        except Exception as e:
            logger.warning("Failed to delete materialized media for %s: %s", guid, e)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})"""))

for old_s, new_s in repls:
    src = src.replace(old_s, new_s, 1)

io.open(OUT, "w", encoding="utf-8", newline="").write(src)
print("WROTE", OUT, "bytes=", len(src.encode("utf-8")))

checks = {
    "async def _download_favorite_media(": 1,
    "def spawn_favorite_download(": 1,
    "def all_online_favorite_guids(": 1,
    "def is_favorite_online_guid(": 1,
    "def delete_materialized_media(": 1,
    "def _safe_unlink_in_media_dirs(": 1,
    "def range_starts_at_zero(": 1,
    "class _ReqShim:": 1,
    "_probe_write(\"[favdl]": 7,
    "_probe_write(\"[favdel]": 1,
    "_library_ok": 6,
    "favorites_only=bool(CONF.get(\"tee_favorites_only\"))": 1,
    "if CONF.get(\"fav_dl_on_favorite\"):": 1,
    "if CONF.get(\"fav_dl_on_play\") and range_starts_at_zero": 1,
    "if CONF.get(\"fav_dl_delete_on_unfav\") and not is_favorite_online_guid(guid):": 1,
    # 旧探针必须原样保留
    "_probe_write(\"[tee] nocache": 1,
    "_probe_write(\"[tee] judge": 1,
    "_probe_write(\"[tee] saved": 1,
    "_probe_write(\"[tee] rolling": 1,
    "_probe_write(\"[tee] finally": 1,
    "_probe_write(\"[hist-del]": 4,
    # 历史补丁不得被覆盖
    "https://music.163.com/api/song/enhance/player/url": 1,
    "def _spawn_bg(": 1,
    "first_guid = _daily_poster_guid(user_guid)": 1,
    "for key in matched_keys:": 1,
    # v50 不得再出现无条件写歌词
    "if saved and lyric.strip():": 1,
}
for needle, want in checks.items():
    got = src.count(needle)
    assert got == want, "%s count=%d want=%d" % (needle, got, want)

assert "unknown" not in src.split("def library_basename")[1].split("def ")[0].replace("unknown", "", 1), "sanity"
compile(src, OUT, "exec")
print("OK v50 self-check passed (compile clean)")
print("lines:", src.count(chr(10)) + 1)
