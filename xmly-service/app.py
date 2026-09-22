"""喜马拉雅（ximalaya）音源服务。

对外 HTTP 端口：容器内 8000，宿主机映射 8774。

对外接口（给 fnmusic-ext proxy 调用）：
  GET  /healthz
  GET  /api/v1/auth/status          当前登录态
  GET  /api/v1/auth/qrcode          生成扫码二维码 -> {qrId, img(base64 png), expires}
  GET  /api/v1/auth/poll?qrId=      轮询扫码结果   -> {status, uid, nickname, raw_ret}
  POST /api/v1/auth/logout          清除已保存 cookie
  GET  /api/v1/search?keyword=&limit=       搜索专辑 -> {albums:[...]}
  GET  /api/v1/album/tracks                专辑的全部声音 -> {tracks:[...], total}
  GET  /api/v1/album/detail                 专辑详情 -> {album:{...}}
  GET  /api/v1/track/url?albumId=&trackId=  单集播放地址 -> {url, ext, quality}

★ 四个关键实现细节（都是 2026-09-22 在本机逐条实测出来的，别再改回去）：
  1. **搜索**：GET /revision/search?core=album&kw=&page=&rows=
     —— 无鉴权、不需要 xm-sign。返回 data.result.response.docs[]，
        字段是 id / title / nickname / cover_path / intro / play / tracks / is_paid。
     ✗ /revision/search/main 会返回 {"reason":"risk invalid","riskLevel":5}。
  2. **专辑详情**：GET /revision/album?albumId=  （mainInfo / anchorInfo / tracksInfo）
  3. **曲目列表**：GET /revision/album/getTracksList?albumId=&pageNum=N&sort=0
     —— 每页固定 30 条（pageSize 无效），翻到空即可。**路径里不要带 v1**，
        /revision/album/v1/getTracksList 会报 "webtk缺失"（要 anti-bot cookie）。
        实测 1024 集专辑能完整拉到 1024 条 = 35 页。
     ✗ mobile.ximalaya.com/mobile/v1/album/track 有约一半专辑返回
       ret=924「亲，该内容因故已下架」，不能作为主路径。
  4. **播放地址**：GET /mobile-playpage/track/v3/baseInfo/{albumId}
     ?device=web&trackId={tid}&trackQualityLevel=1
     -> trackInfo.playUrlList[0].url 是**密文**
     -> AES-128-ECB 解密，key = bytes.fromhex("aaad3e4fd540b0f79dca95606e72bf93")
        base64url 解码（补 "=="），最后按 PKCS7 去填充
     VIP：付费内容的完整音源依赖 cookie 里的 `1&_token`，扫码登录后才有。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import time
from typing import Any

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

# AES-128-ECB 解密（喜马拉雅音频 URL）
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _AES_KEY = bytes.fromhex("aaad3e4fd540b0f79dca95606e72bf93")

    def _decrypt_url(ciphertext: str) -> str:
        if not ciphertext:
            return ""
        try:
            raw = base64.urlsafe_b64decode(ciphertext + "==")
        except Exception:
            return ""
        if not raw or len(raw) % 16:
            return ""
        try:
            dec = Cipher(algorithms.AES(_AES_KEY), modes.ECB()).decryptor()
            data = dec.update(raw) + dec.finalize()
        except Exception:
            return ""
        if not data:
            return ""
        pad = data[-1]
        if 1 <= pad <= 16:
            data = data[:-pad]
        return data.decode("utf-8", "replace").strip()

except Exception:  # pragma: no cover - 依赖缺失时降级为不解密
    def _decrypt_url(ciphertext: str) -> str:
        return ""


logging.basicConfig(level=os.environ.get("XMLY_LOG_LEVEL", "INFO"))
logger = logging.getLogger("xmly")

DATA_DIR = os.environ.get("XMLY_DATA_DIR", "/data")
COOKIE_FILE = os.path.join(DATA_DIR, "xmly_cookie.json")

UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UA_MOBILE = "ting_6.6.72 (iPhone; iOS 16.6; Scale/3.00)"

WEB = "https://www.ximalaya.com"
MOBILE = "https://mobile.ximalaya.com"
PASSPORT = "https://passport.ximalaya.com"

app = FastAPI(title="xmly-service")

# ---------------------------------------------------------------- cookie 持久化


def _load_cookie() -> str:
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return str(data.get("cookie") or "")
        return str(data or "")
    except Exception:
        return ""


def _save_cookie(cookie: str, extra: dict | None = None) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {"cookie": cookie, "ts": time.time()}
        if extra:
            payload.update(extra)
        tmp = COOKIE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, COOKIE_FILE)
    except Exception as e:
        logger.warning("save cookie failed: %s", e)


def _cookie_has_token(cookie: str) -> bool:
    """是否含登录凭证 `1&_token`（VIP 取流的关键字段）。"""
    body = cookie or ""
    return ("1&_token=" in body) or ("1%26_token=" in body) or ("token=" in body and "1&" in body)


# ---------------------------------------------------------------- 请求客户端

_client: httpx.AsyncClient | None = None


def _cli() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=25.0, follow_redirects=True)
    return _client


def _web_headers(with_sign: bool = False) -> dict:
    h = {
        "User-Agent": UA_WEB,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": WEB + "/",
    }
    ck = _load_cookie()
    if ck:
        h["Cookie"] = ck
    if with_sign:
        h["xm-sign"] = _xm_sign_sync()
    return h


def _xm_sign_sync() -> str:
    """xm-sign = md5('himalaya-'+serverTime) + '(' + rand + ')' + serverTime + '(' + rand + ')' + nowTime"""
    st = ""
    try:
        r = httpx.get(WEB + "/revision/time", timeout=8.0,
                      headers={"User-Agent": UA_WEB})
        st = (r.text or "").strip()
    except Exception as e:
        logger.debug("server time failed: %s", e)
    if not st:
        st = str(int(time.time() * 1000))
    now = str(round(time.time() * 1000))
    return (hashlib.md5("himalaya-{}".format(st).encode()).hexdigest()
            + "({})".format(str(round(random.random() * 100)))
            + st
            + "({})".format(str(round(random.random() * 100)))
            + now)


async def _xm_sign() -> str:
    try:
        r = await _cli().get(WEB + "/revision/time", timeout=8.0,
                             headers={"User-Agent": UA_WEB})
        st = (r.text or "").strip() or str(int(time.time() * 1000))
    except Exception:
        st = str(int(time.time() * 1000))
    now = str(round(time.time() * 1000))
    return (hashlib.md5("himalaya-{}".format(st).encode()).hexdigest()
            + "({})".format(str(round(random.random() * 100)))
            + st + "({})".format(str(round(random.random() * 100))) + now)


# ---------------------------------------------------------------- 基础接口


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "xmly", "logged_in": _cookie_has_token(_load_cookie())}


@app.get("/api/v1/auth/status")
async def auth_status():
    ck = _load_cookie()
    has_tok = _cookie_has_token(ck)
    info = {"logged_in": False, "uid": "", "nickname": "", "is_vip": False, "ret": None}
    if ck:
        try:
            r = await _cli().get(WEB + "/revision/main/getCurrentUser",
                                 headers=_web_headers(), timeout=12.0)
            j = r.json()
            info["ret"] = j.get("ret")
            data = j.get("data") or {}
            if j.get("ret") == 200 and data:
                info["logged_in"] = True
                info["uid"] = str(data.get("uid") or "")
                info["nickname"] = str(data.get("nickName") or data.get("nickname") or "")
                info["is_vip"] = bool(data.get("isVip") or data.get("vipType"))
        except Exception as e:
            logger.warning("getCurrentUser failed: %s", e)
            # 拿不到用户信息不代表没登录，cookie 里有 token 就算登录
            info["logged_in"] = has_tok
    return info


@app.get("/api/v1/auth/qrcode")
async def auth_qrcode():
    """生成扫码登录二维码。img 是 base64(png)，前端直接 <img src="data:image/png;base64,...">。"""
    try:
        ck = _load_cookie()
        r = await _cli().get(PASSPORT + "/web/qrCode/gen", params={"level": "L"},
                             headers={"User-Agent": UA_WEB,
                                      "Cookie": ck} if ck else {"User-Agent": UA_WEB},
                             timeout=15.0)
        j = r.json()
        qr_id = str(j.get("qrId") or "")
        img = str(j.get("img") or "")
        if not qr_id:
            return JSONResponse({"ok": False, "msg": "no qrId", "raw": j}, status_code=502)
        logger.info("qrcode generated qrId=%s img_len=%d", qr_id, len(img))
        return {"ok": True, "qrId": qr_id, "img": img, "ret": j.get("ret")}
    except Exception as e:
        logger.warning("qrcode gen failed: %s", e)
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=502)


@app.get("/api/v1/auth/poll")
async def auth_poll(qrId: str = Query("")):
    """轮询扫码状态。

    status: pending(未扫) | scanned(已扫待确认) | success | expired | unknown
    成功时把会话里的 cookie 落盘（/data/xmly_cookie.json）。
    """
    qr_id = (qrId or "").strip()
    if not qr_id:
        return JSONResponse({"ok": False, "msg": "qrId required"}, status_code=400)
    client = _cli()
    try:
        r = await client.get("{}/web/qrCode/check/{}/{}".format(
            PASSPORT, qr_id, int(time.time() * 1000)),
            headers={"User-Agent": UA_WEB}, timeout=20.0)
        j = r.json()
    except Exception as e:
        return {"ok": False, "status": "unknown", "msg": str(e)}

    ret = j.get("ret")
    out: dict[str, Any] = {"ok": True, "status": "unknown", "raw_ret": ret, "raw": j}

    # 有些实现在 body 里带回调 URL，访问它才能完成 set-cookie
    for key in ("url", "redirectUrl", "callbackUrl", "loginUrl"):
        u = j.get(key)
        if isinstance(u, str) and u.startswith("http"):
            try:
                await client.get(u, headers={"User-Agent": UA_WEB}, timeout=15.0)
            except Exception:
                pass

    if ret in (0, 200):
        out["status"] = "success"
        cookie_str = _jar_to_string(client)
        if cookie_str:
            _save_cookie(cookie_str, {"uid": str(j.get("uid") or "")})
        # 兜底：如果 jar 是空的，尝试从 set-cookie 头拼
        if not cookie_str:
            sc = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else []
            if sc:
                cookie_str = "; ".join(sc)
                _save_cookie(cookie_str, {"uid": str(j.get("uid") or "")})
        out["cookie_len"] = len(cookie_str)
        out["has_token"] = _cookie_has_token(cookie_str)
    elif ret == 32000:
        out["status"] = "pending"
    elif ret == 32001:
        out["status"] = "scanned"
    elif ret in (32002, 32003, 32004, 404):
        out["status"] = "expired"
    return out


def _jar_to_string(client: httpx.AsyncClient) -> str:
    try:
        parts = []
        jar = client.cookies.jar
        for c in jar:
            domain = getattr(c, "domain", "") or ""
            if "ximalaya.com" not in domain:
                continue
            parts.append("{}={}".format(c.name, c.value))
        return "; ".join(parts)
    except Exception:
        return ""


@app.post("/api/v1/auth/logout")
async def auth_logout():
    try:
        if os.path.exists(COOKIE_FILE):
            os.remove(COOKIE_FILE)
    except Exception:
        pass
    global _client
    if _client and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None
    return {"ok": True}


# ---------------------------------------------------------------- 搜索 / 专辑 / 取流


@app.get("/api/v1/search")
async def search(keyword: str = Query(""), limit: int = Query(20)):
    """搜索专辑（一部小说 = 一个专辑 = 一个歌单）。

    优先 web 搜索（信息最全）；被风控(risk invalid)时回退移动端搜索。
    """
    kw = (keyword or "").strip()
    if not kw:
        return {"ok": True, "albums": [], "via": "empty"}
    albums = await _search_web(kw, limit)
    via = "web"
    if not albums:
        albums = await _search_mobile(kw, limit)
        via = "mobile"
    logger.info("search kw=%s via=%s n=%d", kw, via, len(albums))
    return {"ok": True, "via": via, "albums": albums}


_warmed = False


async def _ensure_warm() -> None:
    """风控需要站点 cookie：首次请求前先访问首页拿 HWWAFSESID 等。"""
    global _warmed
    if _warmed:
        return
    try:
        await _cli().get(WEB + "/", headers={"User-Agent": UA_WEB}, timeout=15.0)
    except Exception as e:
        logger.debug("warmup failed: %s", e)
    _warmed = True


async def _search_web(kw: str, limit: int) -> list[dict]:
    """★ 实测唯一稳定可用的搜索：GET /revision/search（无鉴权，无需 xm-sign）。

    data.result.response.docs[] 里每个元素就是一个专辑：
      id, title, nickname, cover_path(//开头), intro, play, tracks,
      is_paid, category_title, url(/album/{id})
    """
    await _ensure_warm()
    out: list[dict] = []
    rows = max(1, min(int(limit or 20), 50))
    for attempt in (0, 1):
        try:
            r = await _cli().get(
                WEB + "/revision/search",
                params={"core": "album", "kw": kw, "page": 1, "rows": rows},
                headers={"User-Agent": UA_WEB, "Accept": "application/json"},
                timeout=20.0)
            j = r.json()
        except Exception as e:
            logger.warning("search web failed: %s", e)
            return []
        data = j.get("data") or {}
        if data.get("reason"):
            logger.info("search web risk: reason=%s level=%s retry=%d",
                        data.get("reason"), data.get("riskLevel"), attempt)
            _reset_warm()
            continue
        docs = (((data.get("result") or {}).get("response") or {}).get("docs")
                or [])
        for d in docs:
            if not isinstance(d, dict):
                continue
            aid = d.get("id") or d.get("albumId")
            if not aid:
                continue
            out.append(_album_card(aid, d))
        if out:
            logger.info("search web ok kw=%s n=%d", kw, len(out))
            return out
        # 空结果时用更宽松的检索式再试一次（去 TERM 权重、仅按 title）
        if attempt == 0:
            try:
                r2 = await _cli().get(
                    WEB + "/revision/search",
                    params={"core": "album", "kw": kw, "page": 1, "rows": rows,
                            "condition": "relation", "spellchecker": "true"},
                    headers={"User-Agent": UA_WEB}, timeout=20.0)
                docs2 = ((((r2.json().get("data") or {}).get("result") or {})
                          .get("response") or {}).get("docs") or [])
                for d in docs2:
                    if isinstance(d, dict) and (d.get("id") or d.get("albumId")):
                        out.append(_album_card(d.get("id") or d.get("albumId"), d))
                if out:
                    return out
            except Exception:
                pass
    return []


def _reset_warm() -> None:
    global _warmed
    _warmed = False


async def _search_mobile(kw: str, limit: int) -> list[dict]:
    """兜底：改用 core=all 再取 album 分支。"""
    rows = max(1, min(int(limit or 20), 50))
    try:
        r = await _cli().get(WEB + "/revision/search",
                             params={"core": "all", "kw": kw, "page": 1, "rows": rows},
                             headers={"User-Agent": UA_WEB}, timeout=20.0)
        j = r.json()
    except Exception as e:
        logger.warning("search fallback failed: %s", e)
        return []
    node = ((j.get("data") or {}).get("album")
            or ((j.get("data") or {}).get("result") or {}).get("album") or {})
    docs = node.get("docs") or []
    out = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        aid = d.get("id") or d.get("albumId")
        if aid:
            out.append(_album_card(aid, d))
    if out:
        logger.info("search fallback core=all n=%d", len(out))
    return out


def _abs_cover(cover: str) -> str:
    cover = (cover or "").strip()
    if not cover:
        return ""
    if cover.startswith("//"):
        return "https:" + cover
    if cover.startswith("http"):
        return cover
    return "https://imagev2.xmcdn.com/" + cover.lstrip("/")


def _album_card(aid, d: dict) -> dict:
    """把 /revision/search 的一个 doc 规范化成专辑卡片。"""
    return {
        "albumId": int(aid),
        "title": _strip_html(str(d.get("title") or d.get("richTitle")
                                 or d.get("albumTitle") or d.get("albumName") or "")),
        "author": _strip_html(str(d.get("nickname") or d.get("nickName")
                                  or d.get("anchorName") or d.get("author") or "")),
        "cover": _abs_cover(str(d.get("cover_path") or d.get("coverPath")
                                or d.get("albumCoverPath") or d.get("cover")
                                or d.get("albumCoverUrl") or "")),
        "intro": _strip_html(str(d.get("intro") or d.get("description")
                                 or d.get("albumIntro") or ""))[:500],
        "playCount": int(d.get("play") or d.get("playCount") or d.get("plays") or 0),
        "trackCount": int(d.get("tracks") or d.get("trackCount") or 0),
        "isPaid": bool(d.get("is_paid") or d.get("isPaid")),
        "isVipFree": bool(d.get("isVipFree")),
        "vipType": int(d.get("vipType") or 0),
        "category": _strip_html(str(d.get("category_title") or d.get("categoryName") or "")),
        "updateTime": str(d.get("updated_at") or d.get("updateTime")
                          or d.get("created_at") or ""),
        "url": str(d.get("url") or ("/album/{}".format(aid))),
    }


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return s.replace("&nbsp;", " ").replace("&amp;", "&").strip()


@app.get("/api/v1/album/detail")
async def album_detail(albumId: int = Query(0)):
    """专辑详情。用 web /revision/album（返回 mainInfo + anchorInfo + tracksInfo）。

    ★ 实测规律：web 的 `/revision/album?albumId=` 对任意专辑都 200，
      而移动端的 mobile/v1/album/detail / mobile/v1/album/track 有大约一半
      的专辑会返回 ret=924「已下架」，所以**详情页和列表都统一走 web**。
    """
    aid = int(albumId or 0)
    if aid <= 0:
        return JSONResponse({"ok": False, "msg": "albumId required"}, status_code=400)
    try:
        r = await _cli().get(WEB + "/revision/album",
                             params={"albumId": aid},
                             headers={"User-Agent": UA_WEB,
                                      "Referer": "{}/album/{}".format(WEB, aid)},
                             timeout=20.0)
        j = r.json()
    except Exception as e:
        logger.warning("album detail failed: %s", e)
        return {"ok": False, "msg": str(e)}
    if j.get("ret") != 200:
        return {"ok": False, "msg": str(j.get("msg") or ""), "ret": j.get("ret")}
    d = j.get("data") or {}
    mi = d.get("mainInfo") or {}
    anchor = d.get("anchorInfo") or {}
    crumbs = mi.get("crumbs") or {}
    ti = d.get("tracksInfo") or {}
    return {"ok": True, "album": {
        "albumId": aid,
        "title": _strip_html(str(mi.get("albumTitle") or "")),
        "author": _strip_html(str(anchor.get("anchorName") or "")),
        "authorId": int(anchor.get("anchorId") or 0),
        "cover": _abs_cover(str(mi.get("cover") or "")),
        "intro": _strip_html(str(mi.get("shortIntro") or mi.get("richIntro")
                                 or mi.get("detailRichIntro") or ""))[:800],
        "trackCount": int(ti.get("trackTotalCount") or 0),
        "playCount": int(mi.get("playCount") or 0),
        "subscribeCount": int(mi.get("subscribeCount") or 0),
        "category": _strip_html(str((crumbs or {}).get("categoryTitle") or "")),
        "isPaid": bool(mi.get("isPaid")),
        "isFinished": bool(mi.get("isFinished")),
        "vipType": int(mi.get("vipType") or 0),
        "updateDate": str(mi.get("updateDate") or ""),
        "tags": [str(t) for t in (mi.get("tags") or [])],
    }}


# v79：页数不再写死 60 —— 《诡秘之主》2070 集需要 69 页，60 页上限直接砍掉 270 集
# （歌单里存下来的 track_count 就停在 1800 = 60*30）。现在按 trackTotalCount 算页数，
# 并且第 2 页起并发抓：串行 69 页 9.9s，并发 8 只要 1.7s（实测不触发风控）。
_TRACK_PAGE_SIZE = 30
_TRACK_MAX_PAGES = int(os.environ.get("XMLY_TRACK_MAX_PAGES", "200"))      # 200*30 = 6000 集
_TRACK_CONCURRENCY = max(1, int(os.environ.get("XMLY_TRACK_CONCURRENCY", "8")))


async def _fetch_tracks_page(aid: int, pn: int, sort: int, hd: dict, sem):
    """抓单页；失败或非 200 一律返回 (pn, None)，由调用方决定怎么兜底。"""
    async with sem:
        for attempt in (1, 2):
            j = None
            try:
                r = await _cli().get(WEB + "/revision/album/getTracksList",
                                     params={"albumId": aid, "pageNum": pn, "sort": sort},
                                     headers=hd, timeout=25.0)
                j = r.json()
            except Exception as e:  # noqa: BLE001
                logger.warning("album tracks page failed aid=%s page=%d try=%d: %s",
                               aid, pn, attempt, e)
            if isinstance(j, dict) and j.get("ret") == 200:
                return pn, j
            if attempt == 2:
                logger.info("album tracks stop aid=%s page=%d ret=%s",
                            aid, pn, (j or {}).get("ret") if isinstance(j, dict) else "exc")
                return pn, None
            await asyncio.sleep(0.4)
    return pn, None


@app.get("/api/v1/album/tracks")
async def album_tracks(albumId: int = Query(0), sort: int = Query(0),
                       max_pages: int = Query(0)):
    """专辑的全部声音（= 小说的全部章节）。

    ★ 唯一实测能匿名拉满的接口：GET /revision/album/getTracksList?albumId=&pageNum=N
      - 每页固定 30 条（传 pageSize 无效），翻到空为止
      - 路径是 `/revision/album/getTracksList`，**不是** `/revision/album/v1/getTracksList`
        （v1 版要 webtk cookie）

    ★ v79 两条关键改动（修《诡秘之主》2070 集只出 1800 集）：
      1. **页数按 trackTotalCount 算**，不再写死 60。`max_pages <= 0` = 自动；
         `> 0` = 显式上限（仍受 `XMLY_TRACK_MAX_PAGES` 硬顶保护）。
      2. **第 2 页起并发抓**（默认 8 并发）。2070 集：串行 9.9s → 并发 1.7s。
         结果仍按 pageNum 升序拼装保证章节顺序，遇到空页 / 失败页即停。
    """
    aid = int(albumId or 0)
    if aid <= 0:
        return JSONResponse({"ok": False, "msg": "albumId required"}, status_code=400)
    hd = {"User-Agent": UA_WEB, "Referer": "{}/album/{}".format(WEB, aid)}

    # ---- 第 1 页：拿 trackTotalCount，据此决定要翻多少页 ----
    _, j1 = await _fetch_tracks_page(aid, 1, sort, hd, asyncio.Semaphore(1))
    if j1 is None:
        logger.warning("album tracks aid=%s first page failed", aid)
        return {"ok": True, "tracks": [], "total": 0}
    d1 = j1.get("data") or {}
    total = int(d1.get("trackTotalCount") or 0)
    if total:
        need = -(-total // _TRACK_PAGE_SIZE) + 1     # 多翻 1 页确认结束
    else:
        need = 60                                     # 拿不到总数时退化为旧行为
    try:
        _cap = int(max_pages or 0)
    except (TypeError, ValueError):
        _cap = 0
    if _cap > 0:
        need = min(need, _cap)
    need = max(1, min(need, _TRACK_MAX_PAGES))

    pages: dict[int, dict] = {1: j1}
    if need > 1:
        sem = asyncio.Semaphore(_TRACK_CONCURRENCY)
        got = await asyncio.gather(*[_fetch_tracks_page(aid, pn, sort, hd, sem)
                                     for pn in range(2, need + 1)])
        for pn, j in got:
            if j is not None:
                pages[pn] = j

    tracks: list[dict] = []
    cover_cache = ""
    for pn in range(1, need + 1):
        j = pages.get(pn)
        if j is None:
            break
        d = j.get("data") or {}
        lst = d.get("tracks") or []
        if not lst:
            break
        for t in lst:
            if not isinstance(t, dict):
                continue
            tid = t.get("trackId")
            if not tid:
                continue
            if not cover_cache:
                cover_cache = _abs_cover(str(t.get("albumCoverPath") or ""))
            tracks.append({
                "trackId": int(tid),
                "title": _strip_html(str(t.get("title") or "")),
                "albumId": aid,
                "albumTitle": _strip_html(str(t.get("albumTitle") or "")),
                "duration": int(t.get("duration") or t.get("length") or 0),
                "cover": cover_cache,
                "isPaid": bool(t.get("isPaid")),
                "isVipFirst": bool(t.get("isVipFirst")),
                "playCount": int(t.get("playCount") or 0),
                "createDate": str(t.get("createDateFormat") or ""),
                "anchorName": _strip_html(str(t.get("anchorName") or "")),
                "index": int(t.get("index") or len(tracks) + 1),
            })
        if total and len(tracks) >= total:
            break
    logger.info("album tracks aid=%s -> n=%d total=%d pages=%d",
                aid, len(tracks), total, need)
    return {"ok": True, "tracks": tracks, "total": total or len(tracks)}


_url_cache: dict[str, tuple[float, dict]] = {}
_URL_TTL = float(os.environ.get("XMLY_URL_TTL", "1800"))


@app.get("/api/v1/track/url")
async def track_url(albumId: int = Query(0), trackId: int = Query(0)):
    """单集播放地址（已解密）。VIP 依赖 cookie 里的 1&_token。"""
    aid, tid = int(albumId or 0), int(trackId or 0)
    if aid <= 0 or tid <= 0:
        return JSONResponse({"ok": False, "msg": "albumId & trackId required"}, status_code=400)

    ck = _load_cookie()
    cache_key = "{}:{}:{}".format(aid, tid, "v" if ck else "a")
    hit = _url_cache.get(cache_key)
    if hit and time.time() - hit[0] < _URL_TTL:
        return dict(hit[1], cached=True, ok=True)

    h = {"User-Agent": UA_WEB, "Accept": "application/json"}
    if ck:
        h["Cookie"] = ck
    try:
        r = await _cli().get("{}/mobile-playpage/track/v3/baseInfo/{}".format(WEB, aid),
                             params={"device": "web", "trackId": tid, "trackQualityLevel": 1},
                             headers=h, timeout=25.0)
        j = r.json()
    except Exception as e:
        logger.warning("track url failed aid=%s tid=%s: %s", aid, tid, e)
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=502)

    if j.get("ret") not in (0, None):
        return {"ok": False, "msg": str(j.get("msg") or ""), "ret": j.get("ret")}

    ti = j.get("trackInfo") or {}
    url_list = ti.get("playUrlList") or []
    if not url_list:
        return {"ok": False, "msg": "no playUrlList", "ret": j.get("ret")}

    # playUrlList 按清晰度从高到低，取第一个可用
    chosen = None
    for cand in url_list:
        crypted = str((cand or {}).get("url") or "")
        if not crypted:
            continue
        dec = _decrypt_url(crypted)
        if dec.startswith("http"):
            chosen = (dec, cand)
            break
    if not chosen:
        return {"ok": False, "msg": "decrypt failed"}

    url, meta = chosen
    ext = "m4a"
    low = url.lower().split("?")[0]
    for e in ("m4a", "mp3", "flac", "aac", "wav"):
        if low.endswith("." + e):
            ext = e
            break
    out = {
        "url": url,
        "ext": ext,
        "qualityLevel": (meta or {}).get("qualityLevel"),
        "payType": ti.get("paidType"),
        "isPaid": bool(ti.get("paidType")),
        "duration": int(ti.get("duration") or 0),
        "title": _strip_html(str(ti.get("title") or "")),
        "cover": str(ti.get("coverLarge") or ti.get("coverMiddle") or ""),
        "authorized": bool(ck),
    }
    _url_cache[cache_key] = (time.time(), out)
    if len(_url_cache) > 500:
        now = time.time()
        for k in [k for k, v in _url_cache.items() if now - v[0] > _URL_TTL]:
            _url_cache.pop(k, None)
    return dict(out, ok=True)


@app.on_event("shutdown")
async def _shutdown():
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
