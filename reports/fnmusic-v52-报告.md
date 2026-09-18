# fnmusic-ext v52 变更与验收报告

**时间**：2026-09-18 16:45
**线上文件**：`/home/<user>/fnmusic_ext/proxy/app.py`
**sha1**：`e17a408f93c018ef1cf6e6614926190cb9bde34d`　**体积**：196365 B（5009 行）
**备份**：`app.py.bak.v51`（v51，190171 B，sha1 `27b3bbca…`）
**健康检查**：`curl --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz` → `{"ok":true,…}`

---

## 一、要解决的问题

> 「歌曲下载或者删除后，飞牛音乐没有主动触发扫库，这个有什么建议吗？」

现象：收藏下载出来的歌、以及取消收藏删掉的歌，**在 App 里都不会自己更新**，必须手动点一次「扫描」。

---

## 二、根因（真机取证，logs 实证）

### 结论：曲库换到**云盘挂载点**之后，飞牛的自动扫描彻底失效

飞牛音乐有两个扫描入口：

1. **文件系统事件**（`sharedLibraryFSEventHandler`，inotify/FSEvents 驱动）；
2. **手动/接口扫描**（`POST /music/api/v1/shared-library/scan`）。

实测日志证据：

| 事实 | 证据 |
|---|---|
| 旧曲库（本地目录）时，FS 事件一直在工作 | 今天共 270 条 `sharedLibraryFSEventHandler`，**全部**指向 `/vol2/1000/music` |
| 本地目录被删后，事件流就断了 | 最后一条：`16:02:02 OnDirDeleted: path=/vol2/1000/music` |
| 换成云盘挂载点后，**零 FS 事件** | 16:03 起 `path=/vol02/<vol-id>/music` 的事件数 = **0** |
| 曲库确实是 rclone 云盘 | `/vol02/<vol-id>` = `fuse.rclone` WebDAV 挂载（`cloud-storage/v1/dav`） |

⇒ **rclone/FUSE 挂载不会产生 inotify 事件**，飞牛收不到"有新文件/文件被删"的通知，
所以写进云盘曲库的文件对飞牛**不可见**，删掉的文件在 DB 里也**不会被标记删除**。

补充事实：
- 扫描接口 **必须鉴权**：无凭据一律 `401 {"code":99999,"msg":"INVALID TOKEN"}`，
  有 `cookie: music-token=<登录 token>` 则 `200 {"code":0}`。
- 扫描**能扫出新文件**，**也能清理已删文件**（见下节实验）。
- 飞牛**没有**"定时扫描/自动扫描"设置项。

---

## 三、方案：「谁有凭证谁去调」

插件自己签不出 token（`authx` 是逐请求签名头），所以设计成借 App 的凭证：

```
落盘成功 / 删除成功
      │
      ├─ request_library_scan("tee"|"favdl"|"unfav")   ← 挂一个待办（3s 窗口合并）
      │
      ├─ ① 若触发它的那次请求头还新（≤ auto_scan_auth_ttl_s=90s）
      │      → 3s 后自己打一发 scan（下载刚结束的最佳时机）
      │
      └─ ② 否则留给**下一次带鉴权的 App 请求**兜底消化
             （中间件钩子；App 打开时会持续轮询，必然命中）
```

两个通道都写 `[scanreq]` 探针，便于验收。

---

## 四、改动清单

| # | 改动 | 说明 |
|---|---|---|
| 1 | `library_guid()` | 复用 `detect_library_dir()` 的 30s 缓存，同一个 SQL 顺带读出 `shared_library.guid`（本机 = `<lib-guid>`）；读不到则降级 `scan-all` |
| 2 | `remember_auth_headers()` / `_recent_auth_headers()` | 中间件里记住最近一次**带鉴权**的请求头（内存 + TTL，**绝不打印其值**） |
| 3 | `_call_library_scan()` | `POST /music/api/v1/shared-library/scan`，body `{"guid": …}`；`code==0` 才算成功 |
| 4 | `_scan_soon()` / `request_library_scan()` | 待办 + 3s 合并窗口；凭证新鲜则自动发出 |
| 5 | `consume_pending_scan()` | 中间件钩子：任何带鉴权请求都会顺手把待办打掉（兜底主力） |
| 6 | 三个触发点 | ① tee 落曲库成功 ② 收藏整轨落盘成功 ③ 取消收藏**确实删掉了文件**（`if removed:` 守卫） |
| 7 | `_spawn_bg_task()` | `_spawn_bg()` 的返回 task 版本（需要观察的后台任务用） |
| 8 | `_LIB_DIR_CACHE` 增加 `guid` 字段 | 与目录探测共用一次查询 |

新增开关（`.env` 可覆盖）：

```ini
FNMUSIC_AUTO_SCAN='true'              # 总开关
FNMUSIC_AUTO_SCAN_DELAY_S='3'         # 合并窗口
FNMUSIC_AUTO_SCAN_AUTH_TTL_S='90'     # 凭证保鲜时长
FNMUSIC_AUTO_SCAN_SCAN_ALL='false'    # true = 永远走 scan-all（全量）
```

---

## 五、验收

### 5.1 单元验收：**73 项全通过**

`_v52_check.py`（= v51 全部 29 项 + v52 新增 44 项）：配置/目录/guid、`range_starts_at_zero`、
收藏集合、删除安全边界、`library_media_path` 陈旧映射一致性、鉴权头识别与 TTL、
待办排队/合并/兜底/`auto_scan=False`、`_call_library_scan` 的 path/body/401/非 JSON/scan-all 降级/异常、
探针落盘与**不泄漏 cookie 值**、三个触发点源码级接线。

### 5.2 扫描行为实验（直打上游，真实 token）

| 步骤 | 结果 |
|---|---|
| 造 `zz-v52-probe.mp3` → 扫描 | `audio_file` 新增行 66，未删除 1 → **2** ⇒ **能扫出新文件** ✅ |
| 删掉该文件 → 再扫描 | 行 66 `is_physical_file_deleted` 0 → **1**，未删除 2 → **1** ⇒ **能清理已删文件** ✅ |

### 5.3 端到端：**下载侧**

真实 token 播放收藏曲目（`online:netease:1827600686`）：

```
[favdl] ok dest=/vol02/<vol-id>/music/林达浪 _ h3R3 - 还是会想你.mp3 bytes=24325932 exp=24325932 ext=mp3
[scanreq] queue reason=favdl pending=1
[scanreq] call path=/music/api/v1/shared-library/scan guid=<lib-guid>… status=200 ok=True body={"code":0,"msg":"","data":null}
```

上游随即（16:37:05 落盘 → **16:37:14 扫到**，约 9 秒）：

```
scanner[scrapeAudioFileCloudMetadata]: finished. … elapsed=736ms
scanner[persistAudioFileScrapeResult]: after persisting audio file track. audioFileID=67
search[queuedMetadataIndexer]: after flushing metadata indexes. trackCount=1, albumCount=1, artistCount=2
```

DB：`audio_file` 新增行 67（`is_physical_file_deleted=0`，size 24325932）。
**App 接口 `/music/api/v1/track/list` 立刻就能列出这首** ⇒ 不用再手点扫描 ✅

### 5.4 端到端：**删除侧**

调用真实取消收藏接口（`POST /music/api/v1/favorite-track/delete`）：

```
[favdel] guid=online:netease:1827600686 removed=2 …/林达浪 _ h3R3 - 还是会想你.mp3|…/online_netease_1827600686.ref
[scanreq] queue reason=unfav pending=1
[scanreq] call path=/music/api/v1/shared-library/scan guid=<lib-guid>… status=200 ok=True
```

DB：行 67 `is_physical_file_deleted` 0 → **1**，未删除 2 → **1**；
`track/list` 回到**只有 `空心 - Live` 一首**保证 App 不再显示已删曲目 ✅

### 5.5 兜底通道

无凭证 / 凭证过期时不硬打上游（单元测试 8.1、8.4、8.6 覆盖），
待办保留给下一次带鉴权的 App 请求（8.2/8.3 覆盖）。

---

## 六、现状与残留（均已核对）

- 曲库目录回到基线 **4 个文件**（1 音频 + 3 歌词）。
- `play_history=3`、`favorite_track=0`、`playlist=0`，收藏文件已还原为实验前内容 —— **无实验写入**。
- 测试产生的 `audio_file` 行 66 / 67 处于 `is_physical_file_deleted=1`（与飞牛自己留下的 18 条墓碑同一状态），
  `track/list` 不返回它们，对 App 无影响。
- 实验期间飞牛自己下载的一条孤儿歌词 `online_netease_1973665667.lrc` 已备份到
  NAS `/tmp/v52_cleanup_bak/` 后移除。

---

## 七、一个需要知道的副作用

v52 生效后，**刚下载的文件会立刻被飞牛扫到**，而飞牛在扫到新文件时会
**排队下载歌词**（`after enqueueing lyric download`）。如果你在下载后**马上**取消收藏，
文件被插件删掉了，但那条歌词队列可能仍会完成 → 曲库里留下一个孤儿 `.lrc`。

观察到的次数：本次实验 1 次。若这个副作用 annoying，下一版可以在删除时**顺带清理
同名/同曲目 id 的孤儿 `.lrc`**（现有 `delete_materialized_media` 只删与音频同词干的 `.lrc`，
而飞牛给歌词的命名是 `online_netease_<id>.lrc`，名字对不上）。

---

## 八、回滚

```bash
sudo cp -f /home/<user>/fnmusic_ext/proxy/app.py.bak.v51 /home/<user>/fnmusic_ext/proxy/app.py
sudo systemctl restart fnmusic-ext
```

（v52 只改了代理进程，未动数据结构；回滚后自动扫库功能消失，其余不受影响。）
