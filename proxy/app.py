"""fnmusic-ext 拦截代理 (FastAPI + httpx).

功能：
1. 通用透传：所有非拦截路径原样转发到 trim-music unix socket
2. 搜索合并：GET /music/api/v1/search/track* （兼容 q/keyword，并行 musicdl）
3. 在线播放：stream + HLS 兜底 + transcode 空操作 + tee 缓存回放（音频与歌词 sidecar）
4. 在线元数据/歌词/封面
5. GET /_ext/healthz
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Callable, Coroutine
from urllib.parse import quote
from uuid import uuid4

import httpx
import anyio
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

try:
    from . import recommend as dailyrec
    from .cache_gc import purge_rolling
    from .version import get_version
except ImportError:  # uvicorn --app-dir proxy
    import recommend as dailyrec  # type: ignore
    from cache_gc import purge_rolling  # type: ignore
    from version import get_version  # type: ignore

logger = logging.getLogger("fnmusic_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_HOME = dailyrec.home_dir()

CONF = {
    "musicdl_url": os.environ.get("FNMUSIC_MUSICDL_URL", "http://127.0.0.1:8768"),
    "musicbox_url": os.environ.get("FNMUSIC_MUSICBOX_URL", "http://127.0.0.1:8770"),
    "lx_url": os.environ.get("FNMUSIC_LX_URL", "http://127.0.0.1:8772"),
    "musicdl_enabled": os.environ.get("FNMUSIC_MUSICDL_ENABLED", "true").lower() in ("true", "1", "yes"),
    "netease_enabled": os.environ.get("FNMUSIC_NETEASE_ENABLED", "true").lower() in ("true", "1", "yes"),
    "lx_enabled": os.environ.get("FNMUSIC_LX_ENABLED", "true").lower() in ("true", "1", "yes"),
    "lx_search_limit": int(os.environ.get("FNMUSIC_LX_SEARCH_LIMIT", "20")),
    "lx_quality": os.environ.get("FNMUSIC_LX_QUALITY", "lossless"),
    "netease_wait_s": float(os.environ.get("FNMUSIC_NETEASE_WAIT_S", "3.0")),
    "netease_quality": os.environ.get("FNMUSIC_NETEASE_QUALITY", "lossless"),
    "netease_search_limit": int(os.environ.get("FNMUSIC_NETEASE_SEARCH_LIMIT", "50")),
    # v45 取流快通道：官方接口直取播放地址（0.1s 级），任何失败一律回落 musicbox
    "netease_direct": os.environ.get("FNMUSIC_NETEASE_DIRECT", "true").lower() in ("true", "1", "yes"),
    "netease_cookie_file": os.environ.get(
        "FNMUSIC_NETEASE_COOKIE_FILE",
        os.path.join(_HOME, "musicbox-data", "netease-musicbox", "cookie.txt"),
    ),
    "stream_url_ttl": float(os.environ.get("FNMUSIC_STREAM_URL_TTL", "600")),
    "upstream_sock": os.environ.get("FNMUSIC_UPSTREAM_SOCK", "/var/run/trim_music_upstream.socket"),
    "online_limit": int(os.environ.get("FNMUSIC_ONLINE_LIMIT", "30")),
    "search_list_path": os.environ.get("FNMUSIC_SEARCH_LIST_PATH", "data.list"),
    "cache_dir": os.environ.get("FNMUSIC_CACHE_DIR", os.path.join(_HOME, "cache")),
    # 空=从飞牛 shared_library.path 自动探测；测试可覆盖到临时目录
    "library_dir": os.environ.get("FNMUSIC_LIBRARY_DIR", ""),
    "music_db": os.environ.get(
        "FNMUSIC_MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db"
    ),
    # 边听边存：默认开；保存路径空=自动探测飞牛共享曲库，不可用自动回退；
    # tee_cache_max 仅在关闭边听边存时生效（滚动保留最新 N 首试听缓存）
    "tee_save_enabled": os.environ.get("FNMUSIC_TEE_SAVE_ENABLED", "true").lower() in ("true", "1", "yes"),
    "tee_save_dir": os.environ.get("FNMUSIC_TEE_SAVE_DIR", ""),
    "tee_cache_max": int(os.environ.get("FNMUSIC_TEE_CACHE_MAX", "2")),
    "merge_suggest": os.environ.get("FNMUSIC_MERGE_SUGGEST", "false").lower() in ("true", "1", "yes"),
    "online_sources": os.environ.get("FNMUSIC_ONLINE_SOURCES", "KuwoMusicClient,MiguMusicClient"),
    "lyric_field": os.environ.get("FNMUSIC_LYRIC_FIELD", "data.lyric"),
    "search_timeout": float(os.environ.get("FNMUSIC_SEARCH_TIMEOUT", "15")),
    "search_cache_ttl": float(os.environ.get("FNMUSIC_SEARCH_CACHE_TTL", "604800")),
    "late_page_wait_s": float(os.environ.get("FNMUSIC_LATE_PAGE_WAIT_S", "5.0")),
    "fav_dir": os.environ.get(
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
    # v52 落盘/删除后主动触发飞牛扫库
    "auto_scan": os.environ.get("FNMUSIC_AUTO_SCAN", "true").lower() in ("true", "1", "yes"),
    "auto_scan_delay_s": float(os.environ.get("FNMUSIC_AUTO_SCAN_DELAY_S", "3")),
    "auto_scan_auth_ttl_s": float(os.environ.get("FNMUSIC_AUTO_SCAN_AUTH_TTL_S", "90")),
    "auto_scan_scan_all": os.environ.get("FNMUSIC_AUTO_SCAN_SCAN_ALL", "false").lower() in ("true", "1", "yes"),
    # v53 歌词 sidecar 归属 + 孤儿歌词自愈
    "lyric_orphan_gc": os.environ.get("FNMUSIC_LYRIC_ORPHAN_GC", "true").lower() in ("true", "1", "yes"),
    "lyric_orphan_gc_min_age_s": float(os.environ.get("FNMUSIC_LYRIC_ORPHAN_GC_MIN_AGE_S", "120")),
    "lyric_orphan_gc_interval_s": float(os.environ.get("FNMUSIC_LYRIC_ORPHAN_GC_INTERVAL_S", "1800")),
    # v54 「收藏后歌词没贴身」修复：把留在 cache/ 的歌词提升到曲库同名 sidecar
    "lyric_promote": os.environ.get("FNMUSIC_LYRIC_PROMOTE", "true").lower() in ("true", "1", "yes"),
    # v55 搜索结果按「有海报 + 高音质」优先排序
    "search_rank": os.environ.get("FNMUSIC_SEARCH_RANK", "cover_quality"),
    "search_enrich": os.environ.get("FNMUSIC_SEARCH_ENRICH", "true").lower() in ("true", "1", "yes"),
    "search_enrich_limit": int(os.environ.get("FNMUSIC_SEARCH_ENRICH_LIMIT", "30")),
    "search_enrich_wait_s": float(os.environ.get("FNMUSIC_SEARCH_ENRICH_WAIT_S", "1.5")),
    "search_rank_wait_s": float(os.environ.get("FNMUSIC_SEARCH_RANK_WAIT_S", "1.5")),
    "llm_base_url": (os.environ.get("FNMUSIC_LLM_BASE_URL") or "").strip().rstrip("/"),
    "llm_model": (os.environ.get("FNMUSIC_LLM_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini",
}

_REDACT_KEY_PARTS = ("api_key", "apikey", "token", "secret", "password")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

CACHE_EXTS = ("mp3", "flac", "wav", "ogg", "opus", "m4a", "aac", "ape", "wv", "dsf", "dff", "tta")

# 飞牛 Kl() 归一化：mpeg/mp3→mp3，wav/pcm→wav，m4a/aac/mp4→m4a，其余小写原样（flac/ogg/ape/wv…）
_FORMAT_ALIASES = {
    "mp3": "mp3",
    "mpeg": "mp3",
    "mpga": "mp3",
    "flac": "flac",
    "wav": "wav",
    "wave": "wav",
    "pcm": "wav",
    "lpcm": "wav",
    "ogg": "ogg",
    "vorbis": "ogg",
    "opus": "opus",
    "m4a": "m4a",
    "mp4": "m4a",
    "mp4a": "m4a",
    "aac": "m4a",
    "alac": "m4a",
    "ape": "ape",
    "wv": "wv",
    "wavpack": "wv",
    "dsf": "dsf",
    "dff": "dff",
    "dsd": "dsd",
    "tta": "tta",
    "tak": "tak",
    "wma": "wma",
    "aiff": "aiff",
    "aif": "aiff",
}


# 模块级搜索缓存
_SEARCH_CACHE: dict[str, dict] = {}


def _search_ttl(entry: dict) -> float:
    # Backend IDs/URLs are memory scoped; positive results revalidate in 5m.
    if entry.get("partial"):
        return 30.0
    if not entry.get("items"):
        return 10.0
    return min(float(CONF.get("search_cache_ttl", 604800)), 300.0)


def _clean_search_cache() -> None:
    now = time.time()
    expired = [k for k, v in _SEARCH_CACHE.items() if now - v.get("accessed", v.get("ts", 0)) >= 900]
    if len(_SEARCH_CACHE) > 2000:
        expired += sorted(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k].get("accessed", 0))[:1000]
    for key in expired:
        entry = _SEARCH_CACHE.pop(key, {})
        task = entry.get("task")
        if task and not task.done():
            task.cancel()


def _set_search_cache(keyword: str, entry: dict) -> None:
    _clean_search_cache()
    _SEARCH_CACHE[keyword] = entry


def _source_config() -> dict:
    return {k: v for k, v in CONF.items() if k.endswith(("_enabled", "_url", "_limit", "_quality")) or k == "online_sources"}


def _search_scope(request: Request) -> str:
    auth = [request.headers.get(k, "") for k in ("cookie", "authorization", "x-trim-music-temp-token")]
    config = _source_config()
    filters = sorted((k, v) for k, v in request.query_params.multi_items() if k not in ("page", "q", "query", "keyword"))
    return hashlib.sha256(json.dumps([auth, config, filters, request.url.path], sort_keys=True).encode()).hexdigest()


def _source_enabled(guid: str) -> bool:
    source = source_from_online_guid(guid)
    if not CONF.get({"netease": "netease_enabled", "lx": "lx_enabled"}.get(source, "musicdl_enabled"), True):
        return False
    if source not in ("netease", "lx") and CONF.get("online_sources"):
        selected = {name.strip().lower().removesuffix("musicclient") for name in str(CONF["online_sources"]).split(",")}
        return source.lower() in selected
    return True


ONLINE_TRIAL_MARKERS = (
    "(试听)",
    "（试听）",
    "试听片段",
    "片段试听",
    "试听版",
    "[试听]",
    "【试听】",
    "- 试听",
    " - 试听",
)


def is_playable_online_track(item: dict, require_id: bool = False) -> bool:
    """最终防线校验：过滤无音频流或试听标记的不可播曲目。"""
    if not isinstance(item, dict):
        return False

    title = str(item.get("title") or item.get("name") or item.get("song_name") or "").strip()
    if not title:
        return False

    if require_id:
        sid = str(item.get("id") or item.get("song_id") or item.get("guid") or "").strip()
        if not sid:
            return False

    # 1. 标题含试听标记
    if any(marker in title for marker in ONLINE_TRIAL_MARKERS):
        return False

    # 2. 字段试听标记
    if item.get("is_trial") is True or item.get("freeTrialInfo") or item.get("freeTrialPrivilege"):
        return False
    if int(item.get("is_free_part") or 0) != 0 or int(item.get("fail_process") or 0) == 4:
        return False

    # 3. 收费/VIP 拦截（verified 条目已由服务端完成"直链解析+Range探活"验证，可播性有实证，跳过收费元数据拦截）
    if item.get("verified") is not True:
        if int(item.get("pay_type") or 0) != 0:
            return False
        if int(item.get("pkg_price") or 0) != 0 or int(item.get("price") or 0) != 0:
            return False
        fee = item.get("fee")
        if fee is not None:
            try:
                if int(fee) not in (0, 8):
                    return False
            except (ValueError, TypeError):
                pass

    # 4. 显式不可播/无流标记
    if item.get("unplayable") is True or item.get("playable") is False:
        return False
    if item.get("has_stream") is False:
        return False

    # 5. 音频流直链校验：若带有 download_url 或 url 键，则必须合法可用，绝不能是空串或 404
    if "download_url" in item:
        d_url = str(item.get("download_url") or "").strip()
        if not d_url or not d_url.startswith(("http://", "https://")) or "404/error.html" in d_url or "error.html" in d_url:
            return False
    if "url" in item:
        u = str(item.get("url") or "").strip()
        if not u or "404/error.html" in u or "error.html" in u:
            return False

    # 6. 片段时长校验（<=35s 且带有试听迹象）
    duration = item.get("duration_s") or (item.get("duration") or 0)
    try:
        duration_s = float(duration)
        if 0 < duration_s <= 35 and ("试听" in title or item.get("is_trial")):
            return False
    except (ValueError, TypeError):
        pass

    return True


def _same_recording(left: dict, right: dict) -> bool:
    """Conservative identity: never strip live/remix/version markers."""
    for key in ("title", "artist", "version"):
        a, b = (str(x.get(key) or "").strip().casefold() for x in (left, right))
        if a != b or (key != "version" and not a):
            return False
    try:
        a, b = float(left.get("duration_s") or 0), float(right.get("duration_s") or 0)
        return math.isfinite(a) and math.isfinite(b) and a > 0 and b > 0 and abs(a - b) <= 2.0
    except (TypeError, ValueError):
        return False


def deduplicate_online_items(items: list[dict]) -> list[dict]:
    """Keep the published representative and strict recording alternatives."""
    result: list[dict] = []
    seen = set()
    for item in items:
        if not is_playable_online_track(item):
            continue
        guid = online_guid_from_item(item)
        if guid in seen:
            continue
        seen.add(guid)
        representative = next((x for x in result if _same_recording(x, item)), None)
        if representative is None:
            representative = dict(item)
            representative["_alternatives"] = list(item.get("_alternatives", []))
            result.append(representative)
        else:
            alternatives = representative.setdefault("_alternatives", [])
            if guid not in {online_guid_from_item(x) for x in alternatives}:
                alternatives.append({k: v for k, v in item.items() if k != "_alternatives"})
    return result


def play_format_from_ext(ext: str | None) -> str:
    raw = (ext or "mp3").strip().lower().lstrip(".")
    if raw.startswith("audio/"):
        raw = raw.split("/", 1)[-1]
    return _FORMAT_ALIASES.get(raw, raw or "mp3")


def filter_headers(headers: Any, exclude_keys: set | None = None) -> dict:
    exclude = HOP_BY_HOP | {k.lower() for k in (exclude_keys or set())}
    return {k: v for k, v in headers.items() if k.lower() not in exclude}


def copy_incoming_headers(request: Request) -> dict:
    """透传鉴权 Cookie / Token。Starlette 头名为小写，需显式回填以免丢失 music-token。

    authx 为新版官方前端登录后的逐请求签名头（含时间戳与随机数），
    原样转发给上游即可通过校验；切勿缓存或复用其值。"""
    headers = filter_headers(request.headers, exclude_keys={"host", "content-length"})
    headers["accept-encoding"] = "identity"
    for key in ("cookie", "authorization", "x-trim-music-temp-token", "authx"):
        val = request.headers.get(key)
        if val:
            headers[key] = val
    return headers


def get_by_path(d: Any, path: str) -> Any:
    curr = d
    for p in path.split("."):
        if isinstance(curr, dict) and p in curr:
            curr = curr[p]
        else:
            return None
    return curr


def set_by_path(d: dict, path: str, val: Any):
    parts = path.split(".")
    curr = d
    for p in parts[:-1]:
        if p not in curr or not isinstance(curr[p], dict):
            curr[p] = {}
        curr = curr[p]
    curr[parts[-1]] = val


def extract_keyword(request: Request) -> str:
    """前端打包用 q，部分调用/验收用 keyword。"""
    params = request.query_params
    return (params.get("keyword") or params.get("q") or params.get("query") or "").strip()


def online_guid_from_item(item: dict) -> str:
    raw_id = str(item.get("id") or "")
    src = str(item.get("source") or "")
    if raw_id.startswith("online:"):
        return raw_id
    if ":" in raw_id:
        return f"online:{raw_id}"
    return f"online:{src}:{raw_id}"


def song_id_from_online_guid(guid: str) -> str:
    if guid.startswith("online:"):
        return guid[len("online:") :]
    return guid


def is_online_guid(guid: str) -> bool:
    return bool(guid) and guid.startswith("online:")


_MODE_FILE = os.path.join(_HOME, "ONLINE_HISTORY_MODE")
_MODE_CACHE = {"ts": 0.0, "val": "off"}


def online_history_mode() -> str:
    """full=在线曲目写入收藏/历史；off=不写入（默认，手机端列表恒为本地曲目，绝不再空白）。"""
    try:
        now = time.time()
        if now - _MODE_CACHE["ts"] < 5.0:
            return _MODE_CACHE["val"]
        val = "off"
        if os.path.exists(_MODE_FILE):
            with open(_MODE_FILE, "r", encoding="utf-8") as f:
                raw = (f.read() or "").strip().lower()
            if raw in ("full", "on", "1", "true"):
                val = "full"
        _MODE_CACHE["ts"] = now
        _MODE_CACHE["val"] = val
        return val
    except Exception:
        return "off"


def source_from_online_guid(guid: str) -> str:
    parts = (guid or "").split(":")
    return parts[1] if len(parts) >= 3 else ""


def build_online_track(item: dict) -> dict:
    """对齐飞牛前端 ZQ 解构 / _h() 期望：artists、album 对象、genres 数组、audioSpec、duration 毫秒。"""
    guid = online_guid_from_item(item)
    src = str(item.get("source") or source_from_online_guid(guid) or "")
    title = str(item.get("title") or item.get("name") or "")
    artist = str(item.get("artist") or "")
    album = str(item.get("album") or "")
    duration_s = item.get("duration_s") or 0
    try:
        duration_s = float(duration_s)
    except (TypeError, ValueError):
        duration_s = 0
    duration_ms = int(duration_s * 1000)
    ext = str(item.get("ext") or "mp3") or "mp3"
    play_format = play_format_from_ext(ext)
    file_size = item.get("file_size") or 0
    try:
        file_size = int(file_size or 0)
    except (TypeError, ValueError):
        file_size = 0
    cover = str(item.get("cover_url") or "")
    if not cover:
        # v55: 搜索结果已在外层解析过封面并写进 meta_cache —— 这里兜底回填，
        # 让 App 直接拿到 coverUrl，而不是等 /static/cover 现抓（3~4 秒必超时）。
        try:
            cover = str((_meta_get(guid) or {}).get("cover_url") or "")
        except Exception:
            cover = ""
    # 路径带真实后缀，飞牛 ll() 用 path 解析 extension；封面走 guid 以便 /static/cover 拦截
    spec_path = f"online/{src}/{guid}.{play_format}"

    artists_list = [{"name": artist, "guid": f"{guid}:artist"}] if artist else []
    album_obj = {
        "name": album,
        "guid": f"{guid}:album",
        "artists": artists_list,
        "coverId": guid,
    }
    audio_spec = {
        "path": spec_path,
        "format": play_format,
        "codec": play_format,
        "container": play_format,
        "duration": duration_ms,
        "size": file_size,
        "channel": 2,
        "sampleRate": 44100,
        "bitDepth": 16 if play_format in ("wav", "flac", "aiff") else None,
        "bitrate": 1411000 if play_format in ("flac", "wav", "ape", "wv") else 320000,
    }
    audio_spec = {k: v for k, v in audio_spec.items() if v is not None}

    return {
        "guid": guid,
        "id": guid,
        "title": title,
        "name": title,
        "artist": artist,
        "artists": artists_list,
        "album": album_obj,
        "albumName": album,
        "audioSpec": audio_spec,
        "duration": duration_ms,
        "duration_ms": duration_ms,
        "durationMs": duration_ms,
        "duration_s": duration_s,
        "codec": play_format,
        "codecName": play_format,
        "format": play_format,
        "ext": ext,
        "size": file_size,
        "file_size": file_size,
        "coverId": guid,
        "cover_url": cover,
        "coverUrl": cover,
        "coverURL": cover,
        "source": src,
        "is_online": True,
        "isFavorite": False,
        "isCue": False,
        "hasLyric": bool(item.get("lyric")),
        "genres": [],
        "accessStatus": 0,
    }


def artist_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    a = item.get("artist") or item.get("singer") or item.get("singers") or ""
    if isinstance(a, list):
        names = []
        for x in a:
            if isinstance(x, dict):
                names.append(str(x.get("name") or ""))
            else:
                names.append(str(x))
        return " ".join(n for n in names if n).strip().lower()
    if isinstance(a, dict):
        return str(a.get("name") or "").strip().lower()
    return str(a).strip().lower()


def title_from_track(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("title") or item.get("name") or "").strip().lower()


def should_cache(range_header: str | None) -> bool:
    """完整拉取才落盘：无 Range，或 bytes=0-（开区间）。Safari bytes=0-1 探测不落盘。"""
    if not range_header:
        return True
    r = range_header.strip().lower()
    return bool(re.match(r"^bytes=0-$", r))


def is_range_from_zero_or_none(range_header: str | None) -> bool:
    return should_cache(range_header)


def cache_safe_guid(guid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", guid)


def online_file_id(guid: str) -> str:
    """online:migu:600929… → 600929…，仅用于查找旧文件，不再写进文件名。"""
    return song_id_from_online_guid(guid).rsplit(":", 1)[-1]


def safe_basename_title(title: str) -> str:
    t = re.sub(r'[/\\:\0]', "_", (title or "").strip()) or "unknown"
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:120]


def library_basename(title: str, artist: str = "") -> str:
    """曲库文件名：歌手 - 歌名（无源站 id）。飞牛无标签时会用文件名当标题。"""
    title_s = safe_basename_title(title)
    artist_s = safe_basename_title(artist) if (artist or "").strip() else ""
    if artist_s and artist_s.lower() != title_s.lower() and artist_s != "unknown":
        return f"{artist_s} - {title_s}"
    return title_s


def media_ref_path(guid: str) -> str:
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


def _path_stem(path: str) -> str:
    root, ext = os.path.splitext(path)
    known = set(CACHE_EXTS) | {"lrc", "part"}
    if ext.lstrip(".").lower() in known:
        return root
    return path


def remember_media_path(guid: str, media_path: str) -> None:
    """记住曲库里的文件词干（不含扩展名），音频和 .lrc 共用。"""
    try:
        os.makedirs(CONF["cache_dir"], exist_ok=True)
        with open(media_ref_path(guid), "w", encoding="utf-8") as f:
            f.write(_path_stem(media_path))
    except Exception as e:
        logger.warning("Failed to remember media path for %s: %s", guid, e)


def recalled_media_stem(guid: str) -> str | None:
    ref = media_ref_path(guid)
    if not os.path.exists(ref):
        return None
    try:
        with open(ref, encoding="utf-8") as f:
            stem = _path_stem(f.read().strip())
        if stem:
            return stem
    except Exception:
        return None
    return None


def recalled_media_path(guid: str) -> str | None:
    stem = recalled_media_stem(guid)
    if not stem:
        return None
    for ext in CACHE_EXTS:
        path = f"{stem}.{ext}"
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return path
    return None


def unique_library_path(directory: str, basename: str, ext: str) -> str:
    dest = os.path.join(directory, f"{basename}.{ext}")
    if not os.path.exists(dest):
        return dest
    n = 2
    while os.path.exists(os.path.join(directory, f"{basename} ({n}).{ext}")):
        n += 1
    return os.path.join(directory, f"{basename} ({n}).{ext}")


def write_audio_tags(path: str, title: str, artist: str = "", album: str = "") -> None:
    """写入 title/artist/album，飞牛扫描后用标签而不是文件名显示。"""
    title, artist, album = (title or "").strip(), (artist or "").strip(), (album or "").strip()
    if not title and not artist:
        return
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio is None:
            return
        if getattr(audio, "tags", None) is None:
            try:
                audio.add_tags()
            except Exception:
                pass
        if title:
            audio["title"] = title
        if artist:
            audio["artist"] = artist
        if album:
            audio["album"] = album
        audio.save()
    except Exception as e:
        logger.warning("Failed to write audio tags for %s: %s", path, e)


_LIB_DIR_CACHE: dict = {"exp": 0.0, "val": "", "guid": ""}


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
                rows = con.execute("SELECT guid, path FROM shared_library ORDER BY id").fetchall()
            finally:
                con.close()
            for guid, path in rows:
                if path and os.path.isdir(path):
                    cached["val"] = path
                    cached["guid"] = str(guid or "")
                    cached["exp"] = now + 30.0
                    return path
        except Exception as e:
            logger.warning("Failed to read shared_library path: %s", e)
    cached["val"] = CONF["cache_dir"]
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


_TEE_SAVE_DIR_WARNED = False


def tee_save_dir() -> str:
    """边听边存落盘目录：配置路径可用则用，否则回退自动探测的曲库目录。"""
    explicit = str(CONF.get("tee_save_dir") or "").strip()
    if not explicit:
        return detect_library_dir()
    usable = False
    try:
        os.makedirs(explicit, exist_ok=True)
        usable = os.path.isdir(explicit) and os.access(explicit, os.W_OK)
    except Exception:
        usable = False
    if usable:
        return explicit
    global _TEE_SAVE_DIR_WARNED
    if not _TEE_SAVE_DIR_WARNED:
        _TEE_SAVE_DIR_WARNED = True
        logger.warning(
            "FNMUSIC_TEE_SAVE_DIR=%s 不可用（无法创建或不可写），边听边存回退到 %s",
            explicit, detect_library_dir(),
        )
    return detect_library_dir()


def iter_media_dirs() -> list[str]:
    dirs: list[str] = []
    explicit = str(CONF.get("tee_save_dir") or "").strip()
    for d in (([explicit] if explicit else []) + [detect_library_dir(), CONF["cache_dir"]]):
        if d and d not in dirs:
            dirs.append(d)
    return dirs


def adopt_library_perms(path: str) -> None:
    try:
        parent = os.path.dirname(path) or "."
        st = os.stat(parent)
        os.chown(path, st.st_uid, st.st_gid)
        os.chmod(path, 0o644)
    except Exception:
        pass


def find_cache_file(guid: str) -> str | None:
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        for ext in CACHE_EXTS:
            exact = os.path.join(d, f"{safe}.{ext}")
            if os.path.exists(exact) and os.path.getsize(exact) > 0:
                return exact
    return None


def promote_cache_hit(guid: str, audio_path: str) -> str:
    """旧 cache/ 音频：若曲库已有对应文件或歌词，则对齐过去。"""
    recalled = recalled_media_path(guid)
    if recalled:
        return recalled
    lib = detect_library_dir()
    try:
        if os.path.abspath(os.path.dirname(audio_path)) == os.path.abspath(lib):
            remember_media_path(guid, audio_path)
            return audio_path
    except Exception:
        return audio_path
    # Bare legacy IDs cannot prove source/track identity.
    return audio_path


def _is_rolling_cache_stem(path: str, guid: str) -> bool:
    """是否为 cache 目录下该 guid 的滚动缓存产物（cache_safe_guid 命名）。"""
    rolling = os.path.join(CONF["cache_dir"], cache_safe_guid(guid))
    try:
        return os.path.abspath(os.path.splitext(path)[0]) == os.path.abspath(rolling)
    except Exception:
        return False


def _same_dir(path: str, directory: str) -> bool:
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
    return unique_library_path(lib, library_basename(title, artist), ext)


def find_lyric_file(guid: str) -> str | None:
    v53_pinned = recalled_lyric_path(guid)
    if v53_pinned:
        return v53_pinned
    stem = recalled_media_stem(guid)
    if stem:
        sibling = f"{stem}.lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    audio = find_cache_file(guid)
    if audio:
        sibling = os.path.splitext(audio)[0] + ".lrc"
        if os.path.exists(sibling) and os.path.getsize(sibling) > 0:
            return sibling
    safe = cache_safe_guid(guid)
    for d in iter_media_dirs():
        if not os.path.isdir(d):
            continue
        exact = os.path.join(d, f"{safe}.lrc")
        if os.path.exists(exact) and os.path.getsize(exact) > 0:
            return exact
    return None


def lyric_cache_path(guid: str, title: str = "", artist: str = "") -> str:
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


def read_lyric_cache(guid: str) -> str:
    path = find_lyric_file(guid)
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        logger.warning("Failed to read lyric cache %s: %s", path, e)
        return ""


def write_lyric_cache(guid: str, text: str, title: str = "", artist: str = "") -> None:
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
            f.write("\n")
        os.replace(part_path, path)
        adopt_library_perms(path)
        remember_lyric_path(guid, path)
        _drop_shadow_lyric(guid, path)
        # 只有「歌词就是音频的同名 sidecar」时才动 media .ref，避免把音频映射挤掉
        _audio = materialized_library_file(guid) or find_cache_file(guid) or ""
        if _audio and os.path.splitext(_audio)[0] == os.path.splitext(path)[0]:
            remember_media_path(guid, path)
    except Exception as e:
        logger.warning("Failed to write lyric cache %s: %s", path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass


async def cache_lyrics_from_musicdl(musicdl_client: httpx.AsyncClient, guid: str) -> dict | None:
    """与音频 tee 并行：把 musicdl /info 里的 LRC 落到曲库同目录 sidecar。"""
    song_id = song_id_from_online_guid(guid)
    try:
        r = await musicdl_client.get("/info", params={"id": song_id}, timeout=10.0)
        if r.status_code != 200:
            return None
        data = r.json()
        if not (isinstance(data, dict) and data.get("ok") is not False):
            return None
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
        return data
    except Exception as e:
        logger.warning("lyric sidecar fetch failed for %s: %s", guid, e)
        return None


async def resolve_online_lyric(request: Request, guid: str) -> str:
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

    if not _source_enabled(guid):
        return ""
    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
            if r.status_code == 200:
                res_data = r.json()
                if isinstance(res_data, dict) and res_data.get("ok") is not False:
                    l_data = res_data.get("data")
                    if isinstance(l_data, dict):
                        lyric_text = str(l_data.get("lyric") or "").strip()
                        if lyric_text:
                            info = await _online_info(request, guid)
                            write_lyric_cache(
                                guid,
                                lyric_text,
                                title=str((info or {}).get("title") or ""),
                                artist=str((info or {}).get("artist") or ""),
                            )
                            return lyric_text
        except Exception as e:
            logger.warning("musicbox lyric fetch failed for %s: %s", guid, e)
        return ""

    data = await _online_info(request, guid)
    text = str((data or {}).get("lyric") or "").strip()
    if text:
        write_lyric_cache(
            guid,
            text,
            title=str((data or {}).get("title") or ""),
            artist=str((data or {}).get("artist") or ""),
        )
    return text


def media_type_for_ext(ext: str) -> str:
    return {
        "mp3": "audio/mpeg",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "ape": "audio/x-ape",
        "wv": "audio/x-wavpack",
        "dsf": "audio/x-dsd",
        "dff": "audio/x-dff",
        "tta": "audio/x-tta",
        "wma": "audio/x-ms-wma",
        "aiff": "audio/aiff",
    }.get(ext.lower(), "application/octet-stream")


def ext_from_content_type(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "flac" in ct:
        return "flac"
    if "wavpack" in ct or "x-wv" in ct:
        return "wv"
    if "wav" in ct or "wave" in ct:
        return "wav"
    if "opus" in ct:
        return "opus"
    if "ogg" in ct:
        return "ogg"
    if "ape" in ct:
        return "ape"
    if "aiff" in ct:
        return "aiff"
    if "mp4" in ct or "m4a" in ct:
        return "m4a"
    if "aac" in ct:
        return "aac"
    if "mpeg" in ct or "mp3" in ct:
        return "mp3"
    return play_format_from_ext(ct.split("/")[-1] if "/" in ct else "mp3")


def parse_http_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip(), re.I)
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        suffix = int(end_s)
        start = max(file_size - suffix, 0)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    end = min(end, file_size - 1)
    if start < 0 or start >= file_size or start > end:
        return None
    return start, end


def serve_file_with_range(path: str, range_header: str | None, media_type: str) -> Response:
    file_size = os.path.getsize(path)
    rng = parse_http_range(range_header, file_size)

    def iter_file(offset: int, length: int) -> AsyncGenerator[bytes, None]:
        async def gen() -> AsyncGenerator[bytes, None]:
            remaining = length
            with open(path, "rb") as fp:
                fp.seek(offset)
                while remaining > 0:
                    chunk = fp.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return gen()

    if rng is None:
        return StreamingResponse(
            iter_file(0, file_size),
            status_code=200,
            headers={
                "Content-Type": media_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )

    start, end = rng
    length = end - start + 1
    return StreamingResponse(
        iter_file(start, length),
        status_code=206,
        headers={
            "Content-Type": media_type,
            "Content-Length": str(length),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
        },
    )


def get_upstream_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "upstream_client", None)
    if client is None:
        transport = httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"])
        client = httpx.AsyncClient(transport=transport, base_url="http://unix", timeout=30.0)
        fastapi_app.state.upstream_client = client
    return client


def get_musicdl_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicdl_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicdl_url"], timeout=45.0)
        fastapi_app.state.musicdl_client = client
    return client


def get_musicbox_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "musicbox_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["musicbox_url"], timeout=20.0)
        fastapi_app.state.musicbox_client = client
    return client


def get_lx_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "lx_client", None)
    if client is None:
        client = httpx.AsyncClient(base_url=CONF["lx_url"], timeout=25.0)
        fastapi_app.state.lx_client = client
    return client


def get_llm_client(fastapi_app: FastAPI) -> httpx.AsyncClient:
    client = getattr(fastapi_app.state, "llm_client", None)
    if client is None:
        client = httpx.AsyncClient(timeout=dailyrec.LLM_TIMEOUT_S)
        fastapi_app.state.llm_client = client
    return client


async def forward_to_upstream(request: Request, client: httpx.AsyncClient) -> Response:
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"

    headers = copy_incoming_headers(request)
    body = await request.body()

    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    resp = await client.send(req, stream=True)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})

    async def body_stream() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=resp.status_code,
        headers=resp_headers,
    )


async def fetch_upstream_envelope(request: Request, client: httpx.AsyncClient) -> Response | dict:
    """透传上游并解析 JSON 信封。失败时返回 Response，成功返回 dict。"""
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)
    body = await request.body()
    req = client.build_request(
        method=request.method,
        url=url_path,
        headers=headers,
        content=body if body else None,
    )
    resp = await client.send(req)
    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    if resp.status_code != 200:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    try:
        payload = resp.json()
    except Exception:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    if not isinstance(payload, dict):
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    payload["_ext_headers"] = resp_headers
    return payload


async def fetch_musicdl_search(client: httpx.AsyncClient, keyword: str, limit: int, sources: str | None = None) -> dict | None:
    if not keyword:
        return None
    params: dict[str, Any] = {"keyword": keyword, "limit": limit}
    selected_sources = CONF["online_sources"] if sources is None else sources
    if selected_sources:
        params["sources"] = selected_sources
    timeout = max(float(CONF.get("search_timeout") or 25), 8.0)
    try:
        r = await client.get("/search", params=params, timeout=timeout)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                if data.get("errors"):
                    logger.warning("musicdl search partial errors: %s", data.get("errors"))
                raw_items = data.get("items")
                if isinstance(raw_items, list):
                    data["items"] = [it for it in raw_items if is_playable_online_track(it)]
                return data
    except Exception as e:
        logger.warning("Failed to fetch online search from musicdl: %s", e)
    return None


async def fetch_musicbox_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict] | None:
    if not keyword:
        return None
    try:
        r = await client.get(
            "/api/v1/search",
            params={"keyword": keyword, "limit": limit, "type": "song"},
            timeout=20.0,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        raw_list = data.get("data")
        if not isinstance(raw_list, list):
            return None
        items = []
        song_ids = []
        for it in raw_list:
            if not isinstance(it, dict):
                continue
            if not is_playable_online_track(it):
                continue
            sid = str(it.get("song_id") or it.get("id") or "")
            if not sid:
                continue
            title = str(it.get("song_name") or it.get("title") or it.get("name") or "")
            artist = str(it.get("artist") or "")
            album = str(it.get("album_name") or it.get("album") or "")
            duration = it.get("duration") or 0
            try:
                duration_s = float(duration)
            except (TypeError, ValueError):
                duration_s = 0.0
            quality = str(it.get("quality") or "").upper()
            ext = "flac" if any(q in quality for q in ("SQ", "HR", "无损")) else "mp3"
            items.append({
                "id": f"netease:{sid}",
                "source": "netease",
                "title": title,
                "version": str(it.get("version") or ""),
                "artist": artist,
                "album": album,
                "duration_s": duration_s,
                "ext": ext,
                "cover_url": "",
                "lyric": "",
            })
            song_ids.append(sid)

        if song_ids:
            try:
                detail_resp = await client.get(
                    "/api/v1/songs/detail",
                    params={"ids": ",".join(song_ids)},
                    timeout=15.0,
                )
                if detail_resp.status_code == 200:
                    detail_json = detail_resp.json()
                    if isinstance(detail_json, dict) and detail_json.get("ok") is not False:
                        detail_list = detail_json.get("data")
                        if isinstance(detail_list, list):
                            detail_map = {}
                            for d_item in detail_list:
                                if isinstance(d_item, dict):
                                    d_sid = str(d_item.get("song_id") or d_item.get("id") or "")
                                    if d_sid:
                                        detail_map[d_sid] = d_item
                            for item in items:
                                raw_sid = item["id"].split(":", 1)[-1]
                                d_info = detail_map.get(raw_sid)
                                if d_info:
                                    pic_url = str(d_info.get("album_pic_url") or "")
                                    if pic_url:
                                        item["cover_url"] = pic_url
                                    if d_info.get("has_sq") or d_info.get("has_hr"):
                                        item["ext"] = "flac"
            except Exception as detail_err:
                logger.warning("Failed to fetch songs detail for %s: %s", keyword, detail_err)

        return [it for it in items if is_playable_online_track(it)]
    except Exception as e:
        logger.warning("Failed to fetch musicbox search: %s", e)
        return None


class _SearchItems(list):
    """List-compatible normalized results with source degradation metadata."""
    def __init__(self, items, partial=False):
        super().__init__(items)
        self.partial = partial


async def fetch_lx_search(client: httpx.AsyncClient, keyword: str, limit: int) -> list[dict]:
    """洛雪音乐源搜索：返回统一 item（id = "lx:<source>:<identifier>"）。"""
    if not keyword:
        return None  # type: ignore[return-value]
    timeout = max(float(CONF.get("search_timeout") or 25), 8.0)
    try:
        r = await client.get(
            "/api/v1/search",
            params={"keyword": keyword, "limit": limit},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, dict) or data.get("ok") is False:
            return None
        raw_list = data.get("items")
        if not isinstance(raw_list, list):
            return None
        items = []
        for it in raw_list:
            if not isinstance(it, dict):
                continue
            if not is_playable_online_track(it):
                continue
            tid = str(it.get("id") or "")
            if not tid:
                continue
            duration = it.get("duration_s") or 0
            try:
                duration_s = float(duration)
            except (TypeError, ValueError):
                duration_s = 0.0
            try:
                file_size = int(it.get("file_size") or 0)
            except (TypeError, ValueError):
                file_size = 0
            items.append({
                "id": tid,
                "source": "lx",
                "lx_source": str(it.get("lx_source") or ""),
                "version": str(it.get("version") or ""),
                "title": str(it.get("title") or it.get("name") or ""),
                "artist": str(it.get("artist") or ""),
                "album": str(it.get("album") or ""),
                "duration_s": duration_s,
                "ext": str(it.get("ext") or "mp3") or "mp3",
                "cover_url": str(it.get("cover_url") or ""),
                "file_size": file_size,
                "lyric": "",
                "verified": it.get("verified") is True,
            })
        return _SearchItems([it for it in items if is_playable_online_track(it)], partial=bool(data.get("errors")))
    except Exception as e:
        logger.warning("Failed to fetch online search from lxmusic: %s", e)
        return None


async def resolve_lx_url(client: httpx.AsyncClient, song_id: str) -> "dict | None":
    """洛雪音乐源直链解析：song_id 形如 "lx:kg:<hash>"。"""
    qualities = []
    primary = str(CONF.get("lx_quality") or "lossless").strip()
    if primary:
        qualities.append(primary)
    for fallback in ("high", "standard"):
        if fallback not in qualities:
            qualities.append(fallback)

    for q in qualities:
        try:
            r = await client.get(
                "/api/v1/track/url",
                params={"id": song_id, "quality": q},
                timeout=15.0,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict) and inner.get("url"):
                        return inner
        except Exception as e:
            logger.warning("resolve_lx_url error for %s (quality=%s): %s", song_id, q, e)
    return None


# === v45: 网易云取流快通道 + musicbox 兜底 + 取流地址短 TTL 缓存/预热 ===
#
# 实测（2026-09-18 NAS 真机）：慢的不是网易云，是 musicbox ——
#   每次取流都要新起一个 musicbox 进程、走 weapi 加密接口，容器里 SQLite 缓存
#   表恒 0 行（responses 只缓存 GET，而 song url 是 POST）且持全局锁
#   ⇒ 单曲 1.2~94s；再被 track/stream 的 4s 硬上限打断 ⇒ 20 首里 15 首 404。
#   而官方 /api/song/enhance/player/url 明文接口（只需 MUSIC_U cookie）稳定 0.10~0.14s。
#
# 本版只做三件事，爆炸半径仅 resolve_netease_url()：
#   ① 取流走直连快通道（登录态从 musicbox 的 cookie.txt 借，不重复维护账号）；
#   ② /api/v1/song/{id}/url 原样保留为兜底（cookie 失效/风控/接口变更自动回落）；
#   ③ 取流地址短 TTL 缓存 + 列表返回时后台预热。
# 搜索/每日推荐/榜单/详情/歌词全部不动，仍走 musicbox。

_STREAM_URL_CACHE: dict = {}
_STREAM_URL_INFLIGHT: dict = {}
_STREAM_URL_MAX = 512
_STREAM_URL_SIG = None
_STREAM_URL_TTL = float(CONF.get("stream_url_ttl") or 600.0)
_STREAM_URL_STATS = {"direct": 0, "fallback": 0, "miss": 0, "warm": 0}

# 后台任务强引用池：asyncio.create_task() 不保留引用时，事件循环只持弱引用，
# 任务可能在执行途中被 GC 回收（插件里 _DAILY_TASKS 就是为此留引用的）。
_BG_TASKS: set = set()


def _spawn_bg(coro) -> None:
    _spawn_bg_task(coro)


def _spawn_bg_task(coro):
    """同上，但把 task 返回来（需要观察/去重的调用方用）。"""
    try:
        task = asyncio.create_task(coro)
    except Exception:
        return None
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


_NET_COOKIE_CACHE: dict = {"sig": None, "header": None}
_NET_DIRECT_CLIENT = None

# 音质 -> 官方接口 br 参数；账号无权限时该接口会自动降级返回可播地址
_NETEASE_DIRECT_BR = {
    "standard": 128000,
    "higher": 192000,
    "exhigh": 320000,
    "lossless": 999000,
    "hires": 1999000,
    "jymaster": 1999000,
}
_NETEASE_DIRECT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _netease_cookie_header():
    """读 musicbox 的 Netscape cookie.txt 取登录态（日志绝不打印 cookie 值）。

    按 (mtime, size) 缓存；用户重新扫码后文件变化 -> 自动失效重读。
    """
    path = str(CONF.get("netease_cookie_file") or "")
    if not path:
        return None
    try:
        st = os.stat(path)
        sig = (st.st_mtime, st.st_size)
    except OSError:
        return None
    if _NET_COOKIE_CACHE.get("sig") == sig and _NET_COOKIE_CACHE.get("header"):
        return _NET_COOKIE_CACHE["header"]
    pairs = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(chr(9))
                if len(parts) < 7:
                    continue
                name, value = parts[5].strip(), parts[6].strip()
                if name and value:
                    pairs.append("%s=%s" % (name, value))
    except OSError:
        return None
    _NET_COOKIE_CACHE["sig"] = sig
    if not any(p.startswith("MUSIC_U=") for p in pairs):
        _NET_COOKIE_CACHE["header"] = None
        return None
    _NET_COOKIE_CACHE["header"] = "; ".join(pairs)
    return _NET_COOKIE_CACHE["header"]


def _netease_direct_client():
    global _NET_DIRECT_CLIENT
    if _NET_DIRECT_CLIENT is None or getattr(_NET_DIRECT_CLIENT, "is_closed", True):
        _NET_DIRECT_CLIENT = httpx.AsyncClient(timeout=3.0, follow_redirects=False)
    return _NET_DIRECT_CLIENT


async def _netease_direct_url(song_id: str, quality: str):
    """官方接口直取播放地址；异常/空地址/试听片段一律返回 None（交 musicbox 兜底）。"""
    if not CONF.get("netease_direct"):
        return None
    song_id = str(song_id or "").strip()
    if not song_id.isdigit():
        return None
    cookie = _netease_cookie_header()
    if not cookie:
        return None
    br = _NETEASE_DIRECT_BR.get(str(quality or "").strip().lower(), 999000)
    try:
        r = await _netease_direct_client().post(
            "https://music.163.com/api/song/enhance/player/url",
            data={"ids": "[%s]" % song_id, "br": str(br)},
            headers={"Referer": "https://music.163.com/", "User-Agent": _NETEASE_DIRECT_UA,
                     "Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
        )
        payload = r.json()
    except Exception as e:
        logger.debug("netease direct failed for %s: %s", song_id, type(e).__name__)
        return None
    if not isinstance(payload, dict) or payload.get("code") != 200:
        return None
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    if not isinstance(row, dict) or row.get("code") != 200:
        return None
    if row.get("freeTrialInfo"):
        # 非空 = 只有 30 秒试听片段。直连绝不能把试听当正片返回，
        # 宁可回落较慢的 musicbox（其 weapi 路径按账号权限给结果）。
        logger.info("netease direct %s 命中试听片段，回落 musicbox", song_id)
        return None
    url = str(row.get("url") or "")
    if not url:
        return None
    logger.debug("netease direct ok %s level=%s br=%s", song_id, row.get("level"), row.get("br"))
    return url


def _stream_url_sync_cookie():
    global _STREAM_URL_SIG
    sig = _NET_COOKIE_CACHE.get("sig")
    if sig != _STREAM_URL_SIG:
        _STREAM_URL_CACHE.clear()
        _STREAM_URL_SIG = sig


def _stream_url_cache_get(song_id: str):
    _stream_url_sync_cookie()
    key = str(song_id or "")
    hit = _STREAM_URL_CACHE.get(key)
    if not hit:
        return None
    exp, url = hit
    if exp <= time.monotonic() or not url:
        _STREAM_URL_CACHE.pop(key, None)
        return None
    return str(url)


def _stream_url_cache_put(song_id: str, url: str) -> None:
    try:
        if len(_STREAM_URL_CACHE) >= _STREAM_URL_MAX:
            now = time.monotonic()
            for k in [k for k, v in list(_STREAM_URL_CACHE.items()) if v[0] <= now]:
                _STREAM_URL_CACHE.pop(k, None)
            if len(_STREAM_URL_CACHE) >= _STREAM_URL_MAX:
                _STREAM_URL_CACHE.clear()
        _STREAM_URL_CACHE[str(song_id or "")] = (time.monotonic() + _STREAM_URL_TTL, str(url))
    except Exception:
        pass


async def _resolve_netease_direct(song_id: str, qualities):
    for q in qualities:
        url = await _netease_direct_url(song_id, q)
        if url:
            _stream_url_cache_put(song_id, url)
            return url
    return None


async def _resolve_netease_direct_dedup(song_id: str, qualities):
    """同曲并发去重：列表预热与用户首播撞车时只发一次请求。"""
    task = _STREAM_URL_INFLIGHT.get(song_id)
    if task is not None and not task.done():
        try:
            return await asyncio.shield(task)
        except Exception:
            return None
    task = asyncio.create_task(_resolve_netease_direct(song_id, qualities))
    _STREAM_URL_INFLIGHT[song_id] = task
    try:
        return await task
    finally:
        if _STREAM_URL_INFLIGHT.get(song_id) is task:
            _STREAM_URL_INFLIGHT.pop(song_id, None)


async def _prefetch_stream_urls(request, items, limit: int = 20, concurrency: int = 3) -> None:
    """列表返回时后台预热网易云取流地址（直连 0.1s 级，整张歌单约 1 秒）。

    只预热 online:netease:*；只走直连快通道 —— 预热绝不能触发慢的 musicbox
    兜底，否则一次列表刷新就会打出 20 个 4~30s 的请求，反而把音乐源打满。
    """
    try:
        targets = []
        for it in (items or []):
            g = ""
            if isinstance(it, dict):
                g = str(it.get("guid") or "")
                if not g.startswith("online:") and isinstance(it.get("track"), dict):
                    g = str(it["track"].get("guid") or "")
            if not g.startswith("online:netease:") or g in targets:
                continue
            if _stream_url_cache_get(song_id_from_online_guid(g).split(":")[-1]) is not None:
                continue
            targets.append(g)
            if len(targets) >= limit:
                break
        if not targets:
            return
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _warm(g):
            async with sem:
                try:
                    sid = song_id_from_online_guid(g).split(":")[-1]
                    hit = await asyncio.wait_for(
                        _resolve_netease_direct_dedup(sid, _netease_qualities()), timeout=5.0)
                    if hit:
                        _STREAM_URL_STATS["warm"] += 1
                except Exception:
                    pass

        for g in targets:
            _spawn_bg(_warm(g))
    except Exception:
        pass


def _netease_direct_stats() -> dict:
    # 主动读一次 cookie.txt（os.stat + 内存缓存，成本可忽略），
    # 否则 cookie_ok 会因懒加载恒为 false，误导排障。
    try:
        _netease_cookie_header()
    except Exception:
        pass
    return {
        "enabled": bool(CONF.get("netease_direct")),
        "cookie_ok": bool(_NET_COOKIE_CACHE.get("header")),
        "cookie_file": str(CONF.get("netease_cookie_file") or ""),
        "cached": len(_STREAM_URL_CACHE),
        "inflight": len(_STREAM_URL_INFLIGHT),
        "ttl_s": _STREAM_URL_TTL,
        **_STREAM_URL_STATS,
    }


def _netease_qualities():
    """按配置给出取流音质尝试顺序（主音质 + exhigh 兜底）。"""
    qualities = []
    primary = str(CONF.get("netease_quality") or "lossless").strip()
    if primary:
        qualities.append(primary)
    if "exhigh" not in qualities:
        qualities.append("exhigh")
    return qualities


async def resolve_netease_url(client: httpx.AsyncClient, song_id: str):
    song_id = str(song_id or "").strip()
    cached = _stream_url_cache_get(song_id)
    if cached:
        return cached
    qualities = _netease_qualities()

    # (1) 直连快通道（0.10~0.14s）；任何失败都不抛错，静默落到 (2)
    if CONF.get("netease_direct"):
        hit = await _resolve_netease_direct_dedup(song_id, qualities)
        if hit:
            _STREAM_URL_STATS["direct"] += 1
            return hit
        _STREAM_URL_STATS["miss"] += 1
        logger.info("netease direct miss %s -> musicbox 兜底", song_id)

    # (2) musicbox 兜底（原逻辑保持不变）
    _STREAM_URL_STATS["fallback"] += 1
    for q in qualities:
        try:
            r = await client.get(f"/api/v1/song/{song_id}/url", params={"quality": q}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        code = inner.get("code")
                        url = inner.get("url")
                        if code == 200 and url:
                            _stream_url_cache_put(song_id, str(url))
                            return str(url)
        except Exception as e:
            logger.warning("resolve_netease_url error for %s (quality=%s): %s", song_id, q, e)
    return None



def ensure_search_list(upstream_json: dict) -> list:
    """保证 data.list 存在，本地 0 条时仍能追加在线条目。"""
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {}
        upstream_json["data"] = data
    target = get_by_path(upstream_json, CONF["search_list_path"])
    if isinstance(target, list):
        return target
    for key in ("list", "items", "tracks", "records"):
        if isinstance(data.get(key), list):
            if key != "list":
                data["list"] = data[key]
            return data["list"]
    data["list"] = []
    if "total" not in data:
        data["total"] = 0
    return data["list"]


def merge_online_tracks(
    upstream_json: dict,
    online_data: list[dict] | dict | None,
    page: int = 1,
    size: int = 50,
    selected: bool = False,
) -> dict:
    target_list = ensure_search_list(upstream_json)
    if not online_data:
        return upstream_json

    if isinstance(online_data, dict):
        raw_items = online_data.get("items", [])
    elif isinstance(online_data, list):
        raw_items = online_data
    else:
        raw_items = []

    if not raw_items:
        return upstream_json

    existing_keys = set()
    for item in target_list:
        t = title_from_track(item)
        a = artist_from_track(item)
        if t and a:
            existing_keys.add((t, a))

    online_limit = CONF["online_limit"]
    if not selected:
        start = 0 if page == 1 else online_limit + (page - 2) * size
        raw_page = raw_items[start:start + (online_limit if page == 1 else size)]
    else:
        raw_page = raw_items
    filtered_online = []
    for online_item in raw_page:
        if not is_playable_online_track(online_item, require_id=True):
            continue
        ot = str(online_item.get("title") or online_item.get("name") or "").strip().lower()
        oa = str(online_item.get("artist") or "").strip().lower()
        if ot and oa and (ot, oa) in existing_keys:
            continue
        filtered_online.append(online_item)

    page_online = filtered_online

    for it in page_online:
        target_list.append(build_online_track(it))

    parts = CONF["search_list_path"].split(".")
    parent = upstream_json
    for p in parts[:-1]:
        if isinstance(parent, dict) and p in parent:
            parent = parent[p]
    if isinstance(parent, dict):
        orig_total = parent.get("total")
        if not isinstance(orig_total, int):
            orig_total = len(target_list) - len(page_online)
        parent["total"] = orig_total + sum(1 for item in raw_items if is_playable_online_track(item, require_id=True) and (title_from_track(item), artist_from_track(item)) not in existing_keys)

    return upstream_json


def extract_guid(request: Request, path_guid: str | None = None) -> str:
    if path_guid:
        return path_guid
    return (
        request.query_params.get("guid")
        or request.query_params.get("trackGUID")
        or request.query_params.get("trackGuid")
        or request.query_params.get("coverId")
        or request.query_params.get("id")
        or request.query_params.get("trackId")
        or ""
    )


async def extract_guid_from_body(request: Request) -> str:
    guid = extract_guid(request)
    if guid:
        return guid
    try:
        body = await request.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        return str(
            body.get("guid")
            or body.get("trackGUID")
            or body.get("trackGuid")
            or body.get("id")
            or body.get("trackId")
            or ""
        )
    return ""


def empty_ok() -> JSONResponse:
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {}})


def build_lyric_list_payload(guid: str, lyric_text: str) -> dict:
    """对齐飞牛 $n.lyric.list → xr(list, preferred)。

    每条需有非空 content；source=2 表示 EXTERNAL_LRC（非内嵌，不强制 offset）。
    """
    text = (lyric_text or "").strip()
    if not text:
        return {"code": 0, "msg": "ok", "data": {"list": [], "preferred": ""}}
    lyric_guid = f"{guid}:lyric"
    now = int(time.time())
    item = {
        "guid": lyric_guid,
        "content": text,
        "source": 2,
        "isLRC": True,
        "offset": 0,
        "createdAt": now,
        "updatedAt": now,
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {"list": [item], "preferred": lyric_guid},
    }


def stub_online_info(guid: str) -> dict:
    song_id = song_id_from_online_guid(guid)
    return {
        "id": song_id,
        "source": source_from_online_guid(guid),
        "title": "",
        "artist": "",
        "album": "",
        "duration_s": 0,
        "ext": "mp3",
        "file_size": 0,
        "cover_url": "",
        "lyric": "",
    }


def build_metadata_payload(guid: str, data: dict | None) -> dict:
    """飞牛 resolveTrackPlayback._h() 会无防护读取 data.track.genres.join / album / artists。

    缺 genres 或 album 不是对象时直接抛错，播放器跳过且不会请求 stream。
    """
    info = dict(data or {})
    info.setdefault("id", song_id_from_online_guid(guid))
    info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(info)
    album_obj = vo["album"] if isinstance(vo.get("album"), dict) else {
        "name": str(vo.get("album") or ""),
        "guid": f"{guid}:album",
        "artists": vo.get("artists") or [],
        "coverId": guid,
    }
    track = {
        "guid": guid,
        "id": guid,
        "title": vo.get("title") or "",
        "artists": vo.get("artists") or [],
        "album": album_obj,
        "genres": list(vo.get("genres") or []),
        "duration": vo.get("duration") or 0,
        "coverId": guid,
        "coverUrl": vo.get("coverUrl") or "",
        "format": vo.get("format") or "mp3",
        "hasLyric": bool(vo.get("hasLyric") or info.get("lyric")),
        "isFavorite": False,
        "isCue": False,
        "accessStatus": 0,
        "audioSpec": vo["audioSpec"],
    }
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            **vo,
            "guid": guid,
            "id": guid,
            "album": album_obj,
            "audioSpec": vo["audioSpec"],
            "track": track,
        },
    }


def _conf_log_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(part in lowered for part in _REDACT_KEY_PARTS):
        return "***" if value else ""
    return value


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    logger.info("=== fnmusic-ext v%s configuration ===", get_version())
    for k, v in CONF.items():
        logger.info("  %s = %s", k, _conf_log_value(k, v))
    logger.info("  llm_enabled = %s", dailyrec.llm_enabled())
    logger.info("==================================")

    created_upstream = False
    created_musicdl = False
    created_musicbox = False
    created_lx = False
    created_llm = False

    if getattr(fastapi_app.state, "upstream_client", None) is None:
        fastapi_app.state.upstream_client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=CONF["upstream_sock"]),
            base_url="http://unix",
            timeout=30.0,
        )
        created_upstream = True

    if getattr(fastapi_app.state, "musicdl_client", None) is None:
        fastapi_app.state.musicdl_client = httpx.AsyncClient(
            base_url=CONF["musicdl_url"],
            timeout=45.0,
        )
        created_musicdl = True

    if getattr(fastapi_app.state, "musicbox_client", None) is None:
        fastapi_app.state.musicbox_client = httpx.AsyncClient(
            base_url=CONF["musicbox_url"],
            timeout=20.0,
        )
        created_musicbox = True

    if getattr(fastapi_app.state, "lx_client", None) is None:
        fastapi_app.state.lx_client = httpx.AsyncClient(
            base_url=CONF["lx_url"],
            timeout=25.0,
        )
        created_lx = True

    if getattr(fastapi_app.state, "llm_client", None) is None:
        fastapi_app.state.llm_client = httpx.AsyncClient(timeout=dailyrec.LLM_TIMEOUT_S)
        created_llm = True

    # v53：启动后清一次孤儿歌词，之后周期复扫
    if CONF.get("lyric_orphan_gc"):
        _spawn_bg_task(_lyric_orphan_loop())

    # v54：启动后把「音频已在曲库、歌词却留在 cache/」的歌词补成曲库同名 sidecar
    if CONF.get("lyric_promote"):
        _spawn_bg_task(_lyric_promote_loop())

    try:
        yield
    finally:
        tasks = [entry["task"] for entry in _SEARCH_CACHE.values() if entry.get("task") and not entry["task"].done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if created_upstream and getattr(fastapi_app.state, "upstream_client", None):
            await fastapi_app.state.upstream_client.aclose()
            fastapi_app.state.upstream_client = None
        if created_musicdl and getattr(fastapi_app.state, "musicdl_client", None):
            await fastapi_app.state.musicdl_client.aclose()
            fastapi_app.state.musicdl_client = None
        if created_musicbox and getattr(fastapi_app.state, "musicbox_client", None):
            await fastapi_app.state.musicbox_client.aclose()
            fastapi_app.state.musicbox_client = None
        if created_lx and getattr(fastapi_app.state, "lx_client", None):
            await fastapi_app.state.lx_client.aclose()
            fastapi_app.state.lx_client = None
        if created_llm and getattr(fastapi_app.state, "llm_client", None):
            await fastapi_app.state.llm_client.aclose()
            fastapi_app.state.llm_client = None


app = FastAPI(title="fnmusic-ext", lifespan=lifespan)


import threading  # noqa: E402  (诊断探针用)

_PROBE_LOG_PATH = os.path.join(_HOME, "access_probe.log")
_PROBE_LOCK = threading.Lock()
_PROBE_MAX = 12 * 1024 * 1024


def _probe_write(line: str) -> None:
    try:
        with _PROBE_LOCK:
            if os.path.exists(_PROBE_LOG_PATH) and os.path.getsize(_PROBE_LOG_PATH) > _PROBE_MAX:
                with open(_PROBE_LOG_PATH, "w", encoding="utf-8") as f:
                    f.write("")
            with open(_PROBE_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


@app.middleware("http")
async def _access_probe(request: Request, call_next):
    """诊断用：把每次请求的方法/路径/状态码/Content-Type 落到 access_probe.log。

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
        response = await call_next(request)
        status = response.status_code
        ct = str(response.headers.get("content-type") or "-").split(";")[0]
        try:
            blen = int(response.headers.get("content-length") or -1)
        except (TypeError, ValueError):
            blen = -1
        return response
    except Exception as e:
        ct = "ERR:" + type(e).__name__
        raise
    finally:
        try:
            if not skip:
                p = request.url.path
                q = request.url.query
                cost = int((time.time() - t0) * 1000)
                _probe_write("%s %s%s | %s | %sms | %s | len=%s" % (
                    request.method, p, ("?" + q) if q else "", status, cost, ct, blen))
        except Exception:
            pass


@app.get("/_ext/livez")
async def ext_livez():
    return {"ok": True, "service": "fnmusic-ext", "pid": os.getpid()}


@app.get("/_ext/healthz")
async def ext_healthz(request: Request):
    async def probe(name: str, client: httpx.AsyncClient, path: str) -> dict:
        try:
            response = await asyncio.wait_for(client.get(path, timeout=2.0), timeout=2.4)
            healthy = response.status_code < 500 if name == "upstream" else response.status_code == 200
            detail: dict = {"status": "ok" if healthy else "fail", "http_status": response.status_code}
            if name != "upstream" and healthy:
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        detail["dependency"] = payload
                        if payload.get("ok") is False:
                            detail["status"] = "fail"
                    else:
                        detail["status"] = "fail"
                except ValueError:
                    detail["status"] = "fail"
                    detail["error"] = "invalid health JSON"
            return detail
        except Exception as exc:
            return {"status": "fail", "error": type(exc).__name__}

    checks = [("upstream", True, get_upstream_client, "/music/api/v1/search/track?keyword=healthz_probe"),
              ("musicdl", CONF.get("musicdl_enabled", True), get_musicdl_client, "/healthz"),
              ("musicbox", CONF.get("netease_enabled", True), get_musicbox_client, "/healthz"),
              ("lxmusic", CONF.get("lx_enabled", True), get_lx_client, "/healthz")]
    enabled = [(name, getter, path) for name, on, getter, path in checks if on]
    results = await asyncio.gather(*(probe(name, getter(request.app), path) for name, getter, path in enabled))
    details = {name: {"status": "disabled"} for name, on, _, _ in checks if not on}
    details.update({name: result for (name, _, _), result in zip(enabled, results)})
    statuses = {name: value["status"] for name, value in details.items()}
    failed = [name for name, status in statuses.items() if status == "fail"]
    source_ok = any(statuses[name] == "ok" for name in ("musicdl", "musicbox", "lxmusic"))
    return {"ok": statuses["upstream"] == "ok" and source_ok, "version": get_version(),
            **statuses, "llm": "enabled" if dailyrec.llm_enabled() else "disabled",
            "netease_direct": _netease_direct_stats(),
            "recommend": {
                "mode": "source-native",
                "netease": bool(CONF.get("netease_enabled", True)),
                "lx": bool(CONF.get("lx_enabled", True)),
                "llm_fallback": dailyrec.llm_enabled() and not CONF.get("netease_enabled", True),
                "recent": dailyrec.last_recommend_summary(),
            },
            "degraded": bool(failed), "failures": failed, "details": details}


# === v55: 搜索结果「有海报 + 高音质」优先 ===
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


@app.get("/music/api/v1/search/track")
@app.get("/music/api/v1/search/track/{subpath:path}")
async def search_track(request: Request):
    upstream_client = get_upstream_client(request.app)
    musicdl_client = get_musicdl_client(request.app)
    musicbox_client = get_musicbox_client(request.app)
    keyword = extract_keyword(request)

    page_str = request.query_params.get("page")
    try:
        page = int(page_str) if page_str else 1
    except (TypeError, ValueError):
        page = 1
    if page < 1:
        page = 1

    size_str = request.query_params.get("size")
    try:
        size = int(size_str) if size_str else 50
    except (TypeError, ValueError):
        size = 50
    if size < 1:
        size = 50

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)

    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    if not keyword:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    key = _search_scope(request) + ":" + keyword
    _clean_search_cache()
    entry = _SEARCH_CACHE.get(key)
    if entry is None:
        entry = {"items": [], "pages": {}, "cursor": 0, "ts": 0, "keyword": keyword,
                 "scope": _search_scope(request), "credentials": _credential_scope(request), "config": _source_config(), "task": None}
        _set_search_cache(key, entry)
    entry["accessed"] = time.time()
    task = entry.get("task")
    if (not task or task.done()) and time.time() - entry["ts"] >= _search_ttl(entry):
        task = asyncio.create_task(_aggregate_search(request, keyword, entry))
        entry["task"] = task
    if task and not task.done():
        if page == 1:
            await asyncio.wait({task}, timeout=float(CONF["netease_wait_s"]))
            # Empty/error completions do not exhaust the remaining wait budget.
            if not entry["items"]:
                deadline = asyncio.get_running_loop().time() + float(CONF["late_page_wait_s"])
                while not entry["items"] and not task.done() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.wait({task}, timeout=min(0.02, max(0, deadline - asyncio.get_running_loop().time())))
        else:
            await asyncio.wait({task}, timeout=float(CONF["late_page_wait_s"]))
    if _search_rank_enabled() and task is not None and not task.done() and entry["items"] and not entry["pages"]:
        # v55: 首页再多等一小会儿，让「补全封面/音质 → 重排」在页码分配之前完成。
        # 页码一旦分配就按 guid 固定，之后重排也改不了首屏顺序。
        # 实测聚合只比 netease_wait_s 晚几十毫秒结束，这次等待通常瞬间返回。
        await asyncio.wait({task}, timeout=float(CONF["search_rank_wait_s"]))
    local_list = ensure_search_list(upstream_json)
    local_keys = {(title_from_track(x), artist_from_track(x)) for x in local_list}
    total_online = sum(1 for x in entry["items"] if (title_from_track(x), artist_from_track(x)) not in local_keys)
    original_total = upstream_json.get("data", {}).get("total", len(local_list))
    if _search_rank_enabled():
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
        pass
    if isinstance(original_total, int):
        merged["data"]["total"] = original_total + total_online
    return JSONResponse(content=merged, status_code=upstream_resp.status_code, headers=resp_headers)


async def _aggregate_search(request: Request, keyword: str, entry: dict) -> None:
    sources = []
    if CONF.get("netease_enabled", True):
        sources.append(fetch_musicbox_search(get_musicbox_client(request.app), keyword, CONF["netease_search_limit"]))
    if CONF.get("musicdl_enabled", True):
        sources.append(fetch_musicdl_search(get_musicdl_client(request.app), keyword, CONF["online_limit"]))
    if CONF.get("lx_enabled", True):
        sources.append(fetch_lx_search(get_lx_client(request.app), keyword, CONF["lx_search_limit"]))
    tasks = [asyncio.create_task(coro) for coro in sources]
    pending = set(tasks)
    partial = False
    results: dict[asyncio.Task, list] = {}
    enrich_task: asyncio.Task | None = None
    try:
        deadline = asyncio.get_running_loop().time() + max(1.0, float(CONF["search_timeout"]))
        while pending:
            done, pending = await asyncio.wait(pending, timeout=max(0, deadline - asyncio.get_running_loop().time()), return_when=asyncio.FIRST_COMPLETED)
            if not done:
                partial = True
                break
            for task in tasks:
                if task not in done:
                    continue
                try:
                    data = task.result()
                except Exception:
                    data = None
                partial |= data is None or bool(getattr(data, "partial", False)) or (isinstance(data, dict) and bool(data.get("errors") or data.get("ok") is False))
                items = data.get("items", []) if isinstance(data, dict) else (data or [])
                results[task] = items
                if not entry["pages"]:
                    ordered = [item for source_task in tasks for item in results.get(source_task, [])]
                else:
                    ordered = entry["items"] + items
                entry["items"] = deduplicate_online_items(ordered)[:2000]
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
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _resync_published_pages(entry: dict) -> int:
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


def _session_page(entry: dict, page: int, size: int) -> list[dict]:
    pages = entry["pages"]
    count = int(CONF["online_limit"]) if page == 1 else size
    if page not in pages:
        if len(pages) >= 2000:
            return []
        pages[page] = []
    # Only the trailing page can grow; no published prefix ever moves. This
    # also lets a repeated empty first page recover after its negative TTL.
    if page == max(pages):
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
        entry["cursor"] = len(taken)
    by_guid = {online_guid_from_item(item): item for item in entry["items"]}
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
    return [by_guid[guid] for guid in pages[page] if guid in by_guid and _source_enabled(guid)]


@app.get("/music/api/v1/search/suggest")
@app.get("/music/api/v1/search/suggest/{subpath:path}")
async def search_suggest(request: Request):
    if not CONF["merge_suggest"]:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    musicdl_client = get_musicdl_client(request.app)
    keyword = extract_keyword(request)

    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    musicdl_task: asyncio.Task | None = None
    if keyword and CONF.get("musicdl_enabled", True):
        musicdl_task = asyncio.create_task(fetch_musicdl_search(musicdl_client, keyword, 5))

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        if musicdl_task:
            musicdl_task.cancel()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        if musicdl_task:
            musicdl_task.cancel()
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        if musicdl_task:
            musicdl_task.cancel()
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    musicdl_data = None
    if musicdl_task:
        try:
            musicdl_data = await asyncio.wait_for(asyncio.shield(musicdl_task), timeout=10.0)
        except Exception as e:
            logger.warning("Suggest musicdl error: %s", e)
            musicdl_task.cancel()

    data_field = upstream_json.get("data")
    if isinstance(data_field, list) and musicdl_data and "items" in musicdl_data:
        for item in musicdl_data.get("items", [])[:5]:
            title = item.get("title")
            if title and title not in data_field:
                data_field.append(title)

    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


def stream_tee_response(
    resp: httpx.Response,
    guid: str,
    range_header: str | None,
    coro_factory: Callable[[], Coroutine[Any, Any, Any]] | None = None,
    client_to_close: httpx.AsyncClient | None = None,
    resolved_ext: str | None = None,
    pre_info: dict | None = None,
    chunks: Any = None,
    first_chunk: bytes = b"",
    favorites_only: bool = False,
) -> Response:
    headers = {"Accept-Ranges": "bytes"}
    for key in ("content-type", "content-length", "content-range"):
        if resp.headers.get(key):
            headers[key] = resp.headers[key]
    if resolved_ext:
        headers["content-type"] = media_type_for_ext(resolved_ext)
    length = resp.headers.get("content-length", "")
    expected = int(length) if length.isdigit() else None
    full_resource = resp.status_code == 200
    if resp.status_code == 206:
        # Content-Length proves only this segment, not the entire recording.
        match = re.fullmatch(r"bytes\s+0-(\d+)/(\d+)", resp.headers.get("content-range", "").strip(), re.I)
        full_resource = bool(match and int(match[1]) + 1 == int(match[2]) and int(match[2]) > 0)
        if full_resource:
            total = int(match[2])
            full_resource = expected is None or expected == total
            expected = total
    ext = resolved_ext or ext_from_content_type(resp.headers.get("content-type", ""))

    async def body() -> AsyncGenerator[bytes, None]:
        # Pull-through provides backpressure: no unbounded producer queue and no
        # downloader outliving its consumer. Only a clean EOF may finalize.
        part = None
        fp = None
        written = 0
        eof = False
        info_task = None
        saved = False
        _tee_t0 = time.time()
        try:
            tee_enabled = bool(CONF.get("tee_save_enabled"))
            # v50: 「写曲库」与「写滚动缓存」拆开判定。
            # favorites_only 时非收藏只进本地 cache/，不碰 rclone 云盘挂载的曲库。
            _library_ok = tee_enabled and not (favorites_only and not is_favorite_online_guid(guid))
            _cache_ok = should_cache(range_header)
            if _cache_ok and full_resource:
                directory = tee_save_dir() if _library_ok else CONF["cache_dir"]
                os.makedirs(directory, exist_ok=True)
                part = os.path.join(directory, f"{cache_safe_guid(guid)}.{uuid4().hex}.part")
                fp = open(part, "wb")
                _probe_write("[tee] start guid=%s range=%s status=%s full=%s tee=%s lib=%s fav=%s exp=%s dir=%s" % (
                    guid, range_header, resp.status_code, full_resource, tee_enabled, _library_ok,
                    is_favorite_online_guid(guid), expected, directory))
                if pre_info is None and coro_factory:
                    info_task = asyncio.create_task(coro_factory())
            else:
                _probe_write("[tee] nocache guid=%s range=%s cache_ok=%s status=%s full=%s tee=%s exp=%s ct=%s" % (
                    guid, range_header, _cache_ok, resp.status_code, full_resource, tee_enabled,
                    expected, resp.headers.get("content-range")))
            iterator = chunks if chunks is not None else resp.aiter_bytes()
            if first_chunk:
                if fp:
                    fp.write(first_chunk)
                written += len(first_chunk)
                yield first_chunk
            async for chunk in iterator:
                if chunk:
                    if fp:
                        fp.write(chunk)
                    written += len(chunk)
                    yield chunk
            eof = True
            if fp:
                fp.close()
                fp = None
            info = pre_info
            if info is None and info_task:
                try:
                    info = await asyncio.wait_for(info_task, timeout=8.0)
                except Exception:
                    info = None
            _ok = bool(part and eof and written >= 1024 and (expected is None or written == expected))
            _probe_write("[tee] judge guid=%s written=%d exp=%s eof=%s part=%s ok=%s t=%.2fs" % (
                guid, written, expected, eof, bool(part), _ok, time.time() - _tee_t0))
            if _ok:
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
                    dest = library_media_path(guid, title, ext, artist=artist, directory=tee_save_dir())
                    os.replace(part, dest)
                    saved = True
                    remember_media_path(guid, dest)
                    adopt_library_perms(dest)
                    write_audio_tags(dest, title, artist, album)
                    request_library_scan("tee")
                    _probe_write("[tee] saved guid=%s dest=%s title=%r artist=%r ext=%s bytes=%d t=%.2fs" % (
                        guid, dest, title, artist, ext, written, time.time() - _tee_t0))
                else:
                    # 非收藏（或关闭边听边存）：只写本地滚动缓存（cache_safe_guid 命名，精确名可命中）
                    dest = os.path.join(CONF["cache_dir"], f"{cache_safe_guid(guid)}.{ext}")
                    os.replace(part, dest)
                    saved = True
                    _probe_write("[tee] rolling guid=%s dest=%s bytes=%d t=%.2fs" % (
                        guid, dest, written, time.time() - _tee_t0))
                lyric = str((info or {}).get("lyric") or (info or {}).get("lrc") or "")
                # v50: 音频没落地就不写 .lrc —— 55 个孤儿歌词全部来自这里
                if saved and lyric.strip():
                    write_lyric_cache(guid, lyric, title, artist)
                if not _library_ok:
                    try:
                        purge_rolling(CONF["cache_dir"], keep=int(CONF.get("tee_cache_max", 2)))
                    except Exception as e:
                        logger.warning("Rolling cache purge failed: %s", e)
        finally:
            try:
                _probe_write("[tee] finally guid=%s saved=%s written=%d exp=%s eof=%s t=%.2fs" % (
                    guid, saved, written, expected, eof, time.time() - _tee_t0))
            except Exception:
                pass
            if fp:
                fp.close()
            if part and os.path.exists(part):
                os.remove(part)
            with anyio.CancelScope(shield=True):
                if info_task and not info_task.done():
                    info_task.cancel()
                    await asyncio.gather(info_task, return_exceptions=True)
                await resp.aclose()
                if client_to_close:
                    await client_to_close.aclose()

    return StreamingResponse(body(), status_code=resp.status_code, headers=headers)


def _credential_scope(request: Request) -> str:
    return hashlib.sha256(json.dumps([request.headers.get(k, "") for k in
        ("cookie", "authorization", "x-trim-music-temp-token")]).encode()).hexdigest()


def _retained_track(request: Request, guid: str) -> tuple[dict | None, dict | None]:
    _clean_search_cache()
    for entry in reversed(list(_SEARCH_CACHE.values())):
        if entry.get("credentials") != _credential_scope(request) or entry.get("config", _source_config()) != _source_config():
            continue
        for item in entry.get("items", []):
            if online_guid_from_item(item) == guid:
                return item, entry
            for alternative in item.get("_alternatives", []):
                if online_guid_from_item(alternative) == guid:
                    return alternative, entry
    return None, None


async def _recover_source(request: Request, guid: str, entry: dict | None) -> bool:
    """One bounded source re-search after a backend loses its in-memory IDs."""
    if not entry or not _source_enabled(guid):
        return False
    source = source_from_online_guid(guid)
    recovery_key = "recovered:" + source
    if time.monotonic() - entry.get(recovery_key, -1000) < 30:
        return False
    entry[recovery_key] = time.monotonic()
    keyword = entry.get("keyword", "")
    if source == "netease":
        coro = fetch_musicbox_search(get_musicbox_client(request.app), keyword, CONF["netease_search_limit"])
    elif source == "lx":
        coro = fetch_lx_search(get_lx_client(request.app), keyword, CONF["lx_search_limit"])
    else:
        selected = [name.strip() for name in str(CONF.get("online_sources") or "").split(",")
                    if name.strip().lower().removesuffix("musicclient") == source.lower()]
        coro = fetch_musicdl_search(get_musicdl_client(request.app), keyword, CONF["online_limit"],
                                   ",".join(selected) or source)
    try:
        result = await asyncio.wait_for(coro, timeout=3.0)
        items = result.get("items", []) if isinstance(result, dict) else (result or [])
        return any(online_guid_from_item(item) == guid for item in items)
    except Exception:
        return False


async def _open_online_stream(request: Request, guid: str, range_header: str | None):
    """Resolve and read first bytes before committing HTTP headers to the client."""
    source = source_from_online_guid(guid)
    info, _ = _retained_track(request, guid)
    headers = {"Accept-Encoding": "identity"}
    if range_header:
        headers["Range"] = range_header
    owned = None
    resp = None
    ext = None
    try:
        if source in ("netease", "lx"):
            if source == "netease":
                url = await resolve_netease_url(get_musicbox_client(request.app), song_id_from_online_guid(guid).split(":")[-1])
                if not url:
                    return None
            else:
                resolved = await resolve_lx_url(get_lx_client(request.app), song_id_from_online_guid(guid))
                if not resolved:
                    return None
                url = resolved["url"]
                ext = resolved.get("ext")
                for key, value in (resolved.get("headers") or {}).items():
                    if key.lower() in ("referer", "user-agent"):
                        headers[key] = str(value)
            if info is None:
                try:
                    info = await asyncio.wait_for(_fetch_online_info(request, guid), timeout=0.75)
                except Exception:
                    pass
            ext = ext or (info or {}).get("ext")
            owned = httpx.AsyncClient(timeout=10.0, follow_redirects=True)
            client = owned
            req = client.build_request("GET", url, headers=headers)
        else:
            client = get_musicdl_client(request.app)
            req = client.build_request("GET", "/stream", params={"id": song_id_from_online_guid(guid), "proxy": "true"}, headers=headers)
        resp = await client.send(req, stream=True)
        content_type = resp.headers.get("content-type", "").lower()
        if (resp.status_code not in (200, 206)
                or any(x in content_type for x in ("text/", "json"))
                or resp.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity")):
            # Reject servers ignoring identity: decoded bytes cannot use encoded
            # Content-Length/Range offsets, and must not enter the audio cache.
            return None
        chunks = resp.aiter_bytes()
        first = await anext(chunks, b"")
        if not first:
            return None
        result = (resp, owned, ext, info, chunks, first)
        resp = owned = None  # transfer ownership to response iterator
        return result
    finally:
        if resp:
            await resp.aclose()
        if owned:
            await owned.aclose()


@app.get("/music/api/v1/track/stream")
@app.get("/music/api/v1/track/stream/{subpath:path}")
async def stream_track(request: Request):
    guid = extract_guid(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    range_header = request.headers.get("range")
    cached = find_cache_file(guid)
    if cached:
        ext = os.path.splitext(cached)[1].lstrip(".") or "mp3"
        return serve_file_with_range(cached, range_header, media_type_for_ext(ext))
    # v50 播放补漏：收藏曲目在「播放起点」触发一次后台整轨落盘
    # （真实播放器按 1MB 定长窗口取流，tee 尾部落盘永远轮不到）
    if CONF.get("fav_dl_on_play") and range_starts_at_zero(range_header) and is_favorite_online_guid(guid):
        spawn_favorite_download(request, guid)
    item, entry = _retained_track(request, guid)
    candidates = [guid]
    # Byte offsets are encoding-specific: do not cross sources on seek/probe.
    if should_cache(range_header) and item and request.query_params.get("_ext_rendition") != "1":
        candidates += [online_guid_from_item(x) for x in item.get("_alternatives", []) if _same_recording(item, x)]
    deadline = asyncio.get_running_loop().time() + 12.0
    for candidate in list(dict.fromkeys(candidates))[:3]:
        if not _source_enabled(candidate):
            continue
        for attempt in range(2):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                opened = await asyncio.wait_for(_open_online_stream(request, candidate, range_header), timeout=min(4.0, remaining))
            except Exception as exc:
                logger.warning("Stream startup failed for %s: %s", candidate, type(exc).__name__)
                opened = None
            if opened:
                resp, owned, ext, info, chunks, first = opened
                if candidate != guid:
                    # Publish the selected source identity before any audio.
                    # The client owns B's URL for later Range requests even if
                    # this proxy's search session expires; never alias B as A.
                    with anyio.CancelScope(shield=True):
                        await resp.aclose()
                        if owned:
                            await owned.aclose()
                    target = request.url.include_query_params(guid=candidate, _ext_rendition="1")
                    return RedirectResponse(str(target), status_code=307, headers={"Cache-Control": "no-store"})
                # Cache the selected source's bytes under its own GUID, never
                # splice a failed stream or alias different encodings for seeks.
                return stream_tee_response(resp, candidate, range_header,
                    coro_factory=lambda: _online_info(request, candidate), client_to_close=owned,
                    resolved_ext=ext, pre_info=info, chunks=chunks, first_chunk=first,
                    favorites_only=bool(CONF.get("tee_favorites_only")))
            if attempt or deadline - asyncio.get_running_loop().time() <= 3:
                break
            if not await _recover_source(request, candidate, entry):
                break
    return JSONResponse(content={"code": 404, "msg": "online source unavailable", "data": None}, status_code=404)


@app.get("/music/api/v1/track/hls/{guid}/preset.m3u8")
@app.get("/music/api/v1/track/hls/{guid}/{filename}")
async def track_hls(request: Request, guid: str, filename: str = "preset.m3u8"):
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    info = await _online_info(request, guid)
    duration_s = 0
    if info:
        try:
            duration_s = int(float(info.get("duration_s") or 0))
        except (TypeError, ValueError):
            duration_s = 0
    if duration_s <= 0:
        duration_s = 240

    stream_url = f"/music/api/v1/track/stream?guid={quote(guid, safe='')}"
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{max(duration_s, 1)}\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXTINF:{duration_s:.3f},\n"
        f"{stream_url}\n"
        "#EXT-X-ENDLIST\n"
    )
    return Response(content=playlist, media_type="application/vnd.apple.mpegurl")


@app.api_route("/music/api/v1/track/transcode/heartbeat", methods=["GET", "POST"])
@app.api_route("/music/api/v1/track/transcode/quit", methods=["GET", "POST"])
async def track_transcode_session(request: Request):
    guid = await extract_guid_from_body(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"guid": guid}})


@app.api_route("/music/api/v1/track/transcode", methods=["GET", "POST"])
async def track_transcode(request: Request):
    guid = await extract_guid_from_body(request)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))
    return JSONResponse(
        content={
            "code": 0,
            "msg": "ok",
            "status": "success",
            "data": {"guid": guid, "status": "ready"},
        }
    )


async def _online_info(request: Request, guid: str) -> dict | None:
    """v42: 命中短时缓存直接返回，避免同一次操作里反复问洛雪容器（每次约 4 秒）。

    只缓存元数据（title/artist/album/封面/歌词/时长）—— 播放地址由
    resolve_netease_url / resolve_lx_url 单独解析，不经过这里，因此不会拿到过期 token。
    """
    key = _online_info_key(request, guid)
    hit = _online_info_cache_get(request, guid)
    if hit is not None:
        return hit
    task = _ONLINE_INFO_INFLIGHT.get(key)
    if task is not None:
        try:
            got = await asyncio.shield(task)
            return dict(got) if got else got
        except Exception:
            pass
    task = asyncio.ensure_future(_online_info_raw(request, guid))
    _ONLINE_INFO_INFLIGHT[key] = task
    try:
        data = await task
    except Exception:
        data = None
    finally:
        _ONLINE_INFO_INFLIGHT.pop(key, None)
    if data:
        _online_info_cache_put(request, guid, data)
    return dict(data) if data else data


async def _online_info_raw(request: Request, guid: str) -> dict | None:
    retained, entry = _retained_track(request, guid)
    if not _source_enabled(guid):
        return retained
    try:
        data = await asyncio.wait_for(_fetch_online_info(request, guid), timeout=4.0)
        if data:
            return data
        if await _recover_source(request, guid, entry):
            data = await asyncio.wait_for(_fetch_online_info(request, guid), timeout=3.0)
            if data:
                return data
    except Exception:
        pass
    return retained


async def _fetch_online_info(request: Request, guid: str) -> dict | None:
    src = source_from_online_guid(guid)
    if src == "netease":
        musicbox_client = get_musicbox_client(request.app)
        raw_song_id = song_id_from_online_guid(guid)
        song_id = raw_song_id.split(":")[-1]
        try:
            r = await musicbox_client.get(f"/api/v1/song/{song_id}/info", timeout=10.0)
            if r.status_code == 200:
                res_data = r.json()
                if isinstance(res_data, dict) and res_data.get("ok") is not False:
                    data = res_data.get("data")
                    if isinstance(data, dict):
                        name = str(data.get("name") or "")
                        ar = data.get("ar") or []
                        ar_names = []
                        if isinstance(ar, list):
                            for x in ar:
                                if isinstance(x, dict) and x.get("name"):
                                    ar_names.append(str(x["name"]))
                                elif isinstance(x, str):
                                    ar_names.append(x)
                        artist = " / ".join(ar_names)
                        al = data.get("al") or {}
                        album_name = str(al.get("name") or "") if isinstance(al, dict) else ""
                        cover_url = str(al.get("picUrl") or "") if isinstance(al, dict) else ""
                        dt = data.get("dt") or 0
                        duration_s = float(dt) / 1000.0 if dt else 0.0
                        sq = data.get("sq")
                        hr = data.get("hr")
                        h = data.get("h") or {}
                        ext = "flac" if (sq or hr) else "mp3"
                        size_obj = sq or h or {}
                        file_size = int(size_obj.get("size", 0) or 0) if isinstance(size_obj, dict) else 0

                        lyric_text = ""
                        try:
                            lr = await musicbox_client.get(f"/api/v1/song/{song_id}/lyric", timeout=10.0)
                            if lr.status_code == 200:
                                l_res = lr.json()
                                if isinstance(l_res, dict) and l_res.get("ok") is not False:
                                    l_data = l_res.get("data")
                                    if isinstance(l_data, dict):
                                        lyric_text = str(l_data.get("lyric") or "").strip()
                        except Exception as l_err:
                            logger.warning("musicbox lyric fetch in _online_info failed for %s: %s", guid, l_err)

                        return {
                            "id": f"netease:{song_id}",
                            "source": "netease",
                            "title": name,
                            "artist": artist,
                            "album": album_name,
                            "cover_url": cover_url,
                            "duration_s": duration_s,
                            "ext": ext,
                            "file_size": file_size,
                            "lyric": lyric_text,
                        }
        except Exception as e:
            logger.warning("musicbox /info failed for %s: %s", guid, e)
        return None

    if src == "lx":
        lx_client = get_lx_client(request.app)
        song_id = song_id_from_online_guid(guid)
        try:
            r = await lx_client.get("/api/v1/track/info", params={"id": song_id}, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and data.get("ok") is not False:
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        lyric_text = ""
                        if not inner.get("lyric"):
                            try:
                                lr = await lx_client.get(
                                    "/api/v1/track/lyric", params={"id": song_id}, timeout=10.0
                                )
                                if lr.status_code == 200:
                                    l_res = lr.json()
                                    if isinstance(l_res, dict) and l_res.get("ok") is not False:
                                        lyric_text = str((l_res.get("data") or {}).get("lyric") or "").strip()
                            except Exception as l_err:
                                logger.warning("lxmusic lyric fetch failed for %s: %s", guid, l_err)
                        else:
                            lyric_text = str(inner.get("lyric") or "").strip()
                        return {
                            "id": song_id,
                            "source": "lx",
                            "lx_source": str(inner.get("lx_source") or ""),
                            "title": str(inner.get("title") or ""),
                            "artist": str(inner.get("artist") or ""),
                            "album": str(inner.get("album") or ""),
                            "cover_url": str(inner.get("cover_url") or ""),
                            "duration_s": float(inner.get("duration_s") or 0),
                            "ext": str(inner.get("ext") or "mp3") or "mp3",
                            "file_size": int(inner.get("file_size") or 0),
                            "lyric": lyric_text,
                        }
        except Exception as e:
            logger.warning("lxmusic /info failed for %s: %s", guid, e)
        return None

    musicdl_client = get_musicdl_client(request.app)
    song_id = song_id_from_online_guid(guid)
    try:
        r = await musicdl_client.get("/info", params={"id": song_id}, timeout=10.0)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and data.get("ok") is not False:
                return data
    except Exception as e:
        logger.warning("musicdl /info failed for %s: %s", guid, e)
    return None


@app.get("/music/api/v1/lyric/list")
@app.get("/music/api/v1/lyric/list/{subpath:path}")
async def lyric_list(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    return JSONResponse(content=build_lyric_list_payload(guid, lyric_text))


@app.get("/music/api/v1/track/lyrics")
@app.get("/music/api/v1/track/lyrics/{subpath:path}")
@app.get("/music/api/v1/detail/lyrics/{subpath:path}")
async def track_lyrics(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    lyric_text = await resolve_online_lyric(request, guid)
    if lyric_text:
        res = {"code": 0, "msg": "ok", "data": {"guid": guid, "lyric": lyric_text}}
        set_by_path(res, CONF["lyric_field"], lyric_text)
        return JSONResponse(content=res)
    return empty_ok()


@app.get("/music/api/v1/track/metadata")
@app.get("/music/api/v1/track/metadata/{subpath:path}")
@app.get("/music/api/v1/track/audio-info")
async def track_metadata(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    data = await _online_info(request, guid) or stub_online_info(guid)
    cached_lyric = read_lyric_cache(guid)
    if cached_lyric:
        data = {**data, "lyric": cached_lyric}
    elif data.get("lyric"):
        write_lyric_cache(
            guid,
            str(data.get("lyric") or ""),
            title=str(data.get("title") or ""),
            artist=str(data.get("artist") or ""),
        )
    return JSONResponse(content=build_metadata_payload(guid, data))



# === v30: 在线曲目元数据持久化 + 封面占位（修复手机端收藏/历史整列消失） ===

_META_CACHE_PATH = os.path.join(_HOME, "meta_cache.json")
_META_CACHE: dict = {}
_META_CACHE_LOADED = False
_PLACEHOLDER_PNG: bytes | None = None


def _meta_load() -> dict:
    global _META_CACHE, _META_CACHE_LOADED
    if _META_CACHE_LOADED:
        return _META_CACHE
    _META_CACHE_LOADED = True
    try:
        if os.path.exists(_META_CACHE_PATH):
            with open(_META_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _META_CACHE = data
    except Exception as e:
        logger.debug("meta cache load failed: %s", e)
    return _META_CACHE


def _meta_get(guid: str) -> dict:
    try:
        return dict((_meta_load() or {}).get(str(guid or "")) or {})
    except Exception:
        return {}


def _meta_set(guid: str, info: dict) -> None:
    try:
        m = _meta_load()
        cur = m.get(str(guid or "")) or {}
        for k, v in (info or {}).items():
            if v not in (None, "", 0):
                cur[k] = v
        m[str(guid or "")] = cur
        tmp = _META_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False)
        os.replace(tmp, _META_CACHE_PATH)
    except Exception as e:
        logger.debug("meta cache save failed: %s", e)


_IMG_BYTES_CACHE: dict = {}
_COVER_DIR = os.path.join(_HOME, "cover_cache")


def _cover_disk_key(url: str) -> str:
    import hashlib
    return hashlib.sha1(str(url or "").encode("utf-8")).hexdigest()[:32]


def _cover_bucket(size) -> int:
    """把客户端要的 size 归并到固定档位，既尊重大图需求又不会每个像素值存一份。"""
    try:
        s = int(float(size))
    except (TypeError, ValueError):
        s = 300
    for b in (120, 160, 300, 600, 800, 1200):
        if s <= b:
            return b
    return 1200


_DAILY_POSTER_MEMO: dict = {}


def _daily_poster_guid(user_guid: str = "") -> str:
    """每日推荐歌单海报 = 该歌单第一首曲目的封面。

    优先取当前用户当天的推荐缓存；取不到（典型情况：图片请求不带会话 cookie，
    探测不出 user_guid）就退回磁盘上任意用户最新的当天缓存 ——
    海报绝不能因为鉴权缺失而变成灰块。
    """
    day = ""
    try:
        day = dailyrec.today_key()
        cached = dailyrec.load_daily_cache(user_guid, day)
        tracks = (cached or {}).get("tracks") or []
        if tracks:
            g = str(tracks[0].get("guid") or "")
            if is_online_guid(g):
                return g
    except Exception:
        pass
    try:
        memo = _DAILY_POSTER_MEMO.get(day)
        if memo and time.time() - float(memo[0]) < 30:
            return str(memo[1] or "")
        import glob as _glob
        best, best_mt = "", -1.0
        for p in _glob.glob(os.path.join(_HOME, "recommend_cache", "*", str(day or "*") + ".json")):
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt > best_mt:
                best_mt, best = mt, p
        out = ""
        if best:
            with open(best, "r", encoding="utf-8") as f:
                data = json.load(f)
            tracks = (data or {}).get("tracks") or []
            if tracks:
                cand = str(tracks[0].get("guid") or "")
                if is_online_guid(cand):
                    out = cand
        _DAILY_POSTER_MEMO[day] = (time.time(), out)
        return out
    except Exception:
        return ""


def _cover_guid_path(guid: str, size=None):
    g = re.sub(r"[^a-zA-Z0-9_.:-]", "_", str(guid or ""))[:120]
    if not g:
        return None
    b = _cover_bucket(size) if size is not None else 300
    # 300 档沿用旧文件名，兼容磁盘上已有的缓存
    if b == 300:
        return os.path.join(_COVER_DIR, g + ".img")
    return os.path.join(_COVER_DIR, "%s@%d.img" % (g, b))


def _cover_by_guid_get(guid: str, size=None, fallback: bool = False):
    """按 guid + 尺寸档直取封面（命中即毫秒级返回，无需再问 lx/网易云）。

    fallback=True 时目标档位没有缓存就退而求其次返回任意已缓存档位：
    宁可给一张尺寸不对的图，也不要让手机端因为空图而整列不渲染。
    """
    try:
        order = [_cover_bucket(size) if size is not None else 300]
        if fallback:
            for cand in (1200, 800, 600, 300, 160, 120):
                if cand not in order:
                    order.append(cand)
        for cand in order:
            p = _cover_guid_path(guid, cand)
            if not (p and os.path.exists(p) and os.path.getsize(p) > 100):
                continue
            # 带过期时间的缓存（占位图 6 小时后失效，避免真实封面恢复后被永久盖住）
            if os.path.exists(p + ".exp"):
                try:
                    with open(p + ".exp", "r", encoding="utf-8") as f:
                        if time.time() > float((f.read() or "0").strip() or 0):
                            continue
                except Exception:
                    pass
            ct = "image/jpeg"
            if os.path.exists(p + ".ct"):
                with open(p + ".ct", "r", encoding="utf-8") as f:
                    ct = (f.read() or "image/jpeg").strip() or "image/jpeg"
            with open(p, "rb") as f:
                return f.read(), ct
    except Exception:
        pass
    return None


def _cover_by_guid_put(guid: str, body: bytes, ct: str, ttl: int = 0, size=None) -> None:
    try:
        os.makedirs(_COVER_DIR, exist_ok=True)
        p = _cover_guid_path(guid, size)
        if not p:
            return
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, p)
        with open(p + ".ct", "w", encoding="utf-8") as f:
            f.write(ct or "image/jpeg")
        if ttl > 0:
            with open(p + ".exp", "w", encoding="utf-8") as f:
                f.write(str(int(time.time()) + int(ttl)))
        elif os.path.exists(p + ".exp"):
            os.remove(p + ".exp")
    except Exception:
        pass


def _cover_disk_get(url: str):
    try:
        p = os.path.join(_COVER_DIR, _cover_disk_key(url) + ".img")
        meta = p + ".ct"
        if os.path.exists(p) and os.path.getsize(p) > 100:
            ct = "image/jpeg"
            if os.path.exists(meta):
                with open(meta, "r", encoding="utf-8") as f:
                    ct = (f.read() or "image/jpeg").strip() or "image/jpeg"
            with open(p, "rb") as f:
                return f.read(), ct
    except Exception:
        pass
    return None


def _cover_disk_put(url: str, body: bytes, ct: str) -> None:
    try:
        os.makedirs(_COVER_DIR, exist_ok=True)
        p = os.path.join(_COVER_DIR, _cover_disk_key(url) + ".img")
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, p)
        with open(p + ".ct", "w", encoding="utf-8") as f:
            f.write(ct or "image/jpeg")
    except Exception:
        pass


_COVER_PREFETCH: set = set()


async def _warm_cover(guid: str, size: int) -> None:
    try:
        cover = _normalize_cover_url(str(_meta_get(guid).get("cover_url") or ""))
        if not cover:
            ne_id = _netease_song_id(guid)
            if ne_id:
                ne = await _netease_detail(ne_id)
                cover = str((ne or {}).get("cover_url") or "")
                if cover:
                    _meta_set(guid, {"cover_url": cover})
        if cover:
            got = await _fetch_image_bytes(_sized_cover_url(cover, size))
            if got:
                _cover_by_guid_put(guid, got[0], got[1], size=size)
    except Exception:
        pass
    finally:
        try:
            _COVER_PREFETCH.discard("%s@%d" % (guid, _cover_bucket(size)))
        except Exception:
            pass


def _prefetch_online_covers(items, size: int = 300) -> None:
    """后台预热在线曲目封面。

    手机端列表对封面加载超时很敏感：在线封面若等到客户端来拉时才现抓（3~4 秒/张），
    整列会因超时而渲染失败。因此在返回列表时就提前把封面抓到本地磁盘缓存。
    """
    try:
        for it in (items or [])[:40]:
            g = ""
            if isinstance(it, dict):
                g = str(it.get("guid") or "")
                if not g.startswith("online:") and isinstance(it.get("track"), dict):
                    g = str(it["track"].get("guid") or "")
            key = "%s@%d" % (g, _cover_bucket(size))
            if not g.startswith("online:") or key in _COVER_PREFETCH:
                continue
            _COVER_PREFETCH.add(key)
            asyncio.create_task(_warm_cover(g, size))
    except Exception:
        pass


def _sized_cover_url(url: str, size: int) -> str:
    """按客户端要的 size 取对应尺寸的网易云缩略图（避免动辄 200KB~7MB 的大图）。"""
    u = _normalize_cover_url(url)
    if not u:
        return ""
    try:
        s = int(size)
    except (TypeError, ValueError):
        s = 300
    s = max(60, min(1000, s))
    if "music.126.net" in u:
        base = u.split("?")[0]
        return base + "?param=%dy%d" % (s, s)
    return u


async def _fetch_image_bytes(url: str):
    """抓封面字节（先内存、再磁盘、最后网络），返回 (bytes, content_type)。"""
    try:
        if url in _IMG_BYTES_CACHE:
            return _IMG_BYTES_CACHE[url]
    except Exception:
        pass
    hit = _cover_disk_get(url)
    if hit:
        _IMG_BYTES_CACHE[url] = hit
        return hit
    got = await _fetch_image_network(url)
    if got:
        try:
            _IMG_BYTES_CACHE[url] = got
        except Exception:
            pass
        _cover_disk_put(url, got[0], got[1])
    return got


_IMG_CLIENT = None


def _img_client():
    """共享的长连接客户端：每张图都新建连接要做 DNS+TCP+TLS，实测一张 2.8KB 小图要 4 秒。"""
    global _IMG_CLIENT
    try:
        if _IMG_CLIENT is None:
            _IMG_CLIENT = httpx.AsyncClient(
                timeout=httpx.Timeout(8.0, connect=6.0),
                follow_redirects=True,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
            )
        return _IMG_CLIENT
    except Exception:
        return None


async def _fetch_image_network(url: str):
    """服务端代理抓取封面字节，返回 (bytes, content_type)；失败返回 None。

    手机端对 302 外链的处理不稳定，因此一律由本服务抓图后以 200 返回真实图片。
    """
    try:
        ref = "https://www.kuwo.cn/" if "kuwo" in url else "https://music.163.com/"
        headers = {"Referer": ref,
                   "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        client = _img_client()
        if client is None:
            return None
        r = await client.get(url, headers=headers)
        ct = (r.headers.get("content-type") or "").split(";")[0].strip()
        if r.status_code == 200 and r.content and ct.startswith("image/") and len(r.content) > 100:
            _IMG_BYTES_CACHE[url] = (r.content, ct)
            return _IMG_BYTES_CACHE[url]
    except Exception as e:
        logger.debug("cover fetch failed %s: %s", url, e)
    return None


def _placeholder_png_bytes() -> bytes:
    """最小可用纯色 PNG（无外部依赖）——手机端列表封面取不到时的兜底图。"""
    global _PLACEHOLDER_PNG
    if _PLACEHOLDER_PNG:
        return _PLACEHOLDER_PNG
    import zlib, struct
    w = h = 96
    rgb = (74, 86, 100)
    row = b"\x00" + bytes(rgb) * w
    raw = row * h

    def chunk(tag, data):
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    _PLACEHOLDER_PNG = png
    return png


def _is_dead_online_guid(guid: str) -> bool:
    """酷狗(kg) 解析链路已失效：元数据恒空且不可播，不写入收藏/历史。"""
    return str(guid or "").startswith("online:lx:kg:")


async def _kw_title_via_musicinfo(rid: str):
    """酷我单曲详情：name/artist/album/duration(秒)/pic(封面)。"""
    url = "https://wapi.kuwo.cn/api/www/music/musicInfo?mid=%s" % rid
    headers = {"Referer": "https://www.kuwo.cn/",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=headers, timeout=10.0)
        data = (r.json() or {}).get("data") or {}
    name = data.get("name") or data.get("musicName") or data.get("title") or ""
    artist = data.get("artist") or ""
    album = data.get("album") or ""
    try:
        duration_s = float(data.get("duration") or 0)
    except (TypeError, ValueError):
        duration_s = 0.0
    cover_url = str(data.get("pic") or "")
    return (str(name).strip(), str(artist).strip(), str(album).strip(), duration_s, cover_url)


_NE_DETAIL_CACHE: dict = {}


def _normalize_cover_url(url: str) -> str:
    """网易云原图可达数 MB，手机端列表会卡顿 —— 统一换成 300x300 缩略图。"""
    u = str(url or "")
    if not u:
        return ""
    if "music.126.net" in u and "param=" not in u:
        u = u + ("&param=300y300" if "?" in u else "?param=300y300")
    return u


def _netease_song_id(guid: str) -> str:
    """从 online:lx:wy:<id> / online:netease:<id> 取出纯数字歌曲 ID。"""
    g = str(guid or "")
    for pref in ("online:lx:wy:", "online:netease:"):
        if g.startswith(pref):
            sid = g[len(pref):].split(":")[0].strip()
            return sid if sid.isdigit() else ""
    return ""


async def _netease_detail(song_id: str) -> dict:
    """网易云官方详情：title/artist/album/duration_s/cover_url（lx 源拿不到封面时的兜底）。"""
    if not song_id:
        return {}
    if song_id in _NE_DETAIL_CACHE:
        return _NE_DETAIL_CACHE[song_id]
    out = {}
    try:
        url = "https://music.163.com/api/v3/song/detail?c=" + quote('[{"id":%s}]' % song_id)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                   "Referer": "https://music.163.com/"}
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url, headers=headers)
        songs = ((r.json() or {}).get("songs") or [])
        if songs:
            s = songs[0] or {}
            out["title"] = str(s.get("name") or "")
            ar = s.get("ar") or []
            out["artist"] = "、".join(str(a.get("name") or "") for a in ar if a.get("name"))
            al = s.get("al") or {}
            out["album"] = str(al.get("name") or "")
            pic = str(al.get("picUrl") or "")
            if pic:
                # 原图可达数 MB，手机端列表会卡顿 —— 统一要 300x300 缩略图
                pic = pic + ("&param=300y300" if "?" in pic else "?param=300y300")
            out["cover_url"] = pic
            try:
                dt = float(s.get("dt") or s.get("duration") or 0)
            except (TypeError, ValueError):
                dt = 0.0
            out["duration_s"] = (dt / 1000.0) if dt > 3600 else dt
    except Exception as e:
        logger.debug("netease detail failed %s: %s", song_id, e)
    _NE_DETAIL_CACHE[song_id] = out
    return out


def _norm_duration_s(item: dict) -> float:
    """把各来源混乱的时长字段统一成秒。

    lx 源对 kg 会把 duration(秒) 当成毫秒，导致 duration_s=0.309 这类错误值。
    """
    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0
    ds = f((item or {}).get("duration_s"))
    if 1 <= ds <= 3600:
        return ds
    d = f((item or {}).get("duration"))
    dm = f((item or {}).get("duration_ms") or (item or {}).get("durationMs"))
    if 1 <= d <= 3600:
        return d
    if dm > 3600:
        return dm / 1000.0
    if 0 < ds < 1 and 1 <= d <= 3600:
        return d
    return 0.0


async def _resolve_record_meta(request, guid: str) -> dict:
    """录制在线曲目时的完整元数据（title/artist/album/duration_s/cover_url）。

    v42: 洛雪 / 网易云 / 酷我三个后端由「串行」改为「并发」，最坏耗时 ~6s → ~4s；
    再叠加 _online_info 的短时缓存，同一曲目后续调用基本 0 秒。
    """
    # 缓存条目只有在“有标题 且 有时长/封面”时才算完整；否则继续补全（自愈旧条目）
    cached = _meta_get(guid)
    if cached.get("title") and (cached.get("duration_s") or 0) > 0 and cached.get("cover_url"):
        return dict(cached)
    out = {"title": str(cached.get("title") or ""),
           "artist": str(cached.get("artist") or ""),
           "album": str(cached.get("album") or ""),
           "duration_s": cached.get("duration_s") or 0,
           "cover_url": str(cached.get("cover_url") or ""),
           "source": source_from_online_guid(guid)}

    def _dur_missing():
        try:
            d = float(out.get("duration_s") or 0)
        except (TypeError, ValueError):
            d = 0.0
        return (not d) or 0 < d < 1

    # 2) 搜索缓存里保留的完整曲目对象（同步、零成本，先取出来一起参与合并）
    try:
        ritem, _rentry = _retained_track(request, guid)
    except Exception:
        ritem = None

    ne_id = _netease_song_id(guid)
    kw_rid = str(guid)[len("online:lx:kw:"):] if str(guid).startswith("online:lx:kw:") else ""

    async def _t_info():
        try:
            return await asyncio.wait_for(_online_info(request, guid), timeout=5.0)
        except Exception:
            return None

    async def _t_ne():
        try:
            return await _netease_detail(ne_id)
        except Exception:
            return None

    async def _t_kw():
        try:
            return await _kw_title_via_musicinfo(kw_rid)
        except Exception:
            return None

    coros = [_t_info()]
    if ne_id and (not out.get("cover_url") or _dur_missing()):
        coros.append(_t_ne())
    if kw_rid:
        coros.append(_t_kw())
    try:
        results = await asyncio.gather(*coros, return_exceptions=True)
    except Exception:
        results = [None]
    info = results[0] if results and not isinstance(results[0], BaseException) else None
    idx = 1
    ne = {}
    if ne_id and (not out.get("cover_url") or _dur_missing()):
        if len(results) > idx and not isinstance(results[idx], BaseException):
            ne = results[idx] or {}
        idx += 1
    kwv = None
    if kw_rid and len(results) > idx and not isinstance(results[idx], BaseException):
        kwv = results[idx]

    # 1) 洛雪容器详情（优先级最高）
    if isinstance(info, dict) and info:
        for k in ("title", "artist", "album", "duration_s", "cover_url"):
            v = info.get(k)
            if v:
                out[k] = v
    if isinstance(ritem, dict) and ritem:
        out["title"] = out.get("title") or str(ritem.get("title") or ritem.get("name") or "")
        out["artist"] = out.get("artist") or str(ritem.get("artist") or "")
        alb = ritem.get("album")
        if isinstance(alb, dict):
            alb = alb.get("name") or ""
        out["album"] = out.get("album") or str(ritem.get("albumName") or alb or "")
        nd = _norm_duration_s(ritem)
        if nd and (not out.get("duration_s") or 0 < float(out.get("duration_s") or 0) < 1):
            out["duration_s"] = nd
        out["cover_url"] = out.get("cover_url") or str(ritem.get("cover_url") or ritem.get("coverUrl")
                                                      or ritem.get("coverURL") or "")
    # 3) 网易云官方详情（lx 源对 wy 不返回封面，且时长字段常缺失）
    if isinstance(ne, dict) and ne:
        for k in ("title", "artist", "album", "cover_url"):
            if not out.get(k) and ne.get(k):
                out[k] = ne[k]
        if _dur_missing() and ne.get("duration_s"):
            out["duration_s"] = ne["duration_s"]
    # 4) 酷我 musicInfo（lx 链路对酷我恒返回空标题，必须靠它兜底）
    if kwv:
        try:
            t, a, al, dur, cv = kwv
        except Exception:
            t = ""
        if t:
            out["title"] = out.get("title") or t
            out["artist"] = out.get("artist") or a
            out["album"] = out.get("album") or al
            out["duration_s"] = out.get("duration_s") or dur
            out["cover_url"] = out.get("cover_url") or cv
    # 兜底校正：lx 把“秒”当“毫秒”上报时会出现 0.309 这类错误值
    try:
        ds = float(out.get("duration_s") or 0)
        if 0 < ds < 1:
            out["duration_s"] = ds * 1000.0
    except (TypeError, ValueError):
        pass
    try:
        out["cover_url"] = _normalize_cover_url(out.get("cover_url"))
    except Exception:
        pass
    _meta_set(guid, out)
    return out


# === v42: 在线元数据短时缓存 —— 省掉「首次播放 / 点收藏」重复的 ~4 秒串行查询 ===
# 洛雪容器单次 track/info 约 4 秒；同一次操作里 _online_info 常被调用两次
# （收藏流程：handler 先取一次，_resolve_record_meta 里再取一次），且列表/详情/封面
# 会反复取同一条。加一层 TTL 缓存后，同一曲目在同一 TTL 窗口内只查一次后端。
try:
    _ONLINE_INFO_TTL = float(os.environ.get("FNMUSIC_INFO_TTL", "300"))
except Exception:
    _ONLINE_INFO_TTL = 300.0
_ONLINE_INFO_CACHE: dict = {}
_ONLINE_INFO_INFLIGHT: dict = {}
_ONLINE_INFO_MAX = 1024


def _online_info_key(request, guid: str) -> str:
    """按（凭据 + 音源配置 + guid）分桶，避免不同登录态/配置互相串味。"""
    try:
        return "%s|%s|%s" % (_credential_scope(request),
                             json.dumps(_source_config(), sort_keys=True),
                             guid)
    except Exception:
        return "||%s" % guid


def _online_info_cache_get(request, guid: str):
    try:
        hit = _ONLINE_INFO_CACHE.get(_online_info_key(request, guid))
    except Exception:
        return None
    if not hit:
        return None
    exp, val = hit
    if exp <= time.monotonic() or not val:
        return None
    return dict(val)


def _online_info_cache_put(request, guid: str, data: dict) -> None:
    try:
        key = _online_info_key(request, guid)
        if len(_ONLINE_INFO_CACHE) >= _ONLINE_INFO_MAX:
            now = time.monotonic()
            for k in [k for k, v in list(_ONLINE_INFO_CACHE.items()) if v[0] <= now]:
                _ONLINE_INFO_CACHE.pop(k, None)
            if len(_ONLINE_INFO_CACHE) >= _ONLINE_INFO_MAX:
                _ONLINE_INFO_CACHE.clear()
        _ONLINE_INFO_CACHE[key] = (time.monotonic() + _ONLINE_INFO_TTL, dict(data))
    except Exception:
        pass


async def _prefetch_online_meta(request, items, limit: int = 3, concurrency: int = 2) -> None:
    """列表返回时后台预热在线曲目元数据。

    用户从歌单/历史点播放时 _online_info 已在缓存里 → 首播不再等那 ~4 秒。
    限量 + 限并发，避免把洛雪容器打满。
    """
    try:
        guids = []
        for it in (items or []):
            g = ""
            if isinstance(it, dict):
                g = str(it.get("guid") or "")
                if not g.startswith("online:") and isinstance(it.get("track"), dict):
                    g = str(it["track"].get("guid") or "")
            if not g.startswith("online:") or g in guids:
                continue
            if _online_info_cache_get(request, g) is not None:
                continue
            guids.append(g)
            if len(guids) >= limit:
                break
        if not guids:
            return
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _warm(g):
            async with sem:
                try:
                    await asyncio.wait_for(_online_info(request, g), timeout=6.0)
                except Exception:
                    pass

        for g in guids:
            _spawn_bg(_warm(g))
    except Exception:
        pass


def _meta_pick(info: dict) -> dict:
    try:
        return {k: info.get(k) for k in ("title", "artist", "album", "duration_s", "cover_url")
                if info.get(k)}
    except Exception:
        return {}


@app.api_route("/music/api/v1/static/cover", methods=["GET", "HEAD"])
@app.api_route("/music/api/v1/static/cover/{subpath:path}", methods=["GET", "HEAD"])
async def static_cover(request: Request, subpath: str = ""):
    guid = extract_guid(request, subpath if is_online_guid(subpath) else None)
    if not guid and subpath.startswith("online:"):
        guid = subpath
    if dailyrec.is_daily_playlist_guid(guid):
        # 封面请求绝不返回 401/JSON：手机端的图片请求可能不带会话 cookie，
        # 一旦这里返回 JSON，歌单海报就整块裂图（歌曲封面不走这里，故只有海报坏）。
        user_guid = ""
        try:
            upstream_client = get_upstream_client(request.app)
            _authed, user_guid, _auth = await _probe_upstream_auth(request, upstream_client)
        except Exception:
            user_guid = ""
        first_guid = _daily_poster_guid(user_guid)
        if first_guid:
            guid = first_guid
    if not is_online_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    # v43: 按客户端请求的 size 分档取图（此前固定 300px，歌单/专辑大卡片放大后发虚）
    _size = _cover_bucket(request.query_params.get("size"))
    # 命中本地封面缓存就直接返回：否则要先问 lx 容器（_online_info 约 4 秒），手机端会超时
    __hit = _cover_by_guid_get(guid, _size)
    if __hit:
        return Response(content=__hit[0], media_type=__hit[1],
                        headers={"Cache-Control": "public, max-age=604800"})
    data = await _online_info(request, guid)
    cover = (data or {}).get("cover_url") or ""
    cover = _normalize_cover_url(cover)
    if not cover:
        cover = _normalize_cover_url(str(_meta_get(guid).get("cover_url") or ""))
    if not cover:
        ne_id = _netease_song_id(guid)
        if ne_id:
            ne = await _netease_detail(ne_id)
            cover = str((ne or {}).get("cover_url") or "")
            if cover:
                try:
                    _meta_set(guid, {"cover_url": cover})
                except Exception:
                    pass
    if cover:
        # 按客户端 size 取对应尺寸的小图（网易云原图可达数 MB，抓取要 3~4 秒 → 列表渲染超时）
        got = await _fetch_image_bytes(_sized_cover_url(cover, _size))
        if not got:
            got = await _fetch_image_bytes(cover)
        if got:
            body, ct = got
            _cover_by_guid_put(guid, body, ct, size=_size)
            return Response(content=body, media_type=ct,
                            headers={"Cache-Control": "public, max-age=604800"})
        # 抓图失败：退回任意已缓存档位，比 302 外链 / 灰图对手机端友好得多
        _fb = _cover_by_guid_get(guid, _size, fallback=True)
        if _fb:
            return Response(content=_fb[0], media_type=_fb[1],
                            headers={"Cache-Control": "public, max-age=604800"})
        return RedirectResponse(cover, status_code=302)
    # 手机端列表会因某条封面取不到而整列不渲染 —— 必须返回有效图片，不能 404。
    # 占位图缓存 6 小时：既能让客户端秒开，又不会在真实封面恢复后被永久盖住。
    _ph = _placeholder_png_bytes()
    try:
        _cover_by_guid_put(guid, _ph, "image/png", ttl=21600)
    except Exception:
        pass
    return Response(content=_ph, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=21600"})


# === online favorites ===

_FAV_LOCK = asyncio.Lock()


def sanitize_user_guid(guid: str | None) -> str:
    """过滤文件名合法字符 [A-Za-z0-9-_]，非法字符替换为 _；为空则返回 'shared'。"""
    raw = str(guid or "").strip()
    safe = re.sub(r"[^A-Za-z0-9\-_]", "_", raw)
    return safe or "shared"


def user_fav_path(user_guid: str) -> str:
    fav_dir = CONF.get("fav_dir") or os.path.join(_HOME, "online_favorites")
    safe_name = sanitize_user_guid(user_guid)
    return os.path.join(fav_dir, f"{safe_name}.json")


def load_online_favorites(user_guid: str) -> list[dict]:
    path = user_fav_path(user_guid)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.warning("Failed to load online favorites for %s from %s: %s", user_guid, path, e)
    return []


def save_online_favorites(user_guid: str, items: list[dict]) -> bool:
    path = user_fav_path(user_guid)
    parent = os.path.dirname(path) or "."
    part_path = f"{path}.{uuid4().hex[:8]}.part"
    try:
        os.makedirs(parent, exist_ok=True)
        with open(part_path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(part_path, path)
        return True
    except Exception as e:
        logger.warning("Failed to save online favorites for %s to %s: %s", user_guid, path, e)
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        return False


def build_favorite_track_obj(guid: str, info: dict | None = None, created_at: int | None = None) -> dict:
    raw_info = dict(info or {})
    raw_info.setdefault("id", song_id_from_online_guid(guid))
    raw_info.setdefault("source", source_from_online_guid(guid))
    vo = build_online_track(raw_info)

    now = int(time.time())
    ts = created_at or now

    artist_name = vo.get("artist") or ""
    artists_list = [
        {
            "guid": f"{guid}:artist",
            "name": artist_name,
            "coverId": guid,
            "createdAt": ts,
            "updatedAt": ts,
        }
    ] if artist_name else []

    album_name = vo.get("albumName") or (vo.get("album", {}).get("name") if isinstance(vo.get("album"), dict) else "") or ""
    # 专辑名为空时给兜底值：本地曲目专辑名恒非空，App 可能按专辑分组/排序
    album_name = album_name or "未知专辑"
    album_obj = {
        "guid": f"{guid}:album",
        "name": album_name,
        "artists": artists_list,
        "coverId": guid,
        "releaseDate": None,
        "barcode": None,
        "createdAt": ts,
        "updatedAt": ts,
    }

    audio_spec = vo.get("audioSpec") or {}
    # 与本地曲目逐字段对齐：本地有 bitDepth、缺失字段为 null、数值非 0；
    # 手机端对字段/类型敏感，不一致时会出现整列不渲染。
    audio_spec = {
        "bitDepth": audio_spec.get("bitDepth") or 16,
        "sampleRate": audio_spec.get("sampleRate") or 44100,
        "channel": audio_spec.get("channel") or 2,
        "bitrate": audio_spec.get("bitrate") or 320000,
        "codec": audio_spec.get("codec") or "mp3",
        "container": audio_spec.get("container") or "",
        "duration": int(audio_spec.get("duration") or vo.get("duration") or 0),
        "format": audio_spec.get("format") or "mp3",
        "path": audio_spec.get("path") or "",
        "size": int(audio_spec.get("size") or 0),
    }

    return {
        "guid": guid,
        "title": vo.get("title") or "",
        "coverId": guid,
        "year": None,
        "discNo": None,
        "trackNo": None,
        "isrc": None,
        "duration": int(vo.get("duration") or 0),
        "isCue": False,
        "createdAt": ts,
        "updatedAt": ts,
        "album": album_obj,
        "artists": artists_list,
        "genres": [],
        "audioSpec": audio_spec,
        "isFavorite": True,
    }


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
    return bool(re.match(r"^\s*bytes\s*=\s*0\s*-", range_header.strip(), re.I))


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
            try:
                os.replace(part, dest)
            except OSError:
                # 跨文件系统时 rename 会抛 EXDEV；就地复制再删
                shutil.move(part, dest)
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
            # v54：音频已进曲库。若这次 info 里没带歌词（收藏前播放过、歌词已在 cache/），
            # 把那份缓存歌词提升成曲库同名 sidecar，保证「音频到哪、歌词到哪」。
            if not lyric:
                try:
                    await asyncio.to_thread(promote_one_lyric, guid)
                except Exception:
                    pass
            request_library_scan("favdl")
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
    pinned_lyric = recalled_lyric_path(guid)
    if pinned_lyric:
        stems.add(os.path.splitext(pinned_lyric)[0])
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
    for ref in (media_ref_path(guid), lyric_ref_path(guid)):
        if os.path.isfile(ref):
            try:
                os.remove(ref)
                removed.append(ref)
            except Exception:
                pass
    _probe_write("[favdel] guid=%s removed=%d %s" % (guid, len(removed), "|".join(removed) or "-"))
    return removed


# === v54：歌词「贴身」自愈 ===
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


# === v53 孤儿歌词自愈 ===
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
        body = (r.text or "")[:200].replace("\n", " ")
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


async def _probe_upstream_auth(request: Request, client: httpx.AsyncClient) -> tuple[bool, str, Response | None]:
    """向上游探测用户是否已登录。复用当前请求 headers。
    返回 (is_authed, user_guid, error_response)。
    """
    headers = copy_incoming_headers(request)
    try:
        probe_req = client.build_request("GET", "/music/api/v1/user/me", headers=headers)
        probe_resp = await client.send(probe_req)
        resp_headers = filter_headers(probe_resp.headers, exclude_keys={"content-length", "content-encoding"})

        if probe_resp.status_code == 401:
            return False, "", Response(
                content=probe_resp.content,
                status_code=401,
                headers=resp_headers,
                media_type=probe_resp.headers.get("content-type"),
            )

        if probe_resp.status_code == 200:
            try:
                probe_json = probe_resp.json()
                if isinstance(probe_json, dict) and probe_json.get("code") == 99999:
                    return False, "", JSONResponse(
                        content=probe_json,
                        status_code=200,
                        headers=resp_headers,
                    )
                if isinstance(probe_json, dict) and probe_json.get("code") == 0:
                    data = probe_json.get("data")
                    if isinstance(data, dict) and data.get("guid"):
                        return True, str(data["guid"]), None
                    logger.warning("user/me response missing data.guid, falling back to 'shared': %s", probe_json)
                    return True, "shared", None
            except Exception as e:
                logger.warning("Failed to parse user/me json response: %s", e)
                return True, "shared", None
            return True, "shared", None

        # 其他非 200/401 状态码，上游异常
        return True, "shared", None
    except Exception as e:
        logger.warning("Upstream auth probe failed: %s", e)
        # 探测异常时保守放行
        return True, "shared", None


@app.post("/music/api/v1/favorite-track/create")
async def favorite_track_create(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    if isinstance(body, dict):
        guid = str(body.get("trackGUID") or body.get("guid") or "").strip()

    if not is_online_guid(guid):
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    now = int(time.time())
    info = await _online_info(request, guid)
    if isinstance(info, dict) and info:
        try:
            _meta_set(guid, _meta_pick(info))
        except Exception:
            pass
    # 总是尝试补全字段（lx 源常缺时长/封面，或把秒当毫秒）
    try:
        rm = await _resolve_record_meta(request, guid)
        if rm:
            info = dict(info or {})
            for k, v in rm.items():
                if not v:
                    continue
                cur = info.get(k)
                if k == "duration_s":
                    try:
                        if float(cur or 0) >= 1:
                            continue
                    except (TypeError, ValueError):
                        pass
                elif cur:
                    continue
                info[k] = v
    except Exception:
        pass
    if online_history_mode() != "full":
        # 保守模式：在线曲目不进收藏列表（手机端对该类条目会整列不渲染）
        return JSONResponse(content={"code": 0, "msg": "", "data": None})
    # 护栏：解析不出标题的在线曲目不入库，避免手机端收藏整列不渲染
    if not str((info or {}).get("title") or "").strip():
        logger.warning("skip online favorite, empty title: %s", guid)
        return JSONResponse(content={"code": 0, "msg": "", "data": None})
    if not info:
        cached_lyric = read_lyric_cache(guid)
        title = ""
        artist = ""
        cached_media = find_cache_file(guid)
        if cached_media:
            base = os.path.splitext(os.path.basename(cached_media))[0]
            if " - " in base:
                artist, title = base.split(" - ", 1)
            else:
                title = base
        info = {
            "id": song_id_from_online_guid(guid),
            "source": source_from_online_guid(guid),
            "title": title,
            "artist": artist,
            "lyric": cached_lyric,
        }

    track_obj = build_favorite_track_obj(guid, info, created_at=now)

    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            # 查重
            idx = next((i for i, it in enumerate(items) if it.get("guid") == guid), None)
            if idx is not None:
                # 幂等更新
                items[idx]["track"] = track_obj
            else:
                items.append({
                    "guid": guid,
                    "createdAt": now,
                    "track": track_obj,
                })
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error updating online favorites for user %s: %s", user_guid, e)

    # v50 收藏即下载：后台整轨落盘，立刻返回不拖慢 App
    invalidate_favorites_cache()
    if CONF.get("fav_dl_on_favorite"):
        spawn_favorite_download(request, guid)

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.post("/music/api/v1/favorite-track/delete")
async def favorite_track_delete(request: Request):
    upstream_client = get_upstream_client(request.app)
    try:
        body = await request.json()
    except Exception:
        body = {}

    guid = ""
    if isinstance(body, dict):
        guid = str(body.get("trackGUID") or body.get("guid") or "").strip()

    if not is_online_guid(guid):
        return await forward_to_upstream(request, upstream_client)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    async with _FAV_LOCK:
        try:
            items = load_online_favorites(user_guid)
            items = [it for it in items if it.get("guid") != guid]
            save_online_favorites(user_guid, items)
        except Exception as e:
            logger.warning("Error deleting from online favorites for user %s: %s", user_guid, e)

    # v50 取消收藏即删对应文件；并集里还有人收藏则保留
    invalidate_favorites_cache()
    if CONF.get("fav_dl_delete_on_unfav") and not is_favorite_online_guid(guid):
        removed = []
        try:
            removed = await asyncio.to_thread(delete_materialized_media, guid)
        except Exception as e:
            logger.warning("Failed to delete materialized media for %s: %s", guid, e)
        if removed:
            request_library_scan("unfav")
            try:
                await asyncio.to_thread(sweep_orphan_lyrics)
            except Exception:
                pass

    return JSONResponse(content={"code": 0, "msg": "", "data": None})


@app.get("/music/api/v1/favorite-track/list")
async def favorite_track_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    url_path = request.url.path
    if request.url.query:
        url_path = f"{url_path}?{request.url.query}"
    headers = copy_incoming_headers(request)

    req = upstream_client.build_request("GET", url_path, headers=headers)
    upstream_resp = await upstream_client.send(req)
    resp_headers = filter_headers(upstream_resp.headers, exclude_keys={"content-length", "content-encoding"})

    if upstream_resp.status_code != 200:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    try:
        upstream_json = upstream_resp.json()
    except Exception:
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type"),
        )

    if not isinstance(upstream_json, dict) or upstream_json.get("code") != 0:
        return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)

    # 探测当前用户身份
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp

    # 成功获取官方列表，合并本地在线收藏
    data = upstream_json.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        upstream_json["data"] = data

    official_list = data.get("list")
    if not isinstance(official_list, list):
        official_list = []
        data["list"] = official_list

    # 飞牛音乐前端收藏列表依赖 isFavorite=True 状态判断，遍历补齐官方列表中可能缺失的字段
    for item in official_list:
        if isinstance(item, dict):
            item["isFavorite"] = True

    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official_list)

    async with _FAV_LOCK:
        try:
            fav_items = load_online_favorites(user_guid)
        except Exception as e:
            logger.warning("Error reading online favorites for list for user %s: %s", user_guid, e)
            fav_items = []

    # 按 createdAt 倒序
    fav_items_sorted = sorted(fav_items, key=lambda x: x.get("createdAt", 0), reverse=True)
    online_tracks = []
    for it in fav_items_sorted:
        t = it.get("track")
        if isinstance(t, dict):
            # 确保关键属性为最新或格式完整
            t["isFavorite"] = True
            online_tracks.append(t)
        else:
            g = it.get("guid") or ""
            if g:
                online_tracks.append(build_favorite_track_obj(g, created_at=it.get("createdAt")))

    if online_history_mode() != "full":
        online_tracks = []
    data["list"] = official_list + online_tracks
    data["total"] = official_total + len(online_tracks)
    _prefetch_online_covers(data["list"])
    # v45: _prefetch_online_meta 是 async，此前裸调用等于「建了协程但从不执行」= 死代码，
    # 首播仍要现问一次元数据（约 4 秒）。这里连同取流地址预热一起真正调度起来。
    _spawn_bg(_prefetch_online_meta(request, data["list"]))
    _spawn_bg(_prefetch_stream_urls(request, data["list"]))

    return JSONResponse(content=upstream_json, status_code=upstream_resp.status_code, headers=resp_headers)


# === daily recommend + play history ===

_HISTORY_LOCK = asyncio.Lock()
_DAILY_TASKS: dict[str, asyncio.Task] = {}


def _prune_stale_daily_tasks(day: str) -> None:
    suffix = f":{day}"
    stale = [k for k in list(_DAILY_TASKS) if not str(k).endswith(suffix)]
    for k in stale:
        old = _DAILY_TASKS.pop(k, None)
        if old is not None and not old.done():
            old.cancel()


async def _ensure_daily_task(request: Request, user_guid: str) -> asyncio.Task:
    day = dailyrec.today_key()
    _prune_stale_daily_tasks(day)
    key = f"{user_guid}:{day}"
    task = _DAILY_TASKS.get(key)
    if task is not None and not task.done():
        return task
    if task is not None and task.done():
        try:
            if task.exception() is None:
                result = task.result()
                if isinstance(result, dict) and len(result.get("tracks") or []) >= dailyrec.PLAYLIST_SIZE:
                    return task
        except (asyncio.CancelledError, Exception):
            pass
    async with _FAV_LOCK:
        favs = load_online_favorites(user_guid)
    task = asyncio.create_task(
        dailyrec.get_or_build_daily(
            user_guid=user_guid,
            musicdl_client=get_musicdl_client(request.app) if CONF.get("musicdl_enabled", True) else None,
            musicbox_client=get_musicbox_client(request.app) if CONF["netease_enabled"] else None,
            llm_http=get_llm_client(request.app) if dailyrec.llm_enabled() else None,
            build_track=build_online_track,
            netease_enabled=CONF["netease_enabled"],
            favorite_items=favs,
            lx_client=get_lx_client(request.app) if CONF.get("lx_enabled", True) else None,
            lx_enabled=bool(CONF.get("lx_enabled", True)),
        )
    )
    _DAILY_TASKS[key] = task
    return task


async def _peek_daily_bundle(request: Request, user_guid: str) -> dict:
    """歌单列表用：有缓存立刻返回；否则后台生成，最多等 2s，超时仍返回占位歌单。"""
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day)
    if cached and cached.get("tracks"):
        return cached
    task = await _ensure_daily_task(request, user_guid)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    except asyncio.TimeoutError:
        cached = dailyrec.load_daily_cache(user_guid, day)
        if cached and cached.get("tracks"):
            return cached
        return dailyrec.empty_daily_bundle(user_guid)
    except Exception as e:
        logger.warning("daily recommend peek failed: %s", e)
        return dailyrec.empty_daily_bundle(user_guid)


async def _load_daily_bundle(request: Request, user_guid: str) -> dict:
    day = dailyrec.today_key()
    dailyrec.purge_stale_daily_cache(user_guid, day)
    cached = dailyrec.load_daily_cache(user_guid, day)
    if cached and cached.get("tracks"):
        return cached

    task = await _ensure_daily_task(request, user_guid)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=20.0)
    except asyncio.TimeoutError:
        cached = dailyrec.load_daily_cache(user_guid, day)
        if cached and cached.get("tracks"):
            return cached
        return dailyrec.empty_daily_bundle(user_guid)


def _warm_daily_poster(cover_id) -> None:
    """每日推荐/歌单卡片在首页用的是大图（客户端按 size=600 拉），提前抓一份到本地缓存。

    手机端对封面加载很敏感：等它来拉时才现抓，海报位置会先渲染成灰块/糊图。
    """
    try:
        g = str(cover_id or "")
        if not g.startswith("online:"):
            return
        for b in (600, 300):
            _prefetch_online_covers([{"guid": g}], size=b)
    except Exception:
        pass


def _playlist_public_fields(record: dict) -> dict:
    _warm_daily_poster(record.get("coverId") or record.get("guid"))
    return {
        "guid": record.get("guid"),
        "name": record.get("name") or "每日推荐",
        "coverId": record.get("coverId") or record.get("guid"),
        "createdAt": int(record.get("createdAt") or time.time()),
        "updatedAt": int(record.get("updatedAt") or time.time()),
        "trackCount": int(record.get("trackCount") or 0),
        "isDaily": True,
    }


@app.get("/music/api/v1/playlist/list")
@app.get("/music/api/v1/playlist/list/{subpath:path}")
async def playlist_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        return auth_resp or JSONResponse(content=envelope, headers=headers)

    try:
        bundle = await _peek_daily_bundle(request, user_guid)
    except Exception as e:
        logger.warning("daily recommend list inject failed: %s", e)
        return JSONResponse(content=envelope, headers=headers)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official
    rec = _playlist_public_fields(bundle.get("playlist") or {})
    rec["trackCount"] = len(bundle.get("tracks") or [])
    official = [
        it for it in official
        if not (isinstance(it, dict) and dailyrec.is_daily_playlist_guid(str(it.get("guid") or "")))
    ]
    data["list"] = [rec] + official
    total = data.get("total")
    data["total"] = (total if isinstance(total, int) else len(official)) + 1
    return JSONResponse(content=envelope, headers=headers)


@app.get("/music/api/v1/playlist/detail")
async def playlist_detail(request: Request):
    guid = str(request.query_params.get("guid") or "").strip()
    if not dailyrec.is_daily_playlist_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid)
    rec = _playlist_public_fields(bundle.get("playlist") or {})
    rec["trackCount"] = len(bundle.get("tracks") or [])
    return JSONResponse(content={"code": 0, "msg": "ok", "data": rec})


@app.get("/music/api/v1/playlist/batch-detail")
async def playlist_batch_detail(request: Request):
    raw = request.query_params.get("guids") or request.query_params.get("guid") or ""
    guids = [g.strip() for g in raw.split(",") if g.strip()]
    daily_ids = [g for g in guids if dailyrec.is_daily_playlist_guid(g)]
    if not daily_ids:
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    rest = [g for g in guids if not dailyrec.is_daily_playlist_guid(g)]
    official_list: list = []
    if rest:
        headers = copy_incoming_headers(request)
        req = upstream_client.build_request(
            "GET",
            f"/music/api/v1/playlist/batch-detail?guids={quote(','.join(rest), safe=',')}",
            headers=headers,
        )
        resp = await upstream_client.send(req)
        if resp.status_code == 200:
            try:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("code") == 0:
                    data = payload.get("data") or {}
                    if isinstance(data, dict) and isinstance(data.get("list"), list):
                        official_list = data["list"]
                    elif isinstance(data, list):
                        official_list = data
            except Exception:
                official_list = []

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid)
    rec = _playlist_public_fields(bundle.get("playlist") or {})
    rec["trackCount"] = len(bundle.get("tracks") or [])
    return JSONResponse(content={"code": 0, "msg": "ok", "data": {"list": [rec] + official_list}})


@app.get("/music/api/v1/track/playlist-detail/list")
async def playlist_track_list(request: Request):
    guid = str(
        request.query_params.get("playlistGUID")
        or request.query_params.get("playlistGuid")
        or request.query_params.get("guid")
        or ""
    ).strip()
    if not dailyrec.is_daily_playlist_guid(guid):
        return await forward_to_upstream(request, get_upstream_client(request.app))

    upstream_client = get_upstream_client(request.app)
    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed and auth_resp is not None:
        return auth_resp
    bundle = await _load_daily_bundle(request, user_guid)
    tracks = dailyrec.stamp_playlist_tracks(list(bundle.get("tracks") or []))
    try:
        page = max(int(request.query_params.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        size = int(request.query_params.get("size") or 50)
    except (TypeError, ValueError):
        size = 50
    if size < 1:
        size = 50
    start = (page - 1) * size
    page_tracks = tracks[start:start + size] if size != -1 else tracks
    _prefetch_online_covers(page_tracks)
    _spawn_bg(_prefetch_online_meta(request, page_tracks))
    _spawn_bg(_prefetch_stream_urls(request, page_tracks))
    return JSONResponse(
        content={
            "code": 0,
            "msg": "ok",
            "data": {"list": page_tracks, "total": len(tracks), "sort": request.query_params.get("sort") or ""},
        }
    )


@app.post("/music/api/v1/event/report")
async def event_report(request: Request):
    upstream_client = get_upstream_client(request.app)
    raw = await request.body()
    try:
        body = json.loads(raw.decode("utf-8") or "{}") if raw else {}
    except Exception:
        body = {}
    events = body.get("events") if isinstance(body, dict) else None
    online_plays: list[str] = []
    other_events: list = []
    if isinstance(events, list):
        for ev in events:
            if not isinstance(ev, dict):
                continue
            et = str(ev.get("eventType") or ev.get("type") or "")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            guid = str(payload.get("trackGUID") or payload.get("guid") or "")
            if et in ("track_play", "TrackPlay") and is_online_guid(guid):
                online_plays.append(guid)
            else:
                other_events.append(ev)
    else:
        return await forward_to_upstream(request, upstream_client)

    if online_plays:
        is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
        if is_authed:
            async with _HISTORY_LOCK:
                for guid in online_plays:
                    if online_history_mode() != "full":
                        continue
                    meta = await _resolve_record_meta(request, guid)
                    cached = find_cache_file(guid)
                    title = str(meta.get("title") or "")
                    artist = str(meta.get("artist") or "")
                    if cached:
                        base = os.path.splitext(os.path.basename(cached))[0]
                        if " - " in base:
                            artist, title = base.split(" - ", 1)
                        elif not title:
                            title = base
                    if not title.strip():
                        # 解析不出标题的在线曲目绝不入库 —— 空标题会让手机端整列不渲染
                        logger.warning("skip online history record, empty title: %s", guid)
                        continue
                    meta["title"] = title
                    meta["artist"] = artist
                    try:
                        _meta_set(guid, meta)
                    except Exception:
                        pass
                    dailyrec.record_online_play(
                        user_guid,
                        guid,
                        {"guid": guid, "title": title, "artist": artist,
                         "source": source_from_online_guid(guid),
                         "album": meta.get("album") or "",
                         "duration_s": meta.get("duration_s") or 0,
                         "cover_url": meta.get("cover_url") or ""},
                    )
        elif auth_resp is not None and not other_events:
            return auth_resp

    if other_events:
        headers = copy_incoming_headers(request)
        fwd = dict(body)
        fwd["events"] = other_events
        req = upstream_client.build_request(
            "POST",
            "/music/api/v1/event/report",
            headers=headers,
            content=json.dumps(fwd).encode("utf-8"),
        )
        resp = await upstream_client.send(req)
        resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"),
        )
    return JSONResponse(content={"code": 0, "msg": "ok", "data": None})


# === v46: 播放历史「删除」必须由插件接管在线条目 ===
#
# 上游对 guid 做原生判定：实测 trackGuids 传 32位hex/不存在/过短/大写/空数组
# 一律 code:0，唯独 `online:*` 一律 {"code":100002,"msg":"invalid arguments"}。
# 而在线曲目的历史记录只存在插件自己的 play_history/<user>.json 里
# ⇒ 原样转发必然让 App 弹「无效参数」，且这几条永远删不掉。
# 所以：属于插件的 online 条目自己删；原生 guid 才转给上游。

# 官方前端（web/App 同一套产物）真实发的是 `trackGUIDs`（大写 GUID）：
#   t.playHistory.delete({ trackGUIDs: n })
# 上游实测 trackGUIDs / trackGuids 都认，guids / ids 一律 100001。
# ⇒ 键名一律归一化后比对，并对任何含 "guid" 的键兜底。
_DELETE_GUID_KEYS = ("trackGUIDs", "trackGUIDs[]", "trackGuids", "trackGuids[]",
                     "guids", "guids[]", "ids", "ids[]", "id", "guid", "guid[]",
                     "trackGUID", "trackGuid")

_GUID_VALUE_RE = re.compile(r"^[0-9a-fA-F]{16,64}$")


def _norm_guid_key(key) -> str:
    """键名归一化：去空白、去 [] 后缀、转小写。"""
    return str(key).strip().rstrip("[]").strip().lower()


_DELETE_KEY_SET = {_norm_guid_key(_k) for _k in _DELETE_GUID_KEYS}


def _is_guid_key(key) -> bool:
    """该键是否承载待删 guid（大小写不敏感；含 "guid" 一律认）。"""
    normalized = _norm_guid_key(key)
    if not normalized:
        return False
    if normalized in _DELETE_KEY_SET:
        return True
    return "guid" in normalized


def _looks_like_guid(value) -> bool:
    """值本身是否像 guid：online:* 前缀或 16~64 位 hex。"""
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith("online:"):
        return True
    return bool(_GUID_VALUE_RE.match(text))


def _delete_guids_from_body(body, request: Request) -> tuple[list[str], list[str]]:
    """取出 (命中的键名, 待删 guid 列表)；兼容 JSON body / 裸数组 / query / 表单。"""
    keys: list[str] = []
    found: list[str] = []

    def _add(value) -> None:
        if isinstance(value, str):
            value = value.strip()
            if value:
                found.append(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _add(item)

    def _take(key, value) -> None:
        keys.append(str(key))
        _add(value)

    if isinstance(body, dict):
        for key, value in body.items():
            if _is_guid_key(key):
                _take(key, value)
        if not found:
            # 兜底：键名全不认识时，按「值像不像 guid」再捞一遍（要求至少一个真 guid 形态）
            for key, value in body.items():
                candidates = value if isinstance(value, list) else [value]
                if not candidates:
                    continue
                if all(_looks_like_guid(item) for item in candidates) and any(
                        str(item).startswith("online:") or _GUID_VALUE_RE.match(str(item).strip())
                        for item in candidates):
                    _take(key, value)
    elif isinstance(body, list):
        _add(body)
    try:
        for key in request.query_params.keys():
            if not _is_guid_key(key):
                continue
            for value in request.query_params.getlist(key):
                _take(key, value)
    except Exception:
        pass
    return list(dict.fromkeys(keys)), list(dict.fromkeys(found))


@app.post("/music/api/v1/play-history/delete")
async def play_history_delete(request: Request):
    upstream_client = get_upstream_client(request.app)
    raw = await request.body()
    body = None
    if raw:
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None

    matched_keys, guids = _delete_guids_from_body(body, request)
    if not guids:
        # 取不到任何 guid（例如将来的「清空全部」用别的字段）⇒ 不插手，原样转发
        logger.warning("play-history/delete: 未识别到 guid，原样转发 (body_type=%s keys=%s)",
                       type(body).__name__,
                       sorted(body.keys()) if isinstance(body, dict) else "-")
        _probe_write("[hist-del] UNRECOGNIZED body_type=%s keys=%s raw=%s" % (
            type(body).__name__,
            sorted(body.keys()) if isinstance(body, dict) else "-",
            raw[:200]))
        return await forward_to_upstream(request, upstream_client)

    online = [g for g in guids if is_online_guid(g)]
    native = [g for g in guids if not is_online_guid(g)]
    logger.info("play-history/delete: 收到 %d 个 guid（在线 %d / 原生 %d）keys=%s",
                len(guids), len(online), len(native), matched_keys)
    _probe_write("[hist-del] keys=%s total=%d online=%d native=%d" % (
        matched_keys, len(guids), len(online), len(native)))

    removed = 0
    if online:
        is_authed, user_guid, _auth = await _probe_upstream_auth(request, upstream_client)
        if is_authed:
            async with _HISTORY_LOCK:
                items = dailyrec.load_online_play_history(user_guid)
                targets = set(online)
                kept = [it for it in items if str(it.get("guid") or "") not in targets]
                removed = len(items) - len(kept)
                if removed:
                    dailyrec.save_online_play_history(user_guid, kept)
            logger.info("play-history/delete: 在线记录删除 %d 条 user=%s", removed, user_guid)
            _probe_write("[hist-del] removed=%d user=%s" % (removed, user_guid))
        else:
            logger.warning("play-history/delete: 未认证，在线记录未删除: %s", online)
            _probe_write("[hist-del] UNAUTHED online=%s" % (online,))

    if not native:
        # 全部是插件的在线条目（或压根没有原生 guid）⇒ 上游会判无效参数，直接回成功
        return JSONResponse(content={"code": 0, "msg": "", "data": None})

    # 只把原生 guid 转给上游；online:* 必须剔掉，否则整批被判「无效参数」
    payload = dict(body) if isinstance(body, dict) and body else {"trackGUIDs": native}
    for key in matched_keys:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, list):
            payload[key] = [g for g in value if not is_online_guid(str(g))]
        elif isinstance(value, str) and is_online_guid(value):
            payload.pop(key, None)

    url_path = request.url.path
    if request.url.query:
        url_path = url_path + "?" + request.url.query
    try:
        req = upstream_client.build_request(
            method="POST", url=url_path,
            headers=copy_incoming_headers(request),
            content=json.dumps(payload).encode("utf-8"),
        )
        resp = await upstream_client.send(req)
    except Exception as e:
        logger.warning("play-history/delete forward failed: %s", e)
        return JSONResponse(content={"code": 0, "msg": "", "data": None} if removed
                            else {"code": 100001, "msg": "unknown error", "data": None})

    resp_headers = filter_headers(resp.headers, exclude_keys={"content-length", "content-encoding"})
    try:
        upstream_code = (resp.json() or {}).get("code")
    except Exception:
        upstream_code = None
    if removed and upstream_code not in (0, None):
        # 在线那部分确实删掉了，别让 App 因为原生那部分的报错而弹窗、不刷新
        logger.warning("play-history/delete: 上游 code=%s，但在线路条目已删 %d 条", upstream_code, removed)
        return JSONResponse(content={"code": 0, "msg": "", "data": None})
    return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers,
                    media_type=resp.headers.get("content-type"))


@app.get("/music/api/v1/play-history/list")
async def play_history_list(request: Request):
    upstream_client = get_upstream_client(request.app)
    envelope = await fetch_upstream_envelope(request, upstream_client)
    if isinstance(envelope, Response):
        return envelope
    headers = envelope.pop("_ext_headers", {})
    if envelope.get("code") != 0:
        return JSONResponse(content=envelope, headers=headers)

    is_authed, user_guid, auth_resp = await _probe_upstream_auth(request, upstream_client)
    if not is_authed:
        return auth_resp or JSONResponse(content=envelope, headers=headers)

    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {"list": [], "total": 0}
        envelope["data"] = data
    official = data.get("list")
    if not isinstance(official, list):
        official = []
        data["list"] = official

    async with _HISTORY_LOCK:
        online_items = dailyrec.load_online_play_history(user_guid)
    online_tracks = []
    for it in reversed(online_items):
        guid = str(it.get("guid") or "")
        if not guid:
            continue
        track = it.get("track") if isinstance(it.get("track"), dict) else {}
        obj = build_favorite_track_obj(guid, track, created_at=int(it.get("playedAt") or time.time()))
        obj["isFavorite"] = False
        online_tracks.append(obj)

    seen = {str(x.get("guid")) for x in official if isinstance(x, dict)}
    merged_online = [t for t in online_tracks if t.get("guid") not in seen]
    if online_history_mode() != "full":
        merged_online = []
    data["list"] = merged_online + official
    _prefetch_online_covers(data["list"])
    _spawn_bg(_prefetch_online_meta(request, data["list"]))
    _spawn_bg(_prefetch_stream_urls(request, data["list"]))
    official_total = data.get("total")
    if not isinstance(official_total, int):
        official_total = len(official)
    data["total"] = official_total + len(merged_online)
    return JSONResponse(content=envelope, headers=headers)


@app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def catch_all(request: Request, full_path: str):
    return await forward_to_upstream(request, get_upstream_client(request.app))
