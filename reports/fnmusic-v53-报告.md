# fnmusic-ext v53 变更与验收报告

**时间**：2026-09-18 16:53
**线上文件**：`/home/<user>/fnmusic_ext/proxy/app.py`
**sha1**：`586d7f88dc1a4537ab463858cfac41edbc2bb339`　**体积**：202492 B（5165 行）
**备份**：`app.py.bak.v52`（v52，196365 B）
**健康检查**：`/_ext/healthz` → `{"ok":true,…}`

---

## 一、问题

> 「播放在线音乐会在本地留下孤儿歌词文本」

实测曲库目录：**只有 1 个音频，却有 3 个 `.lrc`**，其中 2 个没有任何同名词曲。

---

## 二、根因（真机取证）

`cache/*.ref` 的内容（root 属主，需 sudo 读）全部指向**曲库目录**：

| `.ref` | 指向 | 实际情况 |
|---|---|---|
| `online_lx_wy_3407803435` | `/vol02/…/music/黄霄雲 _ 刘端端 - 空心 (Live版)` | `.mp3` + `.lrc` 都在 ✓ |
| `online_netease_1851652156` | `/vol02/…/music/h3R3 - 忘不掉的你` | **只有 `.lrc`** ✗ |
| `online_netease_2018733994` | `/vol02/…/music/郑润泽 - 遐想` | **只有 `.lrc`** ✗ |
| `online_netease_1973665667` | `/vol02/…/music/online_netease_1973665667` | 两者都不在曲库 |

### 真正的凶手：`lyric_cache_path()` 把曲库当兜底

```python
def lyric_cache_path(guid, title="", artist=""):
    ...
    audio = find_cache_file(guid)
    if audio:
        return os.path.splitext(audio)[0] + ".lrc"
    d = detect_library_dir()          # ← 曲库目录
    os.makedirs(d, exist_ok=True)
    if title or artist:
        return os.path.join(d, f"{library_basename(title, artist)}.lrc")
    return os.path.join(d, f"{cache_safe_guid(guid)}.lrc")
```

App 一播放在线曲目就会请求歌词 → 旋律走到 `write_lyric_cache()` → 没有落盘音频时，
歌词就被写成 **曲库目录里的「歌手 - 歌名.lrc」**。而曲库是 **rclone 云盘挂载**，
写进去等于真的往云盘上传一个没有音频的歌词文件。

叠加两点，孤儿**永久留存**：

1. **音频与歌词共用同一个 `.ref` 槽**：`remember_media_path()` 先被歌词调用记下歌词词干，
   等音频落进 `cache/` 后又把它改写 ⇒ 歌词映射丢失（顺带导致下次播放重复出网抓词）。
2. **`cache_gc.purge_rolling()` 的 `_stem_has_file()` 把 `.lrc` 也算作"目标还在"**，
   于是连那个指向孤儿歌词的 `.ref` 都不清理 —— 自愈路径全被堵死。

这也解释了用户此前手工清掉的 56 个孤儿歌词。

---

## 三、改动清单

| # | 改动 | 说明 |
|---|---|---|
| 1 | **歌词落地位置重定** | 只有 `materialized_library_file(guid)`（音频真在曲库）才写同名 sidecar；否则一律写本地 `cache/<guid>.lrc`。**云盘曲库不再产出无主歌词** |
| 2 | **歌词专用映射 `<guid>.lyricref`** | 歌词不再和音频共用 `.ref`，`find_lyric_file()` 优先读它 ⇒ 音频从曲库切到 cache 时歌词映射不再丢 |
| 3 | **`write_lyric_cache` 收尾收敛** | 只在「歌词就是音频的同名 sidecar」时才更新 media `.ref` |
| 4 | **`sweep_orphan_lyrics()` 自愈** | 清「插件自己写下、音频已不存在」的曲库歌词；靠 `.ref`/`.lyricref` 识别，**绝不碰飞牛自己管的歌词** |
| 5 | 自愈时机 | 启动后 15 秒清一次 + 每 30 分钟复扫 + 每次取消收藏删除后顺带扫一次 |
| 6 | `delete_materialized_media` | 连 `.lyricref` 一起删，歌词也能随取消收藏清理 |
| 7 | 探针 | 新增 `[lyricgc] removed <path>` |

新增开关（`.env` 可覆盖）：

```ini
FNMUSIC_LYRIC_ORPHAN_GC='true'                     # 自愈总开关
FNMUSIC_LYRIC_ORPHAN_GC_MIN_AGE_S='120'            # 歌词落盘后多久才允许判定为孤儿
FNMUSIC_LYRIC_ORPHAN_GC_INTERVAL_S='1800'          # 复扫间隔
```

**自愈的安全边界**（每条都写进单元测试）：

- 词干必须来自 `cache/` 下的 `.ref`/`.lyricref` ⇒ 只可能是我方写下的文件；
- 词干必须落在**曲库目录**内 ⇒ `cache/` 内的歌词按滚动缓存语义交给 `purge_rolling`；
- 同名词曲（任一音频扩展名）还在 ⇒ 不是孤儿，跳过；
- `.lrc` mtime 未满 `min_age`（默认 120s）⇒ 放过，避开下载竞态；
- 删除走 `_safe_unlink_in_media_dirs()` ⇒ 只允许动曲库 / 缓存目录内的文件。

---

## 四、验收

### 4.1 单元：**117/117 PASS**

`_v53_check.py` = v52 全部 73 项 + v53 新增 44 项。核心断言：

- 音频在曲库 → 歌词写曲库同名 sidecar；音频只在缓存 → 写缓存同名 sidecar；
  **完全无音频 → 一律落 `cache/`，且不会出现「歌手 - 歌名.lrc」于曲库**
- `write_lyric_cache` 落 cache 时**不污染**音频 `.ref`，且写得出 `.lyricref`
- 自愈：孤儿被清 / 有音频不误删 / **飞牛自己的歌词毫发无伤** / cache 内孤儿不归它管 /
  min_age 生效 / 开关关掉就不动手 / `.lyricref` 型孤儿一并清
- 源码级：`lyric_cache_path` 里已无 `library_basename(...).lrc` 兜底

### 4.2 真机：自愈把存量孤儿清掉了

重启后自愈探针：

```
[lyricgc] removed /vol02/<vol-id>/music/郑润泽 - 遐想.lrc
[lyricgc] removed /vol02/<vol-id>/music/h3R3 - 忘不掉的你.lrc
```

曲库从 `1 音频 + 3 歌词` 变为 **`1 音频 + 1 歌词`**（只剩合法的「空心」mp3 + 同名 lrc）。

### 4.3 真机：写入侧不再产生新孤儿

对三个在线曲目请求歌词（`GET /music/api/v1/lyric/list?guid=…`）：

| 曲目 | 结果 |
|---|---|
| `online:netease:1851652156`（无任何音频） | 歌词 → `cache/online_netease_1851652156.lrc` ✅，曲库无新增 |
| `online:netease:1973665667`（音频只在 cache） | 歌词 → `cache/online_netease_1973665667.lrc` ✅，曲库无新增 |
| `online:lx:wy:3407803435`（音频在曲库） | 复用曲库已有同名 sidecar，无新增 ✅ |

三个请求都正常返回了歌词内容 ⇒ **App 侧体验不变**；曲库文件数保持 **2**（前后一致）。

---

## 五、现状与残留

- 曲库目录：`黄霄雲 _ 刘端端 - 空心 (Live版).mp3` + 同名 `.lrc` —— 干净。
- `cache/` 里新增的 `<guid>.lrc` 属于插件自己的缓存目录，按滚动缓存语义由
  `cache_gc.py --keep 0` / `purge_rolling()` 处理；`.lyricref` 也已被 `purge_rolling`
  识别（它以 `.ref` 结尾），目标消失时会自动回收。
- 遗留的陈旧 `.ref`（指向曲库但文件都没了）会在下次 `purge_rolling` 时按既有规则自行消失，
  不影响功能：`find_lyric_file()` 现在优先看 `.lyricref`。

---

## 六、回滚

```bash
sudo cp -f /home/<user>/fnmusic_ext/proxy/app.py.bak.v52 /home/<user>/fnmusic_ext/proxy/app.py
sudo systemctl restart fnmusic-ext
```

（v53 未改数据结构；回滚后「播放即在曲库生成孤儿歌词」的行为会回来。）
