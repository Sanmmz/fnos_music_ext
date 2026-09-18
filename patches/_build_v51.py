"""增量式补丁构建器（v50 -> v51）：修「陈旧 .ref 映射把落盘写到旧曲库目录」。

输入：nas_src/app_v50.py
输出：nas_src/app_v51.py

===== 现象（v50 端到端验收实测）=====
收藏一首在线曲目后触发整轨下载，下载本身成功，但改名时抛：
  FileNotFoundError: '/vol02/<vol-id>/music/online_netease_1827600686.<uuid>.part'
                   -> '/vol2/1000/music/林达浪 _ h3R3 - 还是会想你.flac'

===== 根因 =====
`cache/<guid>.ref` 记录了「该 guid 的曲库文件词干」。本机历史上
`shared_library.path` 是 `/vol2/1000/music`，今天 16:03 被改成
`/vol02/<vol-id>/music`（rclone 云盘挂载），于是 cache/ 里遗留了 10 个
指向 `/vol2/1000/music/...` 的 .ref —— 该目录**已不存在**。

`library_media_path()` 里的两处「复用已有路径」逻辑没有校验目录一致性：
  · `recalled_media_path()` 尚可（内部会判断文件存在）→ 返回 None；
  · 但紧跟的 `recalled_media_stem()` **无条件**返回 `<旧目录>/<词干>`，
    于是 dest 落在不存在的目录上，`os.replace` 报 ENOENT（目标目录不存在）。

===== v51 修法 =====
1. `library_media_path()`：复用 .ref 时必须与目标目录 `directory` 同一目录
   （真实路径比较），否则忽略旧映射、按「歌手 - 歌名」在目标目录新建。
   这样既修好新曲库，也避免旧曲库路径复活。
2. 下载落盘改名加 `shutil.move` 兜底（跨文件系统 rename 会抛 EXDEV）。
"""
import io

BASE = r"<workspace>\nas_src\app_v50.py"
OUT = r"<workspace>\nas_src\app_v51.py"

src = io.open(BASE, "r", encoding="utf-8").read()
repls = []

# ---------------------------------------------------------------- 1) 目录一致性
OLD = '''def library_media_path(guid: str, title: str, ext: str, artist: str = "", directory: str | None = None) -> str:
    lib = directory or detect_library_dir()
    recalled = recalled_media_path(guid)
    if recalled and not _is_rolling_cache_stem(recalled, guid):
        return recalled
    stem = recalled_media_stem(guid)
    if stem and not _is_rolling_cache_stem(stem, guid):
        return f"{stem}.{ext}"
    os.makedirs(lib, exist_ok=True)
    return unique_library_path(lib, library_basename(title, artist), ext)'''
assert src.count(OLD) == 1, "library_media_path count=%d" % src.count(OLD)
repls.append((OLD, '''def _same_dir(path: str, directory: str) -> bool:
    """path 是否就落在 directory 里（真实路径比较）。

    曲库目录被更换过时，cache/*.ref 会留着旧目录里的词干；不复核就可能
    把新文件"写回"一个已不存在的旧曲库路径（rename 报 ENOENT）。
    """
    try:
        return os.path.dirname(os.path.realpath(path)) == os.path.realpath(directory).rstrip("/")
    except Exception:
        return False


def library_media_path(guid: str, title: str, ext: str, artist: str = "", directory: str | None = None) -> str:
    lib = directory or detect_library_dir()
    # v51: 复用旧映射必须先确认它和新曲库是同一个目录
    recalled = recalled_media_path(guid)
    if recalled and not _is_rolling_cache_stem(recalled, guid) and _same_dir(recalled, lib):
        return recalled
    stem = recalled_media_stem(guid)
    if stem and not _is_rolling_cache_stem(stem, guid) and _same_dir(stem, lib):
        return f"{stem}.{ext}"
    os.makedirs(lib, exist_ok=True)
    return unique_library_path(lib, library_basename(title, artist), ext)'''))

# ---------------------------------------------------------------- 2) 改名兜底
OLD = """            dest = library_media_path(guid, title, ext, artist=artist, directory=directory)
            os.replace(part, dest)
            part = None"""
assert src.count(OLD) == 1, "favdl 改名 count=%d" % src.count(OLD)
repls.append((OLD, """            dest = library_media_path(guid, title, ext, artist=artist, directory=directory)
            try:
                os.replace(part, dest)
            except OSError:
                # 跨文件系统时 rename 会抛 EXDEV；就地复制再删
                shutil.move(part, dest)
            part = None"""))

for old_s, new_s in repls:
    src = src.replace(old_s, new_s, 1)

io.open(OUT, "w", encoding="utf-8", newline="").write(src)
print("WROTE", OUT, "bytes=", len(src.encode("utf-8")))

checks = {
    "def _same_dir(path: str, directory: str) -> bool:": 1,
    "_same_dir(recalled, lib)": 1,
    "_same_dir(stem, lib)": 1,
    "shutil.move(part, dest)": 2,          # demote + favdl 兜底
    "if _library_ok and not title.strip() and not artist.strip():": 1,
    "async def _download_favorite_media(": 1,
    "_probe_write(\"[favdl]": 7,
    "_probe_write(\"[favdel]": 1,
    "_probe_write(\"[tee] start": 1,
    "_probe_write(\"[tee] finally": 1,
    "_probe_write(\"[hist-del]": 4,
    "https://music.163.com/api/song/enhance/player/url": 1,
    "def _spawn_bg(": 1,
    "for key in matched_keys:": 1,
}
for needle, want in checks.items():
    got = src.count(needle)
    assert got == want, "%s count=%d want=%d" % (needle, got, want)

compile(src, OUT, "exec")
print("OK v51 self-check passed (compile clean)")
print("lines:", src.count(chr(10)) + 1)
