"""Optional NEMbox internals for batch detail / lyrics (NetEase-MusicBox)."""
from __future__ import annotations

import threading
import time
from typing import Any

_api_lock = threading.Lock()
_api_instance = None


def _get_api():
    global _api_instance
    if _api_instance is None:
        with _api_lock:
            if _api_instance is None:
                from runner import ensure_xdg_dirs

                ensure_xdg_dirs()
                from NEMbox.api import NetEase

                _api_instance = NetEase()
    return _api_instance


def _map_song_detail(item: dict[str, Any]) -> dict[str, Any]:
    sid = item.get("id") or item.get("song_id")
    song_id = int(sid) if sid is not None else 0
    name = str(item.get("name") or "")
    ar_list = item.get("ar") or item.get("artists") or []
    if isinstance(ar_list, list):
        artist = " / ".join(
            str(a.get("name")) for a in ar_list if isinstance(a, dict) and a.get("name")
        )
    else:
        artist = ""
    al = item.get("al") or item.get("album") or {}
    if isinstance(al, dict):
        album_name = str(al.get("name") or "")
        album_pic_url = str(al.get("picUrl") or al.get("pic_url") or "")
    else:
        album_name = ""
        album_pic_url = ""
    duration_ms = int(item.get("dt") or item.get("duration") or 0)
    return {
        "song_id": song_id,
        "name": name,
        "artist": artist,
        "album_name": album_name,
        "album_pic_url": album_pic_url,
        "duration_ms": duration_ms,
        "has_sq": bool(item.get("sq")),
        "has_hr": bool(item.get("hr")),
    }


# ---- v75 self-healing login probe ----
_LOGIN_STATE = {"ts": 0.0, "val": None}
_LOGIN_TTL_OK = 300.0
_LOGIN_TTL_BAD = 60.0


def _reset_api_instance():
    """Drop the cached NetEase() instance so the next call re-reads the cookie file."""
    global _api_instance
    with _api_lock:
        _api_instance = None
    return _get_api()


def _cli_auth_logged_in():
    """Last resort: ask the musicbox CLI (fresh process, reads cookie from disk)."""
    try:
        import json as _json

        from runner import run_musicbox as _run

        code, out, _err = _run(["auth", "status", "--json"], timeout=25.0)
        if code != 0:
            return None
        payload = _json.loads(out or "{}")
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload
        if isinstance(data, dict) and "logged_in" in data:
            return bool(data.get("logged_in"))
    except Exception:
        return None
    return None


def _probe_logged_in() -> bool:
    # 1) the cached in-process instance
    try:
        api = _get_api()
        with _api_lock:
            info = api.get_account_info()
        if info and (info.get("account") or info.get("profile")):
            return True
    except Exception:
        pass
    # 2) rebuild the instance -- the cookie file may have been refreshed after startup
    try:
        api = _reset_api_instance()
        with _api_lock:
            info = api.get_account_info()
        if info and (info.get("account") or info.get("profile")):
            return True
    except Exception:
        pass
    # 3) fall back to the CLI subprocess
    return bool(_cli_auth_logged_in())


def check_is_logged_in() -> bool:
    """Cached login probe with self healing (v75)."""
    now = time.time()
    cached = _LOGIN_STATE.get("val")
    ts = float(_LOGIN_STATE.get("ts") or 0.0)
    ttl = _LOGIN_TTL_OK if cached else _LOGIN_TTL_BAD
    if cached is not None and (now - ts) < ttl:
        return bool(cached)
    val = bool(_probe_logged_in())
    _LOGIN_STATE["val"] = val
    _LOGIN_STATE["ts"] = now
    return val
# ---- end v75 ----


def filter_playable_song_ids(ids: list[int]) -> set[int]:
    """根据真实可播放状态过滤歌曲 ID。

    - 未登录时：使用 api.songs_url 批量获取真实可播状态。凡是 url 为空/404、或者带有 freeTrialInfo（试听片段）且 fee != 0 的曲目，一律过滤掉。
    - 已登录时：如果有账号权限能取到完整真实 url 且非试听，则允许返回；若无权限仍过滤。
    - 只能试听30~45秒片段（带 freeTrialInfo/试听限制）的歌曲，绝不能当作可播放曲目返回。
    """
    if not ids:
        return set()
    api = _get_api()
    with _api_lock:
        try:
            urls_data = api.songs_url(ids)
        except Exception:
            return set()
    if not isinstance(urls_data, list):
        return set()

    logged_in = check_is_logged_in()
    playable_ids: set[int] = set()
    for item in urls_data:
        if not isinstance(item, dict):
            continue
        sid = item.get("id") or item.get("song_id")
        if not sid:
            continue
        try:
            sid_int = int(sid)
        except (ValueError, TypeError):
            continue

        url = item.get("url")
        code = item.get("code")
        fee = item.get("fee", 0)
        free_trial = item.get("freeTrialInfo")

        # 核心铁律：url 为空或 code == 404，坚决过滤
        if not url or not str(url).strip() or code == 404:
            continue
        # 凡是带有 freeTrialInfo（试听片段）且 fee != 0 的曲目，一律过滤掉
        # 并且只能试听片段的歌曲绝不当作可播返回
        if free_trial:
            continue
        # 未登录状态下，收费/VIP/专辑曲目坚决不返回
        if not logged_in and fee != 0 and fee not in (0, 8):
            continue

        playable_ids.add(sid_int)
    return playable_ids


def batch_song_details(ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    api = _get_api()
    with _api_lock:
        raw_items = api.songs_detail(ids)
    if not raw_items or not isinstance(raw_items, list):
        return []

    playable_ids = filter_playable_song_ids(ids)

    detail_map: dict[int, dict[str, Any]] = {}
    for item in raw_items:
        if isinstance(item, dict):
            mapped = _map_song_detail(item)
            if mapped["song_id"] in playable_ids:
                detail_map[mapped["song_id"]] = mapped
    return [detail_map[sid] for sid in ids if sid in detail_map]


def song_lyric_pair(song_id: int) -> dict[str, str]:
    api = _get_api()
    with _api_lock:
        raw_lyric = api.song_lyric(song_id)
        raw_tlyric = api.song_tlyric(song_id)
    lyric_str = "\n".join(str(line) for line in raw_lyric) if isinstance(raw_lyric, list) else ""
    tlyric_str = "\n".join(str(line) for line in raw_tlyric) if isinstance(raw_tlyric, list) else ""
    return {"lyric": lyric_str, "tlyric": tlyric_str}
