#!/bin/bash
# v52 E2E 闭环收尾：取消收藏 → 插件自动删文件 → 自动扫库 → DB 行标记删除 + 恢复基线
set -u
LIB=/vol02/<vol-id>/music
PROBE=/home/<user>/fnmusic_ext/access_probe.log
INFO=/usr/local/apps/@appdata/trim.music/log/info.log
FAV=/home/<user>/fnmusic_ext/online_favorites/43a23a9b970a4cb1bb65e1169f503c74.json
SOCK=/var/run/trim_music.socket
PY=/home/<user>/fnmusic_ext/.venv-proxy/bin/python
TOK="${1:?}"
G="online:netease:1827600686"

show() {
  sudo $PY -c "
import sqlite3
con=sqlite3.connect('file:/usr/local/apps/@appdata/trim.music/db/music.db?mode=ro',uri=True)
print('   audio_file 总数=%d 未删除=%d' % (
 con.execute('SELECT COUNT(*) FROM audio_file').fetchone()[0],
 con.execute('SELECT COUNT(*) FROM audio_file WHERE is_physical_file_deleted=0').fetchone()[0]))
for r in con.execute(\"SELECT id,path,is_physical_file_deleted FROM audio_file WHERE id=67\"):
    print('   行67:', r)
con.close()"
}

echo "=== 1) 取消收藏前 ==="
show
echo "  曲库文件数=$(sudo ls "$LIB" | wc -l)"
P0=$(sudo wc -c < "$PROBE")

echo
echo "=== 2) 调用真实取消收藏接口（带真实 token）==="
curl -s --unix-socket $SOCK -m 20 -X POST \
  -H "cookie: music-token=$TOK" -H "Content-Type: application/json" \
  -d "{\"trackGUID\":\"$G\",\"guid\":\"$G\"}" \
  http://localhost/music/api/v1/favorite-track/delete -w "  http=%{http_code}\n"

echo "  等待插件自动删文件 + 自动扫库…"
sleep 20

echo
echo "  --- 新增探针 ---"
sudo tail -c 3000 "$PROBE" | grep -aE "\[favdel\]|\[scanreq\]" | tail -6

echo
echo "=== 3) 结果 ==="
show
echo "  曲库文件数=$(sudo ls "$LIB" | wc -l)  （应回到 4）"
sudo ls -l "$LIB"
echo "  目标文件是否已从曲库移除: $([ -e "$LIB/林达浪 _ h3R3 - 还是会想你.mp3" ] && echo '✗ 仍在' || echo '✓ 已移除')"
echo "  cache/.ref 残留: $(sudo ls /home/<user>/fnmusic_ext/cache/ | grep -a 1827600686 || echo '（无）')"

echo
echo "=== 4) 恢复收藏文件到实验前状态 ==="
sudo cp -f /tmp/fav.bak.v52e2e "$FAV"
sudo $PY -c "
import json
d=json.load(open('$FAV')); items=d.get('items') if isinstance(d,dict) else d
print('  收藏集合:', [it.get('guid') for it in items])"

echo
echo "=== 5) 探针尾部总览 ==="
sudo tail -c 3000 "$PROBE" | grep -aE "\[favdl\]|\[favdel\]|\[scanreq\]" | tail -12
