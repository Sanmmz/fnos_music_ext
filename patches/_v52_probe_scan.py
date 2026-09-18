import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

LIB = "/vol02/<vol-id>/music"
SRC = os.path.join(LIB, "黄霄雲 _ 刘端端 - 空心 (Live版).mp3")
DST = os.path.join(LIB, "zz-v52-probe.mp3")
DB = "/usr/local/apps/@appdata/trim.music/db/music.db"
GUID = "<lib-guid>"
UP = "/var/run/trim_music_upstream.socket"
TOK = sys.argv[1]


def rows_for(name):
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    r = con.execute(
        "SELECT id, path, is_physical_file_deleted FROM audio_file WHERE path LIKE ?",
        ("%" + name + "%",),
    ).fetchall()
    total = con.execute("SELECT COUNT(*) FROM audio_file").fetchone()[0]
    alive = con.execute(
        "SELECT COUNT(*) FROM audio_file WHERE is_physical_file_deleted = 0"
    ).fetchone()[0]
    con.close()
    return r, total, alive


def scan(tag):
    out = subprocess.run(
        ["curl", "-s", "--unix-socket", UP, "-m", "30", "-X", "POST",
         "-H", "cookie: music-token=" + TOK,
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"guid": GUID}),
         "http://unix/music/api/v1/shared-library/scan"],
        capture_output=True, text=True,
    )
    print("  [%s] scan http_body=%s" % (tag, (out.stdout or "").strip()[:160]))


print("=== 0) 基线 ===")
print("  源文件大小:", os.path.getsize(SRC) if os.path.exists(SRC) else "不存在")
r, total, alive = rows_for("zz-v52-probe")
print("  探针行=%s   audio_file 总数=%d  未删除=%d" % (r, total, alive))

print("=== 1) 造一个探针文件（模拟「下载落盘」）===")
if os.path.exists(DST):
    os.remove(DST)
shutil.copy2(SRC, DST)
print("  已创建:", DST, os.path.getsize(DST))

print("=== 2) 带鉴权扫库（同插件 _call_library_scan 的路径与凭据）===")
t0 = time.time()
scan("after-create")
time.sleep(12)
r, total, alive = rows_for("zz-v52-probe")
print("  → 探针行=%s   audio_file 总数=%d  未删除=%d  （耗时 %.0fs）" % (r, total, alive, time.time() - t0))

print("=== 3) 删除探针文件（模拟「取消收藏删除」）===")
os.remove(DST)
print("  已删除:", os.path.exists(DST))

print("=== 4) 再次带鉴权扫库 ===")
t0 = time.time()
scan("after-delete")
time.sleep(12)
r, total, alive = rows_for("zz-v52-probe")
print("  → 探针行=%s   audio_file 总数=%d  未删除=%d  （耗时 %.0fs）" % (r, total, alive, time.time() - t0))
print()
print("结论：删除后若探针行 is_physical_file_deleted 仍=0，说明「扫描不会自动清理已删文件」。")
