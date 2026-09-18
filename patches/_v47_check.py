"""v47 验收：播放历史删除（用官方前端真实键名 trackGUIDs）。

非破坏性：先备份 play_history JSON，测完原样还原。
"""
import glob
import json
import os
import sqlite3
import subprocess

UP = "/var/run/trim_music_upstream.socket"
PX = "/var/run/trim_music.socket"
PH_DIR = "/home/<user>/fnmusic_ext/play_history"
BAK = "/tmp/_ph_backup_bytes_v47.json"

con = sqlite3.connect("/tmp/_dbcopy/music.db")
tok = (con.execute("select token from user_token limit 1").fetchone() or [""])[0]
con.close()

paths = [p for p in glob.glob(os.path.join(PH_DIR, "*.json")) if ".bak" not in p]
assert len(paths) == 1, paths
PH = paths[0]
print("play_history 文件:", PH)

with open(PH, "rb") as f:
    original = f.read()
with open(BAK, "wb") as f:
    f.write(original)


def items():
    try:
        return json.loads(open(PH, encoding="utf-8").read()).get("items") or []
    except Exception as e:
        return [{"guid": "ERR:%s" % e}]


def online_items():
    return [it for it in items() if str(it.get("guid") or "").startswith("online:")]


def post(sock, body, ctype="application/json"):
    cmd = ["curl", "-s", "--max-time", "8", "--unix-socket", sock,
           "-H", "Authorization: " + tok, "-H", "Content-Type: " + ctype,
           "--data-binary", json.dumps(body),
           "http://localhost/music/api/v1/play-history/delete"]
    return (subprocess.run(cmd, capture_output=True, text=True).stdout or "").strip()[:110]


def hist(sock):
    cmd = ["curl", "-s", "--max-time", "8", "--unix-socket", sock,
           "-H", "Authorization: " + tok,
           "http://localhost/music/api/v1/play-history/list?page=1&size=200"]
    try:
        d = json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout or "{}")
        return len((d.get("data") or {}).get("list") or [])
    except Exception:
        return None


BASE_ONLINE = len(online_items())
BASE_PX, BASE_UP = hist(PX), hist(UP)
VICTIM = online_items()[0]["guid"]
NATIVE = "0123456789abcdef0123456789abcdef"

print("\n【0】基线")
print("   在线条目=%d  经代理历史=%s  上游历史=%s" % (BASE_ONLINE, BASE_PX, BASE_UP))
print("   受害条目: %s" % VICTIM)

print("\n【1】核心用例：真机键名 trackGUIDs + 在线 guid")
print("   请求 ->", post(PX, {"trackGUIDs": [VICTIM]}))
print("   在线条目=%d（应 %d）  经代理=%s（应 %s）  上游=%s（应不变）"
      % (len(online_items()), BASE_ONLINE - 1, hist(PX), (BASE_PX or 0) - 1, hist(UP)))
print("   残留: %s" % ("已删除" if not any(
    str(x.get("guid")) == VICTIM for x in online_items()) else "!! 仍在 !!"))

print("\n【2】原生 guid 仍透传上游（trackGUIDs）")
print("   请求 ->", post(PX, {"trackGUIDs": [NATIVE]}))

print("\n【3】混合：在线 + 原生（在线删掉、原生透传）")
print("   请求 ->", post(PX, {"trackGUIDs": [VICTIM, NATIVE]}))

print("\n【4】大小写不敏感：TRACKGUIDS / trackguids")
print("   TRACKGUIDS  ->", post(PX, {"TRACKGUIDS": [VICTIM]}))
print("   trackguids  ->", post(PX, {"trackguids": [VICTIM]}))

print("\n【5】含 guid 的未知键名（兜底匹配）")
print("   请求 ->", post(PX, {"historyGuidList": [VICTIM]}))

print("\n【6】反例：字段里没有 guid（应原样转发，不误判）")
print("   请求 ->", post(PX, {"x": 1}))

print("\n【7】还原备份并复核")
with open(PH, "wb") as f:
    f.write(original)
print("   在线条目=%d（应 %d）  经代理=%s（应 %s）"
      % (len(online_items()), BASE_ONLINE, hist(PX), BASE_PX))

print("\n【8】健康检查（确认 v45 直连通道未回归）")
h = subprocess.run(["curl", "-s", "--max-time", "5", "--unix-socket", PX,
                    "http://localhost/_ext/healthz"], capture_output=True, text=True).stdout
try:
    hd = json.loads(h)
    print("   status=%s netease_direct=%s" % (hd.get("status"), json.dumps(
        hd.get("netease_direct"), ensure_ascii=False)))
except Exception:
    print("   raw:", h[:200])
