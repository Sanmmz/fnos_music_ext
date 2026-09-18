"""增量式补丁构建器（v51 -> v52）：落盘/删除成功后主动触发飞牛扫库。

输入：nas_src/app_v51.py
输出：nas_src/app_v52.py

===== 问题（用户 2026-09-18 反馈）=====
「歌曲下载或者删除后，飞牛音乐没有主动触发扫库」⇒ 落盘的文件在 App 里看不到、
取消收藏删掉的文件在 App 里还留着，必须手动点一次「扫描」。

===== 真机取证结论 =====
1. 扫库接口是 **`POST /music/api/v1/shared-library/scan`**，body `{"guid": <共享库 guid>}`
   （前端索引表 `q.sharedLibrary = {scan: '/shared-library/scan', scanAll: '/shared-library/scan-all'}`；
   调用点 `Kl = async e => { await F.sharedLibrary.scan({guid: e}, ...) }`），
   本机共享库 guid = `<lib-guid>`（读 db `shared_library.guid`）。
2. **该接口需要鉴权**：无凭据时（无论走插件 socket 还是直接打上游 socket）一律
   `{"code":99999,"msg":"INVALID TOKEN"}` / HTTP 401。
   ⇒ 插件不能"自己"调，必须借 App 请求头里的 cookie / authorization /
   `x-trim-music-temp-token` / authx。
3. **扫描是增量的**：上游日志 `metadata correction not required, persist scrape results … fileCount=1`
   —— 只有新增/变化的文件会被抓取元数据，所以每次落盘后调一次并不贵。
4. **飞牛没有内置定时扫描设置**（前端无 autoScan/scheduledScan 之类配置项）
   ⇒ 不主动触发就只能靠人手点。

===== v52 策略：「谁有凭证谁去调」 =====
· 落盘 / 删除成功 ⇒ `request_library_scan()` 挂一个待办（多次请求 3s 内合并）；
· 若触发它的那次请求头还"新"（默认 90s 内）⇒ 后台立刻尝试一次（下载耗时短时体验最好）；
· 无论如何，**下一次带鉴权的 App 请求**都会在中间件里把待办消化掉 —— 这是兜底主力
  （App 打开时会持续轮询 `shared-library/list` 等接口）。
两个通道都写 `[scanreq]` 探针，便于验收。
"""
import io

BASE = r"<workspace>\nas_src\app_v51.py"
OUT = r"<workspace>\nas_src\app_v52.py"

src = io.open(BASE, "r", encoding="utf-8").read()
repls = []

# ================================================================ 1) CONF 新增
OLD = '''    "fav_dl_max_bytes": int(os.environ.get("FNMUSIC_FAV_DL_MAX_BYTES", str(300 * 1024 * 1024))),
    "llm_base_url": (os.environ.get("FNMUSIC_LLM_BASE_URL") or "").strip().rstrip("/"),'''
assert src.count(OLD) == 1, "CONF v50 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    "fav_dl_max_bytes": int(os.environ.get("FNMUSIC_FAV_DL_MAX_BYTES", str(300 * 1024 * 1024))),
    # v52 落盘/删除后主动触发飞牛扫库
    "auto_scan": os.environ.get("FNMUSIC_AUTO_SCAN", "true").lower() in ("true", "1", "yes"),
    "auto_scan_delay_s": float(os.environ.get("FNMUSIC_AUTO_SCAN_DELAY_S", "3")),
    "auto_scan_auth_ttl_s": float(os.environ.get("FNMUSIC_AUTO_SCAN_AUTH_TTL_S", "90")),
    "auto_scan_scan_all": os.environ.get("FNMUSIC_AUTO_SCAN_SCAN_ALL", "false").lower() in ("true", "1", "yes"),
    "llm_base_url": (os.environ.get("FNMUSIC_LLM_BASE_URL") or "").strip().rstrip("/"),'''))

# ================================================================ 2) _LIB_DIR_CACHE 带上 guid
OLD = '''_LIB_DIR_CACHE: dict = {"exp": 0.0, "val": ""}'''
assert src.count(OLD) == 1, "_LIB_DIR_CACHE count=%d" % src.count(OLD)
repls.append((OLD, '''_LIB_DIR_CACHE: dict = {"exp": 0.0, "val": "", "guid": ""}'''))

OLD = '''            try:
                rows = con.execute("SELECT path FROM shared_library ORDER BY id").fetchall()
            finally:
                con.close()
            for (path,) in rows:
                if path and os.path.isdir(path):
                    cached["val"] = path
                    cached["exp"] = now + 30.0
                    return path'''
assert src.count(OLD) == 1, "detect_library_dir 查询 count=%d" % src.count(OLD)
repls.append((OLD, '''            try:
                rows = con.execute("SELECT guid, path FROM shared_library ORDER BY id").fetchall()
            finally:
                con.close()
            for guid, path in rows:
                if path and os.path.isdir(path):
                    cached["val"] = path
                    cached["guid"] = str(guid or "")
                    cached["exp"] = now + 30.0
                    return path'''))

OLD = '''    cached["val"] = CONF["cache_dir"]
    cached["exp"] = now + 30.0
    return CONF["cache_dir"]


_TEE_SAVE_DIR_WARNED = False'''
assert src.count(OLD) == 1, "detect_library_dir 回退 count=%d" % src.count(OLD)
repls.append((OLD, '''    cached["val"] = CONF["cache_dir"]
    cached["guid"] = ""
    cached["exp"] = now + 30.0
    return CONF["cache_dir"]


def library_guid() -> str:
    """飞牛共享库 guid —— 扫库接口 `shared-library/scan` 要用它。

    顺带复用 detect_library_dir() 的 30s 缓存（同一个 SQLite 查询顺带把 guid 读出来）。
    读不到就返回空串，调用方退化为 `shared-library/scan-all`。
    """
    try:
        detect_library_dir()
        return str(_LIB_DIR_CACHE.get("guid") or "")
    except Exception:
        return ""


_TEE_SAVE_DIR_WARNED = False'''))

# ================================================================ 3) _spawn_bg 暴露 task
OLD = '''def _spawn_bg(coro) -> None:
    try:
        task = asyncio.create_task(coro)
    except Exception:
        return
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)'''
assert src.count(OLD) == 1, "_spawn_bg count=%d" % src.count(OLD)
repls.append((OLD, '''def _spawn_bg(coro) -> None:
    _spawn_bg_task(coro)


def _spawn_bg_task(coro):
    """同上，但把 task 返回来（需要观察/去重的调用方用）。"""
    try:
        task = asyncio.create_task(coro)
    except Exception:
        return None
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task'''))

# ================================================================ 4) 中间件：待办扫库兜底
OLD = '''    """诊断用：把每次请求的方法/路径/状态码/Content-Type 落到 access_probe.log。"""
    t0 = time.time()
    skip = request.url.path.startswith("/_ext/")
    status = 599
    ct = "-"
    blen = -1
    try:
        response = await call_next(request)'''
assert src.count(OLD) == 1, "middleware count=%d" % src.count(OLD)
repls.append((OLD, '''    """诊断用：把每次请求的方法/路径/状态码/Content-Type 落到 access_probe.log。

    v52: 顺手做「待办扫库」的兜底触发 —— App 的每个请求都带着有效鉴权，
    而扫库接口恰恰必须要鉴权，所以这里是最合适的落点。
    """
    t0 = time.time()
    skip = request.url.path.startswith("/_ext/")
    status = 599
    ct = "-"
    blen = -1
    if not skip:
        try:
            remember_auth_headers(request)
            if _PENDING_SCAN["count"]:
                _spawn_bg_task(consume_pending_scan(_ReqShim(request)))
        except Exception:
            pass
    try:
        response = await call_next(request)'''))

# ================================================================ 5) v52 模块本体
NEW_SECTION = '''
# === v52 落盘/删除后主动触发飞牛扫库 ===
#
# 真机取证（2026-09-18）：
#   · 扫库 = POST /music/api/v1/shared-library/scan，body {"guid": <共享库 guid>}
#     （scan-all 为无参全量）；guid 读 db shared_library.guid。
#   · **必须鉴权**：无凭据一律 HTTP 401 / {"code":99999,"msg":"INVALID TOKEN"}
#     ⇒ 插件不能自己调，只能借 App 请求头（cookie / authorization /
#       x-trim-music-temp-token / authx）。
#   · **扫描是增量的**（上游日志 fileCount=1，只抓新增/变化文件），落盘后调一次不贵。
#   · 飞牛**没有内置定时扫描设置**，不主动触发就只能手动点「扫描」。
#
# 因此策略是「谁有凭证谁去调」：落盘/删除成功 → 挂待办；
# 触发它的那次请求头若还新（默认 90s）就立刻试一次；
# 否则交给**下一次带鉴权的 App 请求**（中间件）兜底消化。
_PENDING_SCAN: dict = {"count": 0, "last": 0.0}
_LAST_AUTH: dict = {"ts": 0.0, "headers": None}
_AUTH_HEADER_KEYS = ("cookie", "authorization", "x-trim-music-temp-token", "authx")


def _has_app_auth(headers) -> bool:
    try:
        return any(str(headers.get(k) or "").strip() for k in _AUTH_HEADER_KEYS)
    except Exception:
        return False


def remember_auth_headers(request: Any) -> None:
    """只留最近一次 App 请求的鉴权头（存内存 + 时效），供落盘完成后立刻扫库。

    注意：绝不打印这些值，探针只写调用结果。
    """
    try:
        if not _has_app_auth(request.headers):
            return
        _LAST_AUTH["ts"] = time.monotonic()
        _LAST_AUTH["headers"] = copy_incoming_headers(request)
    except Exception:
        pass


def _recent_auth_headers():
    headers = _LAST_AUTH.get("headers")
    ts = float(_LAST_AUTH.get("ts") or 0.0)
    if not headers or not ts:
        return None
    if time.monotonic() - ts > float(CONF.get("auto_scan_auth_ttl_s") or 90.0):
        return None
    return dict(headers)


async def _call_library_scan(headers: dict) -> bool:
    """真正调上游扫库（增量）。成功返回 True。"""
    guid = "" if CONF.get("auto_scan_scan_all") else library_guid()
    path = "/music/api/v1/shared-library/scan" if guid else "/music/api/v1/shared-library/scan-all"
    try:
        client = get_upstream_client(app)
        if guid:
            r = await client.post(path, json={"guid": guid}, headers=headers, timeout=10.0)
        else:
            r = await client.post(path, headers=headers, timeout=10.0)
        body = (r.text or "")[:200].replace("\\n", " ")
        try:
            ok = r.status_code == 200 and int((r.json() or {}).get("code", -1)) == 0
        except Exception:
            ok = False
        _probe_write("[scanreq] call path=%s guid=%s status=%s ok=%s body=%s" % (
            path, guid or "-", r.status_code, ok, body))
        return ok
    except Exception as e:
        _probe_write("[scanreq] fail path=%s err=%s: %s" % (path, type(e).__name__, str(e)[:160]))
        return False


async def _scan_soon() -> None:
    """合并窗口结束后，若手上还有"新"的鉴权头就立刻打一发。"""
    await asyncio.sleep(max(0.0, float(CONF.get("auto_scan_delay_s") or 0.0)))
    if not _PENDING_SCAN["count"]:
        return
    headers = _recent_auth_headers()
    if headers is None:
        return                      # 交给下一次带鉴权的 App 请求兜底
    if await _call_library_scan(headers):
        _PENDING_SCAN["count"] = 0


def request_library_scan(reason: str = "") -> None:
    """排队一次增量扫库（多次请求会被 3s 窗口合并成一次）。"""
    if not CONF.get("auto_scan"):
        return
    try:
        _PENDING_SCAN["count"] += 1
        _PENDING_SCAN["last"] = time.monotonic()
        _probe_write("[scanreq] queue reason=%s pending=%d" % (reason, _PENDING_SCAN["count"]))
        _spawn_bg_task(_scan_soon())
    except Exception:
        pass


async def consume_pending_scan(request: Any) -> bool:
    """中间件钩子：手上这次请求带鉴权，就用它把待办扫库打出去。"""
    if not CONF.get("auto_scan") or not _PENDING_SCAN["count"]:
        return False
    if not _has_app_auth(request.headers):
        return False
    try:
        headers = copy_incoming_headers(request)
    except Exception:
        return False
    _PENDING_SCAN["count"] = 0
    return await _call_library_scan(headers)


'''

OLD = "async def _probe_upstream_auth(request: Request, client: httpx.AsyncClient) -> tuple[bool, str, Response | None]:"
assert src.count(OLD) == 1, "_probe_upstream_auth 锚点 count=%d" % src.count(OLD)
repls.append((OLD, NEW_SECTION.lstrip("\n") + OLD))

# ================================================================ 6) 触发点①：tee 落曲库成功
OLD = '''                elif _library_ok:
                    dest = library_media_path(guid, title, ext, artist=artist, directory=tee_save_dir())
                    os.replace(part, dest)
                    saved = True
                    remember_media_path(guid, dest)
                    adopt_library_perms(dest)
                    write_audio_tags(dest, title, artist, album)'''
assert src.count(OLD) == 1, "tee 曲库分支 count=%d" % src.count(OLD)
repls.append((OLD, '''                elif _library_ok:
                    dest = library_media_path(guid, title, ext, artist=artist, directory=tee_save_dir())
                    os.replace(part, dest)
                    saved = True
                    remember_media_path(guid, dest)
                    adopt_library_perms(dest)
                    write_audio_tags(dest, title, artist, album)
                    request_library_scan("tee")'''))

# ================================================================ 7) 触发点②：收藏整轨落盘成功
OLD = '''            _probe_write("[favdl] ok guid=%s dest=%s bytes=%d exp=%s ext=%s lyric=%s" % (
                guid, dest, written, expected, ext, bool(lyric)))
            return True'''
assert src.count(OLD) == 1, "favdl ok 探针 count=%d" % src.count(OLD)
repls.append((OLD, '''            _probe_write("[favdl] ok guid=%s dest=%s bytes=%d exp=%s ext=%s lyric=%s" % (
                guid, dest, written, expected, ext, bool(lyric)))
            request_library_scan("favdl")
            return True'''))

# ================================================================ 8) 触发点③：取消收藏删掉文件
OLD = '''    if CONF.get("fav_dl_delete_on_unfav") and not is_favorite_online_guid(guid):
        try:
            await asyncio.to_thread(delete_materialized_media, guid)
        except Exception as e:
            logger.warning("Failed to delete materialized media for %s: %s", guid, e)'''
assert src.count(OLD) == 1, "取消收藏删除 count=%d" % src.count(OLD)
repls.append((OLD, '''    if CONF.get("fav_dl_delete_on_unfav") and not is_favorite_online_guid(guid):
        removed = []
        try:
            removed = await asyncio.to_thread(delete_materialized_media, guid)
        except Exception as e:
            logger.warning("Failed to delete materialized media for %s: %s", guid, e)
        if removed:
            request_library_scan("unfav")'''))

for old_s, new_s in repls:
    src = src.replace(old_s, new_s, 1)

io.open(OUT, "w", encoding="utf-8", newline="").write(src)
print("WROTE", OUT, "bytes=", len(src.encode("utf-8")))

checks = {
    "def request_library_scan(": 1,
    "async def _call_library_scan(": 1,
    "async def consume_pending_scan(": 1,
    "def remember_auth_headers(": 1,
    "def _recent_auth_headers(": 1,
    "async def _scan_soon(": 1,
    "def library_guid() -> str:": 1,
    "def _spawn_bg_task(coro):": 1,
    "def _has_app_auth(headers) -> bool:": 1,
    "_probe_write(\"[scanreq]": 3,
    'request_library_scan("tee")': 1,
    'request_library_scan("favdl")': 1,
    'request_library_scan("unfav")': 1,
    "if _PENDING_SCAN[\"count\"]:": 1,
    '"/music/api/v1/shared-library/scan"': 1,
    '"/music/api/v1/shared-library/scan-all"': 1,
    'SELECT guid, path FROM shared_library': 1,
    # 历史补丁不得被覆盖
    "async def _download_favorite_media(": 1,
    "def delete_materialized_media(": 1,
    "def _same_dir(path: str, directory: str) -> bool:": 1,
    "_probe_write(\"[favdl]": 7,
    "_probe_write(\"[favdel]": 1,
    "_probe_write(\"[tee] judge": 1,
    "_probe_write(\"[hist-del]": 4,
    "https://music.163.com/api/song/enhance/player/url": 1,
    "for key in matched_keys:": 1,
}
for needle, want in checks.items():
    got = src.count(needle)
    assert got == want, "%s count=%d want=%d" % (needle, got, want)

compile(src, OUT, "exec")
print("OK v52 self-check passed (compile clean)")
print("lines:", src.count(chr(10)) + 1)
