import sqlite3, os, subprocess, time, json, sys

DB = "/usr/local/apps/@appdata/trim.music/db/music.db"
LIB = "/vol02/<vol-id>/music"
TARGET = "还是会想你"
UP = "/var/run/trim_music_upstream.socket"
GUID = "<lib-guid>"
TOK = sys.argv[1]


def state(tag):
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    total = con.execute("SELECT COUNT(*) FROM audio_file").fetchone()[0]
    alive = con.execute("SELECT COUNT(*) FROM audio_file WHERE is_physical_file_deleted=0").fetchone()[0]
    r = con.execute(
        "SELECT id, path, size, is_physical_file_deleted FROM audio_file WHERE path LIKE ?",
        ("%" + TARGET + "%",),
    ).fetchall()
    con.close()
    print("  [%s] total=%d alive=%d target_rows=%s" % (tag, total, alive, r))
    return r


print("=== 现状复查（距 favdl 已过数分钟）===")
state("now")

dst = [f for f in os.listdir(LIB) if TARGET in f]
print("  曲库中的目标文件:", dst)
for f in dst:
    p = os.path.join(LIB, f)
    st = os.stat(p)
    print("    size=%d mtime=%s" % (st.st_size, time.strftime("%H:%M:%S", time.localtime(st.st_mtime))))

print("=== 直接再扫一次（文件已稳定）===")
out = subprocess.run(
    ["curl", "-s", "--unix-socket", UP, "-m", "30", "-X", "POST",
     "-H", "cookie: music-token=" + TOK, "-H", "Content-Type: application/json",
     "-d", json.dumps({"guid": GUID}), "http://unix/music/api/v1/shared-library/scan"],
    capture_output=True, text=True)
print("  scan body:", out.stdout.strip()[:160])
for i in range(6):
    time.sleep(5)
    r = state("t+%ds" % ((i + 1) * 5))
    if r:
        break
