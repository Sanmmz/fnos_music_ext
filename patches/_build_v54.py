"""增量式补丁构建器（v53 -> v54）：修「收藏后音频进了曲库，歌词却没贴身」的回归。

输入：nas_src/app_v53.py
输出：nas_src/app_v54.py

===== 症状（用户 2026-09-18 反馈）=====
「上一次修改后（v53），造成收藏音乐之后歌曲下载到了本地，但是歌词没有存储到本地」
—— 音频整轨下载进了云盘曲库 ✅，歌词却留在插件自己的 cache/ 里，曲库音频旁边没有同名 .lrc ❌

===== 真机取证（access_probe.log）=====
    GET /music/api/v1/lyric/list?trackGUID=online%3Anetease%3A2163210456&lan=zh-CN
        | 200 | 1ms | len=2377                     ← 1ms = 命中本地缓存（歌词早已在 cache/）
    [favdl] ok guid=online:netease:2163210456
        dest=/vol02/<vol-id>/music/加木 - 两 难.flac ... lyric=True
    [favdel] guid=online:netease:2163210456 removed=4
        /home/<user>/fnmusic_ext/cache/online_netease_2163210456.lrc      ← 歌词在 cache
        /vol02/<vol-id>/music/加木 - 两 难.flac                     ← 音频在曲库
        /home/<user>/fnmusic_ext/cache/online_netease_2163210456.ref
        /home/<user>/fnmusic_ext/cache/online_netease_2163210456.lyricref
`lyric=True` 说明歌词抓到了 —— 是**落点**错了。
对照 `online:netease:1973665667`（收藏前没播放过、cache 里没有歌词副本）：
歌词正确地落在了 `/vol02/.../马也_Crabbit - 海屿你.lrc`。

===== 根因（v53 自己引入的回归）=====
v53 的 `lyric_cache_path()` 优先级是：
    已有歌词 → 曲库音频同名 sidecar → 缓存音频同名 sidecar → 本地 cache/
而 `find_lyric_file()` 的第 4 档会去 `iter_media_dirs()`（= [曲库, cache/]）里找
`<safe-guid>.lrc` ⇒ **命中 cache/ 里的旧歌词副本**。
于是只要「收藏之前播过一次」（App 拉歌词 → 写进 cache/<guid>.lrc），
收藏整轨下载把音频落进曲库后，歌词仍然写回 cache/ —— 永远不贴身。
v53 之前 `find_lyric_file` 不是第一档，走的是 `.ref` 词干（音频在曲库 ⇒ 歌词贴身），所以没这个问题。

===== v54 修法 =====
1) `lyric_cache_path()`：**「音频已在曲库」提到最前**，优先于任何缓存副本 ⇒ 歌词跟着音频走。
   最后一档仍是本地 cache/，v53 的「绝不把无主歌词写进云盘曲库」保持不变。
2) `write_lyric_cache()`：短路条件从「任意位置内容一致」改成「**目标位置**内容一致」，
   否则 cache 副本会永远短路成功、歌词无法被提升。
3) `_drop_shadow_lyric()`：写对位置后清掉 cache/ 里同 guid 的影子副本。
4) `promote_one_lyric()` / `promote_library_lyrics()`：**自愈**——
   把「音频已在曲库、歌词只在 cache/」的存量歌词搬成曲库同名 sidecar
   （启动后一次 + 周期复扫 + 每次整轨下载落盘后）。
5) `resolve_online_lyric()`：缓存命中时也走一次幂等写入（目标正确则零副作用）。
"""
import io

BASE = r"<workspace>\nas_src\app_v53.py"
OUT = r"<workspace>\nas_src\app_v54.py"

src = io.open(BASE, "r", encoding="utf-8").read()
repls = []

# ================================================================ 1) CONF 新增
OLD = '''    "lyric_orphan_gc_interval_s": float(os.environ.get("FNMUSIC_LYRIC_ORPHAN_GC_INTERVAL_S", "1800")),
'''
assert src.count(OLD) == 1, "CONF v53 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    "lyric_orphan_gc_interval_s": float(os.environ.get("FNMUSIC_LYRIC_ORPHAN_GC_INTERVAL_S", "1800")),
    # v54 「收藏后歌词没贴身」修复：把留在 cache/ 的歌词提升到曲库同名 sidecar
    "lyric_promote": os.environ.get("FNMUSIC_LYRIC_PROMOTE", "true").lower() in ("true", "1", "yes"),
'''))

# ================================================================ 2) ★核心：lyric_cache_path 选路重排
OLD = '''def lyric_cache_path(guid: str, title: str = "", artist: str = "") -> str:
    """歌词落地位置。v53 起**绝不把无主歌词写进云盘曲库**。

    优先级：已有歌词 → 曲库音频的同名 sidecar → 缓存音频的同名 sidecar → 本地 cache/。

    此前最后两级是「写进 detect_library_dir()」—— 曲库是 rclone 云盘挂载，
    于是**播放任意在线曲目都会在曲库上传一个没有音频的 .lrc**（用户看到的孤儿歌词）。
    title/artist 仅保留参数兼容，不再参与选路。
    """
    found = find_lyric_file(guid)
    if found:
        return found
    lib_audio = materialized_library_file(guid)
    if lib_audio:
        return os.path.splitext(lib_audio)[0] + ".lrc"
    audio = find_cache_file(guid)
    if audio and not _same_dir(audio, detect_library_dir()):
        return os.path.splitext(audio)[0] + ".lrc"
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.lrc")
'''
assert src.count(OLD) == 1, "lyric_cache_path v53 count=%d" % src.count(OLD)
repls.append((OLD, '''def lyric_cache_path(guid: str, title: str = "", artist: str = "") -> str:
    """歌词落地位置。**歌词永远跟着音频走**。

    优先级：曲库音频的同名 sidecar → 已有歌词 → 缓存音频的同名 sidecar → 本地 cache/。

    v53 把「已有歌词」放在最前，引入了一个回归：只要**收藏之前**播过一次这首歌
    （App 拉歌词 → 落 cache/<guid>.lrc），这个缓存副本就会劫持落点，
    之后收藏整轨下载把音频落进曲库，**歌词再也不会贴身写到曲库**
    （用户反馈：「收藏音乐之后歌曲下载到了本地，但是歌词没有存储到本地」）。
    v54 起「音频已在曲库」优先于任何缓存副本 —— 有音频在，歌词就必须落在同名 sidecar。
    最后一档仍是本地 cache/，**绝不把无主歌词写进云盘曲库**（v53 的孤儿歌词修复保持）。
    title/artist 仅保留参数兼容，不参与选路。
    """
    lib_audio = materialized_library_file(guid)
    if lib_audio:
        return os.path.splitext(lib_audio)[0] + ".lrc"
    found = find_lyric_file(guid)
    if found:
        return found
    audio = find_cache_file(guid)
    if audio and not _same_dir(audio, detect_library_dir()):
        return os.path.splitext(audio)[0] + ".lrc"
    os.makedirs(CONF["cache_dir"], exist_ok=True)
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.lrc")
'''))

# ================================================================ 3) write_lyric_cache 短路条件 + 影子清理
OLD = '''def write_lyric_cache(guid: str, text: str, title: str = "", artist: str = "") -> None:
    text = (text or "").strip()
    if not text:
        return
    if text == read_lyric_cache(guid):
        return
    path = lyric_cache_path(guid, title=title, artist=artist)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        with open(part_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\\n")
        os.replace(part_path, path)
        adopt_library_perms(path)
        remember_lyric_path(guid, path)
'''
assert src.count(OLD) == 1, "write_lyric_cache v53 count=%d" % src.count(OLD)
repls.append((OLD, '''def write_lyric_cache(guid: str, text: str, title: str = "", artist: str = "") -> None:
    text = (text or "").strip()
    if not text:
        return
    path = lyric_cache_path(guid, title=title, artist=artist)
    # v54：短路条件从「**任意位置**内容一致」改成「**目标位置**内容一致」。
    # v53 的旧写法在 cache/<guid>.lrc 已存在时恒短路成功 ⇒ 歌词永远无法被提升到曲库同名 sidecar。
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                if f.read().strip() == text:
                    remember_lyric_path(guid, path)
                    _drop_shadow_lyric(guid, path)
                    return
    except Exception:
        pass
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        with open(part_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.write("\\n")
        os.replace(part_path, path)
        adopt_library_perms(path)
        remember_lyric_path(guid, path)
        _drop_shadow_lyric(guid, path)
'''))

# ================================================================ 4) 新增 v54 自愈模块
NEW_SECTION = '''# === v54：歌词「贴身」自愈 ===
#
# 目标：**歌词永远和音频放在一起**。
#   · 音频在曲库 ⇒ 歌词写成曲库同名 sidecar
#   · 音频只在 cache/ ⇒ 歌词写成 cache 内同名 sidecar
#   · 两处都没有 ⇒ 歌词落插件自己的 cache/<guid>.lrc（绝不写云盘曲库，v53 原则）
# 下面的自愈只处理「音频已在曲库、歌词却留在 cache/」这一种错位，
# 词干来源限于 cache/ 下 .ref 记过的路径，且该词干下确实有音频才动手。
def _drop_shadow_lyric(guid: str, keep_path: str) -> None:
    """歌词已写到正确位置后，清掉 cache/ 里同一 guid 的影子副本（只动我方缓存目录）。"""
    try:
        cache_dir = str(CONF.get("cache_dir") or "")
        if not cache_dir:
            return
        keep = os.path.realpath(keep_path)
        cand = os.path.join(cache_dir, f"{cache_safe_guid(guid)}.lrc")
        if os.path.realpath(cand) == keep or not os.path.isfile(cand):
            return
        os.remove(cand)
        _probe_write("[lyricshadow] removed %s" % cand)
    except Exception:
        pass


def _promote_lyric_file(src: str, dest: str, ref_path: str | None = None) -> bool:
    """把 cache/ 里的歌词搬到曲库同名 sidecar；dest 已有内容则只清影子副本。"""
    try:
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            os.remove(src)
            _probe_write("[lyricpromo] dropped-shadow %s" % src)
            return False
        with open(src, "rb") as fp:
            data = fp.read()
        if not data:
            return False
        tmp = f"{dest}.{uuid4().hex[:8]}.part"
        with open(tmp, "wb") as fp:
            fp.write(data)
        os.replace(tmp, dest)
        adopt_library_perms(dest)
        if ref_path:
            try:
                with open(ref_path, "w", encoding="utf-8") as fp:
                    fp.write(_path_stem(dest))
            except Exception:
                pass
        os.remove(src)
        _probe_write("[lyricpromo] %s" % dest)
        return True
    except Exception as e:
        logger.warning("Promote lyric %s -> %s failed: %s", src, dest, e)
        return False


def promote_one_lyric(guid: str) -> str | None:
    """单个 guid：音频已在曲库、歌词只在 cache/ ⇒ 搬成曲库同名 sidecar。"""
    if not CONF.get("lyric_promote"):
        return None
    try:
        lib_audio = materialized_library_file(guid)
    except Exception:
        return None
    if not lib_audio:
        return None
    cache_dir = str(CONF.get("cache_dir") or "")
    if not cache_dir:
        return None
    src = os.path.join(cache_dir, f"{cache_safe_guid(guid)}.lrc")
    if not os.path.isfile(src) or os.path.getsize(src) == 0:
        return None
    dest = os.path.splitext(lib_audio)[0] + ".lrc"
    ok = _promote_lyric_file(src, dest, lyric_ref_path(guid))
    return dest if ok else (dest if os.path.isfile(dest) else None)


def promote_library_lyrics() -> list:
    """扫一遍 cache/*.ref，把「音频已在曲库、歌词只留在 cache/」的歌词补成同名 sidecar。

    安全边界（与 sweep_orphan_lyrics 一致）：
      · 只认 cache/ 下 .ref 记过的词干，且必须是绝对路径、必须落在**曲库目录**内；
      · 该词干下确实存在音频（任一扩展名）才动手；
      · 歌词源文件必须是 cache/ 里我方的 <safe-guid>.lrc。
    """
    if not CONF.get("lyric_promote"):
        return []
    cache_dir = str(CONF.get("cache_dir") or "")
    if not cache_dir or not os.path.isdir(cache_dir):
        return []
    try:
        lib_dir = detect_library_dir()
    except Exception:
        return []
    if not lib_dir or not os.path.isdir(lib_dir):
        return []
    moved: list = []
    try:
        names = os.listdir(cache_dir)
    except Exception:
        return []
    for name in names:
        if not name.endswith(".ref") or name.endswith(LYRIC_REF_SUFFIX):
            continue
        safe = name[: -len(".ref")]
        src = os.path.join(cache_dir, f"{safe}.lrc")
        try:
            if not os.path.isfile(src) or os.path.getsize(src) == 0:
                continue
            with open(os.path.join(cache_dir, name), encoding="utf-8") as f:
                stem = _path_stem((f.read() or "").strip())
        except Exception:
            continue
        if not stem or not os.path.isabs(stem) or not _same_dir(stem, lib_dir):
            continue
        try:
            if not any(os.path.isfile(f"{stem}.{ext}") for ext in CACHE_EXTS):
                continue
        except Exception:
            continue
        if _promote_lyric_file(src, f"{stem}.lrc", os.path.join(cache_dir, f"{safe}{LYRIC_REF_SUFFIX}")):
            moved.append(f"{stem}.lrc")
    return moved


async def _lyric_promote_loop() -> None:
    """启动后补一次，之后按与孤儿清扫相同的间隔复扫。"""
    if not CONF.get("lyric_promote"):
        return
    try:
        interval = max(120.0, float(CONF.get("lyric_orphan_gc_interval_s") or 1800.0))
    except Exception:
        interval = 1800.0
    await asyncio.sleep(12.0)
    while True:
        try:
            moved = await asyncio.to_thread(promote_library_lyrics)
            if moved:
                logger.info("Promoted %d lyric(s) next to their library audio", len(moved))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Lyric promote sweep failed: %s", e)
        await asyncio.sleep(interval)


'''

OLD = "# === v53 孤儿歌词自愈 ==="
assert src.count(OLD) == 1, "v53 段落锚点 count=%d" % src.count(OLD)
repls.append((OLD, NEW_SECTION + OLD))

# ================================================================ 5) resolve_online_lyric 缓存命中时也幂等纠正
OLD = '''async def resolve_online_lyric(request: Request, guid: str) -> str:
    """本地 .lrc 优先；没有再向源站要，拿到就落盘。"""
    cached = read_lyric_cache(guid)
    if cached:
        return cached
'''
assert src.count(OLD) == 1, "resolve_online_lyric 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''async def resolve_online_lyric(request: Request, guid: str) -> str:
    """本地 .lrc 优先；没有再向源站要，拿到就落盘。"""
    cached = read_lyric_cache(guid)
    if cached:
        # v54 幂等自愈：音频已在曲库、歌词却留在 cache/ 时，这里把它补成曲库同名 sidecar。
        # 目标位置已正确时零副作用（只多读一次小文件）。
        try:
            await asyncio.to_thread(write_lyric_cache, guid, cached)
        except Exception:
            pass
        return cached
'''))

# ================================================================ 6) 启动时起 promote 循环
OLD = '''    # v53：启动后清一次孤儿歌词，之后周期复扫
    if CONF.get("lyric_orphan_gc"):
        _spawn_bg_task(_lyric_orphan_loop())
'''
assert src.count(OLD) == 1, "lifespan v53 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    # v53：启动后清一次孤儿歌词，之后周期复扫
    if CONF.get("lyric_orphan_gc"):
        _spawn_bg_task(_lyric_orphan_loop())

    # v54：启动后把「音频已在曲库、歌词却留在 cache/」的歌词补成曲库同名 sidecar
    if CONF.get("lyric_promote"):
        _spawn_bg_task(_lyric_promote_loop())
'''))

# ================================================================ 7) 整轨下载落盘后：歌词补位
OLD = '''            _probe_write("[favdl] ok guid=%s dest=%s bytes=%d exp=%s ext=%s lyric=%s" % (
                guid, dest, written, expected, ext, bool(lyric)))
            request_library_scan("favdl")
'''
assert src.count(OLD) == 1, "favdl ok 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''            _probe_write("[favdl] ok guid=%s dest=%s bytes=%d exp=%s ext=%s lyric=%s" % (
                guid, dest, written, expected, ext, bool(lyric)))
            # v54：音频已进曲库。若这次 info 里没带歌词（收藏前播放过、歌词已在 cache/），
            # 把那份缓存歌词提升成曲库同名 sidecar，保证「音频到哪、歌词到哪」。
            if not lyric:
                try:
                    await asyncio.to_thread(promote_one_lyric, guid)
                except Exception:
                    pass
            request_library_scan("favdl")
'''))

for old_s, new_s in repls:
    src = src.replace(old_s, new_s, 1)

io.open(OUT, "w", encoding="utf-8", newline="").write(src)
print("WROTE", OUT, "bytes=", len(src.encode("utf-8")))

# ================================================================ 自检
checks = {
    # v54 新增
    "def _drop_shadow_lyric(": 1,
    "def _promote_lyric_file(": 1,
    "def promote_one_lyric(": 1,
    "def promote_library_lyrics(": 1,
    "async def _lyric_promote_loop(": 1,
    "_spawn_bg_task(_lyric_promote_loop())": 1,
    '"lyric_promote": os.environ.get(': 1,
    "_drop_shadow_lyric(guid, path)": 2,
    "await asyncio.to_thread(promote_one_lyric, guid)": 1,
    "await asyncio.to_thread(write_lyric_cache, guid, cached)": 1,
    "_probe_write(\"[lyricshadow] removed %s\" % cand)": 1,
    "_probe_write(\"[lyricpromo] %s\" % dest)": 1,
    "_probe_write(\"[lyricpromo] dropped-shadow %s\" % src)": 1,
    "if _promote_lyric_file(src, f\"{stem}.lrc\"": 1,
    # v53 的孤儿修复必须保持
    "def sweep_orphan_lyrics(": 1,
    "async def _lyric_orphan_loop(": 1,
    "LYRIC_REF_SUFFIX = \".lyricref\"": 1,
    "return os.path.join(d, f\"{library_basename(title, artist)}.lrc\")": 0,
    "return os.path.join(CONF[\"cache_dir\"], f\"{cache_safe_guid(guid)}.lrc\")": 1,
    # 历史补丁不得被覆盖
    "async def _download_favorite_media(": 1,
    "def delete_materialized_media(": 1,
    "def _same_dir(path: str, directory: str) -> bool:": 1,
    "def request_library_scan(": 1,
    'request_library_scan("favdl")': 1,
    'request_library_scan("unfav")': 1,
    'request_library_scan("tee")': 1,
    "_probe_write(\"[favdl]": 7,
    "_probe_write(\"[favdel]": 1,
    "_probe_write(\"[scanreq]": 3,
    "_probe_write(\"[lyricgc] removed %s\" % lrc)": 1,
    "def lyric_ref_path(": 1,
    "def remember_lyric_path(": 1,
    "def recalled_lyric_path(": 1,
}
for needle, want in checks.items():
    got = src.count(needle)
    assert got == want, "%s count=%d want=%d" % (needle, got, want)

# ★ 回归守卫：lyric_cache_path 里「音频已在曲库」必须排在「已有歌词」之前
body = src.split("def lyric_cache_path(", 1)[1].split("\ndef ", 1)[0]
i_lib = body.index("lib_audio = materialized_library_file(guid)")
i_found = body.index("found = find_lyric_file(guid)")
assert i_lib < i_found, "lyric_cache_path 选路顺序错误：lib_audio@%d 应在 found@%d 之前" % (i_lib, i_found)

# ★ 回归守卫：write_lyric_cache 不得再出现「任意位置内容一致即返回」的旧短路
assert "if text == read_lyric_cache(guid):" not in src, "write_lyric_cache 仍在用旧短路条件"

compile(src, OUT, "exec")
print("OK v54 self-check passed (compile clean)")
print("lines:", src.count(chr(10)) + 1)
