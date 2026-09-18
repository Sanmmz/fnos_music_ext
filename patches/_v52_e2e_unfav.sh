#!/bin/bash
# v52 E2E：真实「取消收藏 → 删除文件 → 插件自动触发扫库」
# 说明：构造一个本插件落盘过的文件（.ref 映射 + 曲库文件），guid 不在收藏里，
#       然后调用 App 真实使用的 favorite-track/delete 接口（带真实 token）。
#       预期：删除文件 → 探针出 [scanreq] queue reason=unfav → [scanreq] call ok=True
#       因为文件在扫描前就已删除，所以不会在 DB 里留下新的 audio_file 行（零污染）。
set -u
LIB=/vol02/<vol-id>/music
PROBE=/home/<user>/fnmusic_ext/access_probe.log
INFO=/usr/local/apps/@appdata/trim.music/log/info.log
CACHE=/home/<user>/fnmusic_ext/cache
SOCK=/var/run/trim_music.socket
TOK="${1:?用法: $0 <music-token>}"
G="online:lx:kw:9000001"
SAFE="online_lx_kw_9000001"
F="$LIB/zz-v52-probe2.mp3"

echo "=== 0) 基线 ==="
P0=$(sudo wc -c < "$PROBE")
I0=$(sudo cat "$INFO" | wc -l)
echo "  探针字节=$P0   info.log 行数=$I0"
sudo ls -l "$LIB" | grep -a "zz-v52-probe2" || echo "  （无同名残留）"

echo
echo "=== 1) 造「已落盘」状态：曲库文件 + .ref 映射 ==="
sudo dd if=/dev/urandom of="$F" bs=1024 count=4 status=none
sudo bash -c "printf '%s' '$LIB/zz-v52-probe2' > '$CACHE/$SAFE.ref'"
sudo ls -l "$F" "$CACHE/$SAFE.ref"

echo
echo "=== 2) 鉴权预检（user/me 必须 200）==="
curl -s -o /dev/null -w "  user/me http=%{http_code}\n" --unix-socket $SOCK -m 10 \
  -H "cookie: music-token=$TOK" http://localhost/music/api/v1/user/me

echo
echo "=== 3) 调用真实取消收藏接口（App 同款路径 + 真实 token）==="
curl -s --unix-socket $SOCK -m 20 -X POST \
  -H "cookie: music-token=$TOK" -H "Content-Type: application/json" \
  -d "{\"trackGUID\":\"$G\",\"guid\":\"$G\"}" \
  http://localhost/music/api/v1/favorite-track/delete -w "\n  http=%{http_code}\n"

echo
echo "=== 4) 等待插件自动扫库（delay=3s + 扫库耗时）==="
sleep 14

echo "  --- 新增探针（[favdel] / [scanreq]）---"
sudo tail -c 4000 "$PROBE" | grep -aE "\[favdel\]|\[scanreq\]" | tail -10

echo
echo "  --- 上游 info.log 新增扫描行 ---"
sudo tail -n +"$((I0+1))" "$INFO" | grep -aE "scanner\[|sharedLibrary" | head -12
echo "  （新增行数: $(sudo tail -n +"$((I0+1))" "$INFO" | wc -l)）"

echo
echo "=== 5) 结果核对 ==="
[ -e "$F" ] && echo "  ✗ 曲库文件仍在" || echo "  ✓ 曲库文件已删除"
[ -e "$CACHE/$SAFE.ref" ] && echo "  ✗ .ref 仍在" || echo "  ✓ .ref 已删除"
echo "  --- 曲库目录 ---"
sudo ls -l "$LIB"
echo "  --- DB 是否被污染（不应出现 zz-v52-probe2 行）---"
sudo /home/<user>/fnmusic_ext/.venv-proxy/bin/python -c "
import sqlite3
con=sqlite3.connect('file:/usr/local/apps/@appdata/trim.music/db/music.db?mode=ro',uri=True)
r=con.execute(\"SELECT id,path,is_physical_file_deleted FROM audio_file WHERE path LIKE '%zz-v52-probe%'\").fetchall()
print('   probe 行:', r)
print('   audio_file 总数=%d 未删除=%d' % (
    con.execute('SELECT COUNT(*) FROM audio_file').fetchone()[0],
    con.execute('SELECT COUNT(*) FROM audio_file WHERE is_physical_file_deleted=0').fetchone()[0]))
con.close()
"
