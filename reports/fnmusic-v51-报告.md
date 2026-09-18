# fnmusic-ext v51 变更与验收报告

**时间**：2026-09-18 16:13
**线上文件**：`/home/<user>/fnmusic_ext/proxy/app.py`
**sha1**：`27b3bbcafe83cc3ed3353f2b6d2f598ec963acb4`　**体积**：190171 B
**备份**：`app.py.bak.v49`（v49）、`app.py.bak.v50`（v50）
**健康检查**：`curl --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz` → `{"ok":true,...}`

---

## 一、本次要实现的功能

> 「只对收藏的歌曲进行边听边存」

按你的两个决定实现：

| 决策项 | 你的选择 | 落地方式 |
|---|---|---|
| 触发时机 | **收藏即下载 + 播放补漏** | `favorite-track/create` 后立即后台整轨下载；开播时再补一次漏 |
| 清理策略 | **取消收藏即删对应文件** | `favorite-track/delete` 后删该曲目的音频 / `.lrc` / `.ref` |
| 落盘位置 | `/vol02/<vol-id>/music` | 与数据库 `shared_library.path` 完全一致，无需改配置 |

---

## 二、v49 为什么"有的下载有的不"（上一轮的结论，本次已绕过）

真实播放器按 **1MB 定长窗口**取流（`bytes=0-1048575` → `1048576-2097151` → …），
而旧的落盘逻辑要求「无 Range 或 `bytes=0-`」，且把 `os.replace` 放在**生成器尾部**
（客户端一断开就被取消）。两重门叠加 ⇒ 真机上几乎从不落盘。

本次不再依赖客户端行为，改为**服务端独立整轨下载**，确定性不依赖 Range 形态与断开时序。

---

## 三、改动清单

| # | 改动 | 说明 |
|---|---|---|
| 1 | 新增收藏集合 | 读 `online_favorites/*.json` 的**并集**（本地读盘 + mtime 3s 缓存），热路径不打上游 |
| 2 | 收藏即下载 | `_download_favorite_media()`：`_open_online_stream(guid, None)` 整轨拉取 → `.part` → 字节数校验 → `os.replace` |
| 3 | 播放补漏 | `stream_track()` 在「播放起点」(`range_starts_at_zero`) 且是收藏时再触发一次（有 `.ref` 即跳过） |
| 4 | 取消收藏即删 | `delete_materialized_media()`：删音频 + `.lrc` + `.ref`，**只在曲库/缓存目录内动手** |
| 5 | tee 兜底双门槛 | `_library_ok` 拆开：非收藏只写本地 `cache/`，**不碰云盘曲库** |
| 6 | 不再出现 `unknown.mp3` | 标题解析不出就放弃落盘；tee 侧退到滚动缓存 |
| 7 | `.lrc` 止血 | 改成**音频落盘成功后**才写歌词，杜绝新的孤儿歌词 |
| 8 | v51 修 bug | 陈旧 `.ref` 会把文件写回到**已不存在的旧曲库** `/vol2/1000/music` → 见下节 |
| 9 | 顺带优化 | `detect_library_dir()` 加 30s 缓存（此前**每个 `/stream` 都开一次 SQLite**并对云盘挂载做 stat） |

新增开关（`.env`）：

```
FNMUSIC_TEE_FAVORITES_ONLY='true'      # 本次主开关：只对收藏落盘（已写入 .env）
FNMUSIC_FAV_DL_ON_FAVORITE=true        # 收藏即下载
FNMUSIC_FAV_DL_ON_PLAY=true            # 播放补漏
FNMUSIC_FAV_DELETE_ON_UNFAV=true       # 取消收藏即删文件
FNMUSIC_FAV_DL_CONCURRENCY=2           # 并发下载上限
FNMUSIC_FAV_DL_TIMEOUT_S=240
FNMUSIC_FAV_DL_MAX_BYTES=314572800     # 单曲上限 300MB
```

---

## 四、v51 修的真实 bug（v50 端到端验收抓到）

**症状**

```
[favdl] fail err=FileNotFoundError:
  '/vol02/<vol-id>/music/online_netease_1827600686.<uuid>.part'
  -> '/vol2/1000/music/林达浪 _ h3R3 - 还是会想你.flac'
```

**根因**：`cache/<guid>.ref` 存的是「曲库文件词干（绝对路径）」。本机今天 16:03 把
`shared_library.path` 从 `/vol2/1000/music` 换成了 `/vol02/<vol-id>/music`，
于是 `cache/` 里遗留 **10 个指向已不存在目录**的 `.ref`。
`library_media_path()` 里 `recalled_media_stem()` 是**无条件**复用旧词干的
（只有 `recalled_media_path()` 才判断文件存在性）⇒ 目标目录不存在 ⇒ `os.replace` 报 ENOENT。

**修法**：新增 `_same_dir(path, directory)`（realpath 比较），复用旧映射前必须确认与目标目录同目录；
另外给改名加 `shutil.move` 兜底（跨文件系统 `rename` 会抛 EXDEV）。
10 个陈旧 `.ref` 已备份到 `/tmp/refbak-v51/` 后删除。

---

## 五、验收结果

### 单元（沙箱 `/tmp/v50t`，不碰真机状态）：**29 / 29 PASS**

覆盖 `range_starts_at_zero`、收藏并集与缓存失效、`materialized_library_file`、
删除的越界拒绝（`/etc/hostname` 与目录外文件均拒删）、陈旧 `.ref` 目录一致性。

### 端到端（真机、不经 App、无需鉴权）

| 用例 | 请求 | 结果 |
|---|---|---|
| A. **非收藏**曲目 | `Range: bytes=0-` | `[tee] start … tee=True lib=False fav=False dir=…/cache`，曲库 **92 → 92 不落盘** ✅ |
| B. **收藏**曲目 | `Range: bytes=0-1048575`（真实播放器首窗） | `[favdl] ok dest=/vol02/<vol-id>/music/林达浪 _ h3R3 - 还是会想你.flac bytes=24325932 exp=24325932 ext=flac lyric=True` ✅ |

用例 B 同时验证：目录正确、文件名 = 「歌手 - 歌名」、整轨字节数与 `content-length` 相符、
`.lrc` 在音频成功后才写、`.part` 无残留。

### 现场状态

- 曲库 `/vol02/<vol-id>/music` 回到基线 **92 个文件**（19 音频 + 73 歌词）
- `cache/` 已清空陈旧 `.ref`；收藏列表已还原为空
- 测试用的下载产物（`.flac` + `.ref`）与人工注入的收藏条目**已全部清除**

---

## 六、还需要你做的两件事

1. **App 端真机验收**（服务端无法伪造 App 的 `authx` 签名头，只能由你操作）：
   - 搜索并**收藏**一首在线歌曲 → 等 10~30 秒 → 曲库应出现「歌手 - 歌名.ext」+ 同名 `.lrc`
   - **非收藏**曲目播放 → 曲库不应新增文件
   - **取消收藏** → 对应文件应消失
   - 若 App 里看不到新文件，多为飞牛扫描延迟，可在「音乐 → 媒体库」手动触发一次扫描
2. **56 个孤儿 `.lrc`**（有词无曲）是否清理 —— 需要你确认后我再动手（会先备份并列全清单）。

---

## 七、本地留下的中间产物（`nas_src/`）

| 文件 | 用途 |
|---|---|
| `app_v51.py` | 当前线上版本源码 |
| `_build_v50.py` / `_build_v51.py` | 带自校验的增量构建器（v49→v50→v51） |
| `_v51_check.py` | 29 项沙箱单元验收 |
| `_v50_e2e.sh` / `_v50_e2e_b.sh` | 真机端到端验收脚本 |
| `_chk_dir.py` / `_chk_dir2.py` | 落盘目录与 `shared_library` 取证 |
