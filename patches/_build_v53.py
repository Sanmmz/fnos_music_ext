"""增量式补丁构建器（v52 -> v53）：修「播放在线音乐在曲库留下孤儿歌词」。

输入：nas_src/app_v52.py
输出：nas_src/app_v53.py

===== 症状（用户 2026-09-18 反馈）=====
「播放在线音乐会在本地留下孤儿歌词文本」——
曲库里只有 1 个音频，却有 3 个 .lrc（其中 2 个无同名词曲）。

===== 真机取证 =====
cache/*.ref 内容（root 属主，需 sudo）：
  online_lx_wy_3407803435.ref      → /vol02/…/music/黄霄雲 _ 刘端端 - 空心 (Live版)   mp3+lrc 都在 ✓
  online_netease_1851652156.ref    → /vol02/…/music/h3R3 - 忘不掉的你               只有 .lrc ✗
  online_netease_2018733994.ref    → /vol02/…/music/郑润泽 - 遐想                    只有 .lrc ✗
  online_netease_1973665667.ref    → /vol02/…/music/online_netease_1973665667        两者都无
⇒ 4 个 .ref 全部指向**曲库目录**，说明歌词被写进了云盘曲库。

===== 根因 =====
`lyric_cache_path()` 在「没有已落盘音频、也没有缓存音频」时把**曲库目录**当兜底：
    d = detect_library_dir(); … return os.path.join(d, f"{歌手} - {歌名}.lrc")
而曲库是 rclone 云盘挂载 —— 写进去 = 真的上传一个没有音频的歌词。
再叠加两点，孤儿永久留存：
  ① 音频与歌词**共用一个 `.ref` 槽**：`remember_media_path()` 先写歌词词干，
     等音频落进 cache 后又被改写 ⇒ 歌词映射丢失（同时让下次播放重复出网抓词）。
  ② `cache_gc.purge_rolling()` 的 `_stem_has_file()` 把 ".lrc" 也算作"目标还在"，
     于是连那个指向孤儿歌词的 `.ref` 都不清理。

===== v53 修法 =====
1) 歌词落地位置：只有 `materialized_library_file(guid)`（音频真在曲库）才写同名 sidecar；
   否则一律写本地 `cache/<guid>.lrc`。云盘曲库不再产出无主歌词。
2) 歌词单独用 `<guid>.lyricref` 记路径，不再和音频共用 `.ref`。
3) 新增 `sweep_orphan_lyrics()`：只清「插件自己写下、且音频已不存在」的曲库歌词
   （靠 .ref/.lyricref 识别，绝不碰飞牛自己管的歌词）；启动后清一次 + 每 30 分钟复扫。
"""
import io

BASE = r"<workspace>\nas_src\app_v52.py"
OUT = r"<workspace>\nas_src\app_v53.py"

src = io.open(BASE, "r", encoding="utf-8").read()
repls = []

# ================================================================ 1) CONF 新增
OLD = '''    "auto_scan_scan_all": os.environ.get("FNMUSIC_AUTO_SCAN_SCAN_ALL", "false").lower() in ("true", "1", "yes"),
'''
assert src.count(OLD) == 1, "CONF v52 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''    "auto_scan_scan_all": os.environ.get("FNMUSIC_AUTO_SCAN_SCAN_ALL", "false").lower() in ("true", "1", "yes"),
    # v53 歌词 sidecar 归属 + 孤儿歌词自愈
    "lyric_orphan_gc": os.environ.get("FNMUSIC_LYRIC_ORPHAN_GC", "true").lower() in ("true", "1", "yes"),
    "lyric_orphan_gc_min_age_s": float(os.environ.get("FNMUSIC_LYRIC_ORPHAN_GC_MIN_AGE_S", "120")),
    "lyric_orphan_gc_interval_s": float(os.environ.get("FNMUSIC_LYRIC_ORPHAN_GC_INTERVAL_S", "1800")),
'''))

# ================================================================ 2) 歌词专用 .lyricref
OLD = '''def media_ref_path(guid: str) -> str:
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.ref")
'''
assert src.count(OLD) == 1, "media_ref_path count=%d" % src.count(OLD)
repls.append((OLD, '''def media_ref_path(guid: str) -> str:
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.ref")


# v53：歌词单独占一个映射槽，避免把音频的 .ref 挤掉（此前音频/歌词共用 .ref，
# 音频从曲库切到 cache/ 时会把歌词映射覆盖掉 ⇒ 反复重抓歌词、且曲库残留孤儿 .lrc）。
LYRIC_REF_SUFFIX = ".lyricref"


def lyric_ref_path(guid: str) -> str:
    return os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}{LYRIC_REF_SUFFIX}")


def remember_lyric_path(guid: str, lyric_path: str) -> None:
    try:
        os.makedirs(CONF["cache_dir"], exist_ok=True)
        with open(lyric_ref_path(guid), "w", encoding="utf-8") as f:
            f.write(_path_stem(lyric_path))
    except Exception as e:
        logger.warning("Failed to remember lyric path for %s: %s", guid, e)


def recalled_lyric_path(guid: str) -> str | None:
    ref = lyric_ref_path(guid)
    if not os.path.exists(ref):
        return None
    try:
        with open(ref, encoding="utf-8") as f:
            stem = _path_stem(f.read().strip())
    except Exception:
        return None
    if not stem:
        return None
    path = f"{stem}.lrc"
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    except Exception:
        pass
    return None
'''))

# ================================================================ 3) find_lyric_file 先查 lyricref
OLD = '''def find_lyric_file(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if stem:
        sibling = f"{stem}.lrc"'''
assert src.count(OLD) == 1, "find_lyric_file count=%d" % src.count(OLD)
repls.append((OLD, '''def find_lyric_file(guid: str) -> str | None:
    v53_pinned = recalled_lyric_path(guid)
    if v53_pinned:
        return v53_pinned
    stem = recalled_media_stem(guid)
    if stem:
        sibling = f"{stem}.lrc"'''))

# ================================================================ 4) lyric_cache_path：核心修复
OLD = '''def lyric_cache_path(guid: str, title: str = "", artist: str = "") -> str:
    found = find_lyric_file(guid)
    if found:
        return found
    audio = find_cache_file(guid)
    if audio:
        return os.path.splitext(audio)[0] + ".lrc"
    d = detect_library_dir()
    os.makedirs(d, exist_ok=True)
    if (title or "").strip() or (artist or "").strip():
        return os.path.join(d, f"{library_basename(title, artist)}.lrc")
    return os.path.join(d, f"{cache_safe_guid(guid)}.lrc")
'''
assert src.count(OLD) == 1, "lyric_cache_path count=%d" % src.count(OLD)
repls.append((OLD, '''def lyric_cache_path(guid: str, title: str = "", artist: str = "") -> str:
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
'''))

# ================================================================ 5) write_lyric_cache 收尾
OLD = '''        os.replace(part_path, path)
        adopt_library_perms(path)
        remember_media_path(guid, path)'''
assert src.count(OLD) == 1, "write_lyric_cache 收尾 count=%d" % src.count(OLD)
repls.append((OLD, '''        os.replace(part_path, path)
        adopt_library_perms(path)
        remember_lyric_path(guid, path)
        # 只有「歌词就是音频的同名 sidecar」时才动 media .ref，避免把音频映射挤掉
        _audio = materialized_library_file(guid) or find_cache_file(guid) or ""
        if _audio and os.path.splitext(_audio)[0] == os.path.splitext(path)[0]:
            remember_media_path(guid, path)'''))

# ================================================================ 6) delete_materialized_media 带上歌词映射
OLD = '''    cached = find_cache_file(guid)
    if cached:
        stems.add(os.path.splitext(cached)[0])'''
assert src.count(OLD) == 1, "delete stems count=%d" % src.count(OLD)
repls.append((OLD, '''    cached = find_cache_file(guid)
    if cached:
        stems.add(os.path.splitext(cached)[0])
    pinned_lyric = recalled_lyric_path(guid)
    if pinned_lyric:
        stems.add(os.path.splitext(pinned_lyric)[0])'''))

OLD = '''    ref = media_ref_path(guid)
    if os.path.isfile(ref):
        try:
            os.remove(ref)
            removed.append(ref)
        except Exception:
            pass
    _probe_write("[favdel] guid=%s removed=%d %s" % (guid, len(removed), "|".join(removed) or "-"))'''
assert src.count(OLD) == 1, "delete ref count=%d" % src.count(OLD)
repls.append((OLD, '''    for ref in (media_ref_path(guid), lyric_ref_path(guid)):
        if os.path.isfile(ref):
            try:
                os.remove(ref)
                removed.append(ref)
            except Exception:
                pass
    _probe_write("[favdel] guid=%s removed=%d %s" % (guid, len(removed), "|".join(removed) or "-"))'''))

# ================================================================ 7) 新增自愈模块
NEW_SECTION = '''# === v53 孤儿歌词自愈 ===
#
# 只清「插件自己写下、且音频已不存在」的曲库歌词 sidecar：
#   · 词干来源 = cache/ 下的 .ref / .lyricref（即本插件记过的落盘路径）
#   · 词干必须落在**曲库目录**内（cache/ 里的孤儿交由 cache_gc.purge_rolling 处理）
#   · 同名词曲文件还在 ⇒ 不是孤儿，跳过
#   · 刚落盘（mtime 未满 min_age）先放过，避免和下载竞态
# ⇒ 因此绝不会动飞牛自己下载/管理的歌词。
def sweep_orphan_lyrics(min_age_s: float | None = None) -> list:
    if not CONF.get("lyric_orphan_gc"):
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
    if min_age_s is None:
        try:
            min_age_s = float(CONF.get("lyric_orphan_gc_min_age_s") or 120.0)
        except Exception:
            min_age_s = 120.0
    now = time.time()
    removed: list = []
    try:
        names = os.listdir(cache_dir)
    except Exception:
        return []
    for name in names:
        if not (name.endswith(".ref") or name.endswith(LYRIC_REF_SUFFIX)):
            continue
        ref = os.path.join(cache_dir, name)
        try:
            with open(ref, encoding="utf-8") as f:
                stem = _path_stem((f.read() or "").strip())
        except Exception:
            continue
        if not stem or not os.path.isabs(stem):
            continue
        if not _same_dir(stem, lib_dir):
            continue
        lrc = f"{stem}.lrc"
        try:
            if not os.path.isfile(lrc):
                continue
            if any(os.path.isfile(f"{stem}.{ext}") for ext in CACHE_EXTS):
                continue
            if now - os.path.getmtime(lrc) < max(0.0, min_age_s):
                continue
        except Exception:
            continue
        if not _safe_unlink_in_media_dirs(lrc):
            continue
        removed.append(lrc)
        _probe_write("[lyricgc] removed %s" % lrc)
        logger.info("Removed orphan lyric sidecar: %s", lrc)
        if name.endswith(LYRIC_REF_SUFFIX):
            try:
                os.remove(ref)
            except Exception:
                pass
    return removed


async def _lyric_orphan_loop() -> None:
    """启动后清一次，之后按间隔复扫（用户可能在云盘侧直接删歌）。"""
    if not CONF.get("lyric_orphan_gc"):
        return
    try:
        interval = max(120.0, float(CONF.get("lyric_orphan_gc_interval_s") or 1800.0))
    except Exception:
        interval = 1800.0
    await asyncio.sleep(15.0)
    while True:
        try:
            removed = await asyncio.to_thread(sweep_orphan_lyrics)
            if removed:
                logger.info("Orphan lyric sweep removed %d file(s)", len(removed))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Orphan lyric sweep failed: %s", e)
        await asyncio.sleep(interval)


'''

OLD = "# === v52 落盘/删除后主动触发飞牛扫库 ==="
assert src.count(OLD) == 1, "v52 段落锚点 count=%d" % src.count(OLD)
repls.append((OLD, NEW_SECTION + OLD))

# ================================================================ 8) 启动时起自愈循环
OLD = '''        created_llm = True

    try:
        yield'''
assert src.count(OLD) == 1, "lifespan 锚点 count=%d" % src.count(OLD)
repls.append((OLD, '''        created_llm = True

    # v53：启动后清一次孤儿歌词，之后周期复扫
    if CONF.get("lyric_orphan_gc"):
        _spawn_bg_task(_lyric_orphan_loop())

    try:
        yield'''))

# ================================================================ 9) 取消收藏后顺带扫一次
OLD = '''        if removed:
            request_library_scan("unfav")'''
assert src.count(OLD) == 1, "unfav 触发点 count=%d" % src.count(OLD)
repls.append((OLD, '''        if removed:
            request_library_scan("unfav")
            try:
                await asyncio.to_thread(sweep_orphan_lyrics)
            except Exception:
                pass'''))

for old_s, new_s in repls:
    src = src.replace(old_s, new_s, 1)

io.open(OUT, "w", encoding="utf-8", newline="").write(src)
print("WROTE", OUT, "bytes=", len(src.encode("utf-8")))

checks = {
    # v53 新增
    "def lyric_ref_path(": 1,
    "def remember_lyric_path(": 1,
    "def recalled_lyric_path(": 1,
    "def sweep_orphan_lyrics(": 1,
    "async def _lyric_orphan_loop(": 1,
    "LYRIC_REF_SUFFIX = \".lyricref\"": 1,
    "_spawn_bg_task(_lyric_orphan_loop())": 1,
    "_probe_write(\"[lyricgc] removed %s\" % lrc)": 1,
    "for ref in (media_ref_path(guid), lyric_ref_path(guid)):": 1,
    "    v53_pinned = recalled_lyric_path(guid)": 1,
    # 关键：曲库已不再是歌词兜底目标
    "return os.path.join(d, f\"{library_basename(title, artist)}.lrc\")": 0,
    "return os.path.join(CONF[\"cache_dir\"], f\"{cache_safe_guid(guid)}.lrc\")": 1,
    "    lib_audio = materialized_library_file(guid)": 1,
    # 历史补丁不得被覆盖
    "async def _download_favorite_media(": 1,
    "def delete_materialized_media(": 1,
    "def _same_dir(path: str, directory: str) -> bool:": 1,
    "def request_library_scan(": 1,
    'request_library_scan("favdl")': 1,
    'request_library_scan("unfav")': 1,
    "_probe_write(\"[favdl]": 7,
    "_probe_write(\"[favdel]": 1,
    "_probe_write(\"[scanreq]": 3,
    "for key in matched_keys:": 1,
}
for needle, want in checks.items():
    got = src.count(needle)
    assert got == want, "%s count=%d want=%d" % (needle, got, want)

compile(src, OUT, "exec")
print("OK v53 self-check passed (compile clean)")
print("lines:", src.count(chr(10)) + 1)
