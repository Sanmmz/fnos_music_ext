#!/bin/bash
# v52 E2E（下载侧）：收藏在线曲目 → 插件整轨落盘 → 插件自动扫库 → 飞牛 DB 出现该曲目
# 随后再走一遍取消收藏 → 自动删文件 → 自动扫库 → DB 行被标记删除（完整闭环）
set -u
LIB=/vol02/<vol-id>/music
PROBE=/home/<user>/fnmusic_ext/access_probe.log
INFO=/usr/local/apps/@appdata/trim.music/log/info.log
FAV=/home/<user>/fnmusic_ext/online_favorites/43a23a9b970a4cb1bb65e1169f503c74.json
SOCK=/var/run/trim_music.socket
PY=/home/<user>/fnmusic_ext/.venv-proxy/bin/python
TOK="${1:?用法: $0 <music-token>}"
G="online:netease:1827600686"

cnt() {
  sudo $PY -c "
import sqlite3
con=sqlite3.connect('file:/usr/local/apps/@appdata/trim.music/db/music.db?mode=ro',uri=True)
print('%d %d %d' % (
 con.execute('SELECT COUNT(*) FROM audio_file').fetchone()[0],
 con.execute('SELECT COUNT(*) FROM audio_file WHERE is_physical_file_deleted=0').fetchone()[0],
 con.execute(\"SELECT COUNT(*) FROM audio_file WHERE path LIKE '%还是会想你%'\").fetchone()[0]))
con.close()"
}

echo "=== 0) 基线 ==="
P0=$(sudo wc -c < "$PROBE"); I0=$(sudo cat "$INFO" | wc -l)
echo "  探针字节=$P0  info.log 行数=$I0"
echo "  曲库文件数=$(sudo ls "$LIB" | wc -l)"
echo "  audio_file 总数/未删除/含「还是会想你」: $(cnt)"

echo
echo "=== 1) 加入收藏（备份原文件）==="
sudo cp -f "$FAV" /tmp/fav.bak.v52e2e
sudo $PY -c "
import json
p='$FAV'; g='$G'
d=json.load(open(p)); items=d.get('items') if isinstance(d,dict) else d
items=[it for it in items if it.get('guid')!=g]
items.append({'guid':g,'createdAt':1,'track':{'guid':g,'title':'__v52_probe__'}})
json.dump({'items':items}, open(p,'w'), ensure_ascii=False, indent=2)
print('  收藏集合:', [it['guid'] for it in items])"

echo
echo "=== 2) 用真实 token 播放首窗（触发收藏整轨落盘）==="
curl -s -o /dev/null -w "  stream http=%{http_code} 下载=%{size_download} 字节\n" \
  --unix-socket $SOCK -m 30 -H "cookie: music-token=$TOK" -H "Range: bytes=0-1048575" \
  "http://localhost/music/api/v1/track/stream?guid=$G"

echo "  等待整轨落盘 + 自动扫库…"
for i in $(seq 1 30); do
  sleep 4
  if sudo tail -c 6000 "$PROBE" | grep -aq "\[scanreq\] call"; then break; fi
done
sleep 8

echo
echo "  --- 探针 ---"
sudo tail -c 6000 "$PROBE" | grep -aE "\[favdl\]|\[scanreq\]" | tail -12
echo
echo "  --- 上游 info.log 新增扫描行（本轮）---"
sudo tail -n +"$((I0+1))" "$INFO" | grep -aE "scanner\[scrapeAudioFileCloudMetadata|metadata correction|persistAudioFileScrapeResultAfterRescrape\]: finished" | head -8

echo
echo "=== 3) 结果：飞牛是否「看见」了新下载的曲目 ==="
echo "  audio_file 总数/未删除/含「还是会想你」: $(cnt)   （未删除应从 1 → 2）"
echo "  --- 曲库目录 ---"
sudo ls -l "$LIB"
echo "  --- 新落盘文件在 DB 的状态 ---"
sudo $PY -c "
import sqlite3
con=sqlite3.connect('file:/usr/local/apps/@appdata/trim.music/db/music.db?mode=ro',uri=True)
for r in con.execute(\"SELECT id,path,size,is_physical_file_deleted FROM audio_file WHERE path LIKE '%还是会想你%'\"):
    print('   ', r)
con.close()"
