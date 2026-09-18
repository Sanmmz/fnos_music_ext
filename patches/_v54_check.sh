#!/bin/bash
# v54 沙箱单元验收：v52/v53 全部检查 + 歌词「永远跟着音频走」
set -u
SB=/tmp/v54t
PY=/home/<user>/fnmusic_ext/.venv-proxy/bin/python
REAL=/home/<user>/fnmusic_ext/proxy

echo "=== 0) 重建沙箱 $SB ==="
rm -rf "$SB"
mkdir -p "$SB/proxy" "$SB/home/cache" "$SB/home/online_favorites" "$SB/lib" "$SB/outside"
for f in "$REAL"/*.py; do
  b=$(basename "$f")
  [ "$b" = "app.py" ] && continue
  cp -f "$f" "$SB/proxy/$b"
done
cp -f /tmp/app_v54.py "$SB/proxy/app.py"
echo "  proxy 文件: $(ls "$SB/proxy" | tr '\n' ' ')"

echo "=== 1) 造假 music.db ==="
"$PY" - <<'PY'
import sqlite3
db = "/tmp/v54t/music.db"
con = sqlite3.connect(db)
con.execute("DROP TABLE IF EXISTS shared_library")
con.execute("CREATE TABLE shared_library (id INTEGER PRIMARY KEY, guid TEXT, path TEXT)")
con.execute("INSERT INTO shared_library (id, guid, path) VALUES (1, ?, ?)",
            ("test-guid-0001", "/tmp/v54t/lib"))
con.commit()
print("  ", con.execute("SELECT guid, path FROM shared_library").fetchall())
con.close()
PY

echo "=== 2) 跑单元验收 ==="
export FNMUSIC_HOME="$SB/home"
export FNMUSIC_LIBRARY_DIR=""
export FNMUSIC_MUSIC_DB="$SB/music.db"
export FNMUSIC_TEE_SAVE_ENABLED="true"
export FNMUSIC_TEE_FAVORITES_ONLY="true"
export FNMUSIC_FAV_DL_ON_FAVORITE="true"
export FNMUSIC_FAV_DL_DELETE_ON_UNFAV="true"
cd "$SB" && "$PY" /tmp/_v54_check.py
rc=$?
echo
echo "=== 3) 自愈/提升探针 ==="
grep -aE "\[lyricgc\]|\[lyricpromo\]|\[lyricshadow\]" "$SB/home/access_probe.log" 2>/dev/null || echo "  （无）"
echo "=== 4) 退出码 $rc ==="
exit $rc
