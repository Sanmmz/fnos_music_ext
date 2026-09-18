#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v54 真机端到端：复现「收藏后音频进曲库、歌词也必须贴身」。

复现路径（= 用户实际操作）：
  1) 先播放一次该在线曲目 → App 拉歌词 → 歌词落 cache/<safe>.lrc（收藏前就有缓存副本）
  2) 在 App 里收藏 → 触发服务端整轨下载 → 音频落曲库
  3) 断言：曲库音频旁边必须有**同名 .lrc**；cache 影子副本应被清掉
  4) 清理：取消收藏 → 文件消失、收藏列表恢复原样
"""
import os
import json
import time
import sqlite3
import subprocess
import sys

HOME = os.environ.get("FNMUSIC_HOME", "/home/<user>/fnmusic_ext")
LIB = os.environ.get("FNMUSIC_LIBRARY_DIR", "/vol02/<vol-id>/music")
SOCK = os.environ.get("FNMUSIC_SOCK", "/var/run/trim_music.socket")
DB = os.environ.get("FNMUSIC_MUSIC_DB",
                    "/usr/local/apps/@appdata/trim.music/db/music.db")
CACHE = os.path.join(HOME, "cache")
FAVDIR = os.path.join(HOME, "online_favorites")
PROBE = os.path.join(HOME, "access_probe.log")
GUID = "online:netease:2163210456"
SAFE = GUID.replace(":", "_")
AUDIO_EXTS = ("mp3", "flac", "m4a", "aac", "wav", "ogg", "opus")

fails = []


def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  >> " + str(extra)) if not cond else ""))
    if not cond:
        fails.append(name)


def curl(method, path, data=None, tok=None, timeout=180):
    cmd = ["curl", "-s", "-X", method, "--unix-socket", SOCK, "-m", str(timeout),
           "http://localhost" + path]
    if tok:
        cmd += ["-H", "cookie: music-token=" + tok]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(data)]
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def token():
    con = sqlite3.connect(DB)
    row = con.execute(
        "SELECT token FROM user_token WHERE expired_at > datetime('now') "
        "ORDER BY is_auth_login DESC, id LIMIT 1").fetchone()
    con.close()
    return row[0] if row else ""


def favorites():
    out = {}
    for n in sorted(os.listdir(FAVDIR)):
        if not n.endswith(".json"):
            continue
        try:
            obj = json.load(open(os.path.join(FAVDIR, n), encoding="utf-8"))
            out[n] = [i.get("guid") for i in (obj.get("items") or [])]
        except Exception:
            pass
    return out


def mine():
    """曲库中本曲目的音频 / 歌词文件名。"""
    files = sorted(os.listdir(LIB))
    aud = [f for f in files if "加木" in f and f.rsplit(".", 1)[-1].lower() in AUDIO_EXTS]
    lrc = [f for f in files if "加木" in f and f.endswith(".lrc")]
    return aud, lrc


def probe_since(n=12):
    try:
        lines = open(PROBE, encoding="utf-8", errors="replace").read().splitlines()
    except Exception:
        return []
    return [x for x in lines[-400:]
            if any(k in x for k in ("[favdl]", "[favdel]", "[lyricpromo]",
                                    "[lyricshadow]", "[lyricgc]", "[scanreq]"))][-n:]


print("=== 0) 前置 ===")
tok = token()
ck("拿到可用 token", bool(tok), tok[:8])
fav0 = favorites()
print("  收藏(前):", fav0)
aud0, lrc0 = mine()
print("  曲库该曲目(前): audio=%s lrc=%s" % (aud0, lrc0))
ck("前置：目标曲目尚未在曲库", not aud0, aud0)

print("=== 1) 先播一次歌词（模拟「收藏前播放过」）===")
r = curl("GET", "/music/api/v1/lyric/list?guid=" + GUID)
clrc = os.path.join(CACHE, SAFE + ".lrc")
ck("歌词接口返回内容", len(r) > 200, len(r))
ck("cache 里已生成歌词副本（本次回归的触发条件）", os.path.isfile(clrc), clrc)
leye = os.path.join(CACHE, SAFE + ".lyricref")
print("  影子前置: lrc=%s lyricref=%s" % (os.path.exists(clrc), os.path.exists(leye)))

print("=== 2) 收藏（触发整轨下载）===")
r = curl("POST", "/music/api/v1/favorite-track/create",
         data={"trackGUID": GUID}, tok=tok)
print("  resp:", r[:220])
ck("收藏接口返回 code=0", '"code":0' in r or '"code": 0' in r, r[:200])

print("=== 3) 等待整轨落盘（最多 240s）===")
deadline = time.time() + 240
aud, lrc = [], []
while time.time() < deadline:
    aud, lrc = mine()
    if aud:
        time.sleep(8)          # 多等一拍，让歌词补位完成
        aud, lrc = mine()
        break
    time.sleep(5)
ck("音频已落曲库", bool(aud), aud)
print("  audio=%s" % aud)
print("  lrc  =%s" % lrc)

print("=== 4) ★ 核心断言：歌词必须贴身 ===")
ck("曲库出现了歌词 sidecar", bool(lrc), lrc)
if aud and lrc:
    astem = os.path.splitext(aud[0])[0]
    ck("歌词与音频**同名**（同词干）", os.path.splitext(lrc[0])[0] == astem,
       "audio=%r lrc=%r" % (astem, os.path.splitext(lrc[0])[0]))
    p = os.path.join(LIB, lrc[0])
    ck("歌词文件非空", os.path.getsize(p) > 50, os.path.getsize(p))
ck("cache 影子副本已被清掉（或已提升）", not os.path.exists(clrc), clrc)
print("  cache 影子现状: lrc=%s" % os.path.exists(clrc))
print("  .ref      : %s" % open(os.path.join(CACHE, SAFE + ".ref"), encoding="utf-8").read().strip()
      if os.path.exists(os.path.join(CACHE, SAFE + ".ref")) else "  .ref      : (无)")
print("  .lyricref : %s" % open(leye, encoding="utf-8").read().strip()
      if os.path.exists(leye) else "  .lyricref : (无)")

print("=== 5) 探针 ===")
for line in probe_since():
    print("  " + line)

print("=== 6) 清理：取消收藏 ===")
r = curl("POST", "/music/api/v1/favorite-track/delete",
         data={"trackGUID": GUID, "guid": GUID}, tok=tok)
print("  resp:", r[:160])
deadline = time.time() + 120
while time.time() < deadline:
    aud2, lrc2 = mine()
    if not aud2 and not lrc2:
        break
    time.sleep(4)
aud2, lrc2 = mine()
ck("取消收藏后音频已删除", not aud2, aud2)
ck("取消收藏后歌词已删除", not lrc2, lrc2)
fav1 = favorites()
ck("收藏列表恢复到原样", fav1 == fav0, "before=%s after=%s" % (fav0, fav1))

print("=== 7) 用户原有收藏未受影响 ===")
aud3, lrc3 = [], []
files = sorted(os.listdir(LIB))
for f in files:
    if "空心" in f:
        (lrc3 if f.endswith(".lrc") else aud3).append(f)
ck("原有收藏《空心》音频在", bool(aud3), aud3)
ck("原有收藏《空心》歌词贴身在", bool(lrc3), lrc3)

print()
if fails:
    print("FAILED %d: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL v54 E2E CHECKS PASSED")
