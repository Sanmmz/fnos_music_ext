#!/bin/bash
# v55 沙箱单元验收：搜索结果「有海报 + 高音质」优先排序
set -u
SB=/tmp/v55t
PY=/home/sanmmz/fnmusic_ext/.venv-proxy/bin/python
REAL=/home/sanmmz/fnmusic_ext/proxy

echo "=== 0) 重建沙箱 $SB ==="
rm -rf "$SB"
mkdir -p "$SB/proxy" "$SB/home/cache" "$SB/home/cover_cache" "$SB/home/online_favorites" "$SB/lib"
for f in "$REAL"/*.py; do
  b=$(basename "$f")
  [ "$b" = "app.py" ] && continue
  cp -f "$f" "$SB/proxy/$b"
done
cp -f /tmp/app_v55.py "$SB/proxy/app.py"
echo "  proxy 文件: $(ls "$SB/proxy" | tr '\n' ' ')"

echo "=== 1) 造假 music.db ==="
"$PY" - <<'PY'
import sqlite3
db = "/tmp/v55t/music.db"
con = sqlite3.connect(db)
con.execute("DROP TABLE IF EXISTS shared_library")
con.execute("CREATE TABLE shared_library (id INTEGER PRIMARY KEY, guid TEXT, path TEXT)")
con.execute("INSERT INTO shared_library (id, guid, path) VALUES (1, ?, ?)",
            ("test-guid-0001", "/tmp/v55t/lib"))
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
cd "$SB" && "$PY" /tmp/_v55_check.py
rc=$?
echo
echo "=== 3) 探针 ==="
grep -aE "\[searchrank\]" "$SB/home/access_probe.log" 2>/dev/null || echo "  （无）"
echo "=== 4) 退出码 $rc ==="
exit $rc
