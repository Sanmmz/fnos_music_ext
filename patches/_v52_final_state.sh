#!/bin/bash
# 最终状态核对：App 视角能看到什么 + 清理测试残留
set -u
SOCK=/var/run/trim_music.socket
LIB=/vol02/<vol-id>/music
PY=/home/<user>/fnmusic_ext/.venv-proxy/bin/python
TOK="${1:?}"
GUID="<lib-guid>"

echo "=== A) DB 口径 ==="
sudo $PY -c "
import sqlite3
con=sqlite3.connect('file:/usr/local/apps/@appdata/trim.music/db/music.db?mode=ro',uri=True)
q=lambda s: con.execute(s).fetchone()[0]
print('  audio_file 总=%d  未标删=%d' % (q('SELECT COUNT(*) FROM audio_file'), q('SELECT COUNT(*) FROM audio_file WHERE is_physical_file_deleted=0')))
print('  track      总=%d  未标删=%d' % (q('SELECT COUNT(*) FROM track'), q('SELECT COUNT(*) FROM track WHERE is_audio_file_deleted=0')))
print('  track 未标删明细:')
for r in con.execute('SELECT id,guid,title FROM track WHERE is_audio_file_deleted=0'):
    print('    ', r)
con.close()"

echo
echo "=== B) App 接口口径（真实 token 走代理）==="
for U in \
  "/music/api/v1/track/list?sharedLibraryGUID=$GUID&offset=0&limit=100" \
  "/music/api/v1/shared-library/list" \
  "/music/api/v1/track/list?limit=100&offset=0" ; do
  echo "  --- $U"
  curl -s --unix-socket $SOCK -m 12 -H "cookie: music-token=$TOK" "http://localhost$U" \
    | head -c 700
  echo
done

echo
echo "=== C) 曲库目录 ==="
sudo ls -la "$LIB"

echo
echo "=== D) 清理本实验留下的一次性残片 ==="
for f in "$LIB"/online_netease_*.part; do
  [ -e "$f" ] || continue
  echo "  删除孤儿 .part: $f"
  sudo rm -f "$f"
done
echo "  曲库文件数=$(sudo ls "$LIB" | wc -l)"
