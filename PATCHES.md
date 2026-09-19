# 增强补丁说明（v30 ~ v53）

> 本仓库是 [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) **v1.6.0** 的增强分支。
>
> **改动只涉及一个文件：`proxy/app.py`**（119,717 B → 233,256 B，5,849 行）。其余文件与上游 v1.6.0 逐字节一致，
> 可直接用上游的 `install.sh` / `extend.sh` / `restore.sh` 安装与卸载。

---

## 为什么会有这个分支

上游 v1.5.0 / v1.6.0 把「在线曲库」（洛雪 lx、网易云 musicbox、酷我、musicdl 多音源）接进飞牛音乐后，
有几个会**直接打断手机 App 可用性**的缺陷：

| # | 现象 | 真实根因 |
|---|---|---|
| 1 | 手机 App 里收藏 / 最近播放**整列空白消失** | 在线曲目入库时标题为空、时长为 0、封面 URL 无效 |
| 2 | 列表能出但**要等几十秒**，或干脆超时 | 在线封面是**现抓**的，约 4 秒/张 × N 条 |
| 3 | 每日推荐 20 首里**15 首点播放就 404** | 网易云取流走 musicbox，耗时 1.2~94 秒，被 4 秒硬超时打断 |
| 4 | 下载 / 删除后**曲库不会自动刷新**，必须手动点扫描 | 曲库迁到 rclone 云盘挂载点后不再产生 inotify 事件 |
| 5 | 播放在线音乐会在云盘曲库留下**孤儿歌词** `.lrc` | 歌词落盘路径把曲库目录当兜底 |

以上 5 类问题在本分支中全部修复，并各配了可复现的验收脚本（见 [`patches/`](patches/)）与验收报告（见 [`reports/`](reports/)）。

---

## 改动总览

| 版本段 | 主题 | 关键改动 |
|---|---|---|
| v30 ~ v34 | 在线条目入库质量 | 封面四级兜底（含占位图）、废弃 `stub_online_info`、解析不出标题的条目**一律不入库**、时长/封面归一化 |
| v36 ~ v41 | 列表渲染超时 | 封面**按 guid 落盘缓存** + 列表返回时后台预取 + 抓图共享长连接；实测 4000ms → 0~2ms |
| v42 | 元数据重复查询 | `_online_info` TTL 缓存 + 并发去重；三后端并发解析；不可播曲目四处一起删 |
| v43 ~ v44 | 封面接口契约 | 封面尺寸按客户端 `size` 分档；**封面接口任何情况都不返回 JSON / 404**（必须给 200 有效图） |
| v45 | 取流快通道 | 网易云**直连解析播放地址**（借 musicbox 登录态）+ musicbox 兜底 + 取流地址缓存/预热 |
| v46 ~ v48 | 播放历史 | 「移除在线曲目」因参数校验失败被拒 → 修复 `play-history/delete` |
| v49 | 边听边存诊断 | 定位「落盘时有时无」= 真实播放器按 1MB 定长窗口取流，旧逻辑只认 `bytes=0-` |
| v50 ~ v51 | 收藏即下载 | 只对**收藏**的在线曲目落盘（服务端独立整轨下载），取消收藏即删除 |
| v52 | 自动扫库 | 落盘/删除成功后**借 App 凭证主动调飞牛扫描接口** |
| v53 | 歌词归属 | 歌词落地归属重定 + `.lyricref` 专用映射 + **孤儿歌词自愈** |
| v54 | 歌词贴身 | **歌词永远跟着音频走**：选路重排 + 短路条件收紧 + 存量歌词提升自愈 |
| v55 | 搜索结果排序 | 补全封面 + 真音质（网易云批量 / 酷我单曲）；稳定重排 `(-有海报, -音质档, idx)`；首屏四层保证 + 确定性判据；搜索页封面后台预取 |

---

## 一、在线曲目渲染可用性（v30 ~ v44）

### 1.1 封面接口绝不能 404 / 绝不能返回 JSON（v30、v43、v44）

上游在没有封面时**直接返回 404**。手机 App 拿到 404 后会**整列渲染失败**（不是跳过那一行，是整列消失）。
本分支改为四级兜底，**无论什么情况都返回 200 + 真实图片字节**：

```
_online_info 缓存 → meta_cache → 网易云详情接口 → 内置占位 PNG
```

同时：

- **封面尺寸按客户端 `size` 分档缓存**，不写死一个尺寸；
- 抓图失败时返回**占位图**，且占位图带 6 小时过期（`.exp`），真实封面恢复后不会被永久灰图盖住。

> 铁律：给手机端提供「代理抓取的外部资源」（封面、头像等）时，**任何异常都不能让 HTTP 状态码或
> `Content-Type` 变得不像一张图**。401 也一样 —— 不能返回 JSON 错误体。

### 1.2 在线条目入库必须字段齐全（v30 ~ v34）

原 `event/report` 用 `stub_online_info` 录制，只写 `{guid, "", "", source}` ⇒ 标题空、时长 0。
改为 `_resolve_record_meta`，按优先级补齐：

```
_online_info → _retained_track（搜索缓存）→ 酷我 musicInfo?mid= → 网易云 api/v3/song/detail
```

并且**解析不出标题的在线曲目一律不入库**——宁可少一条，也不能让一条空标题条目拖垮整列。

归一化两处坑：

- `_norm_duration_s`：洛雪把酷狗的「秒」当毫秒返回 ⇒ 0.309 秒被显示成 309 毫秒，需要 ×1000；
- `_normalize_cover_url`：126.net 原图 7MB ⇒ 加 `?param=300y300` 后 102KB，否则列表加载卡顿。

### 1.3 封面现抓 4 秒 → 落盘缓存 0~2ms（v38 ~ v41）

**决定性证据**（来自内置请求探针 `access_probe.log`）：手机 App 打开含在线条目的列表时，
会**为每条逐个请求封面**，而封面接口耗时 3.3~4.2 秒 —— 但那 4 秒**不是抓图慢**，
而是封面接口在返回前先调了 `_online_info`（查洛雪容器，约 4 秒）。

修法：

1. 封面**按 guid 落盘缓存** `cover_cache/<safe-guid>.img`，命中即返回，不再调 `_online_info`；
2. 统一按 300px 缓存一份，忽略客户端 `size=120/400/800`（否则每档各抓一次）；
3. 列表返回时**后台预取**封面（历史 / 收藏 / 每日推荐三处）；
4. 抓图改用**共享长连接 client**（原每次新建 `httpx.AsyncClient()` = 每次 DNS+TCP+TLS）。

实测：`size=120/400/800` 均 **0~2ms**（原 ~4000ms），列表接口本身 11~15ms 不受预取影响。

### 1.4 在线元数据短时缓存（v42）

用户感知「首次播放」与「点收藏」各等约 4 秒。三处改动：

1. `_online_info` 外包一层 **TTL 缓存**（默认 300s）+ 并发去重（`_ONLINE_INFO_INFLIGHT`）。
   缓存键 = `sha256(凭据头) + json(source_config) + guid` —— **必须带凭据与配置**，否则不同登录态会串味。
2. `_resolve_record_meta` 三后端 `asyncio.gather` 并发。
3. `_prefetch_online_meta`：历史 / 收藏 / 歌单详情三个列表接口返回时后台预热前 3 条。

> 安全性：`_online_info` 只返回**元数据**（标题/歌手/专辑/封面/时长/歌词），
> **播放地址**走独立的 `resolve_netease_url` / `resolve_lx_url`，不经这里 ⇒ 不会缓存过期 token。

### 1.5 不可播曲目要四处一起删（v42）

在线条目的元数据散在 4 个地方，只删列表 JSON 是不够的（封面缓存和 meta 缓存会继续让那一行被渲染）：

```
play_history/<user-guid>.json        # items[] 里的条目
online_favorites/<user-guid>.json    # items[] 里的条目
meta_cache.json                      # 键就是 guid
cover_cache/<safe-guid>.img(.ct)     # 文件名 = re.sub(r"[^a-zA-Z0-9_.:-]", "_", guid)
```

---

## 二、取流与播放（v45 ~ v49）

### 2.1 网易云「直连快通道 + musicbox 兜底」（v45）

**症状**：每日推荐 20 首里 15 首点播放 404（`online source unavailable`），失败耗时恰好 4.00s。

**根因**：慢在 musicbox（1.2~94 秒/次），被 `track/stream` 的 4 秒硬上限打断。

**原则**：直连**只替代 musicbox 的一个函数**（解析播放地址）。musicbox 的其余 6 类职责
（搜索 / 批量详情+可播过滤 / 歌曲详情 / 歌词 / 个性化日推 / 榜单）**一行都不改**——
它仍然是网易云的登录态持有者与 weapi 签名方。

```
resolve_netease_url()
  ├─ ① 查取流地址缓存（TTL 600s，上限 512，cookie 变化整体作废）
  ├─ ② 直连 POST https://music.163.com/api/song/enhance/player/url（并发去重）
  └─ ③ musicbox /api/v1/song/{id}/url 原样保留兜底
```

实测：每日推荐可播 **5/20 → 20/20**（全 FLAC），单首首字节 **4000ms → ~800ms**。

**硬约束（违反会出比 404 更糟的问题）**

- ★ **`freeTrialInfo` 非空必须判失败**。老接口对 VIP 曲目会返回**只有 30 秒的试听片段**，
  直接播会让用户听到半首歌就断 —— 比 404 还糟。判失败后自动回落 musicbox。
- ★ **预热绝不触发 musicbox 兜底**。否则直连一旦失效，一次列表刷新就是 20 个 4~30 秒的请求。
- ★ 直连是**借** musicbox 的登录态，不是自己维护账号；cookie 过期只影响快通道，扫码入口仍是 `./netease_login.sh`。

顺带修掉两个真 bug：

- `_prefetch_online_meta` 是 `async def` 但 3 处调用点全是**裸调用**（无 `await` 也无 `create_task`）
  ⇒ 协程建了从不执行，元数据预热一直是**死代码**；
- `asyncio.create_task()` 不留引用时事件循环只持弱引用，**任务可能在执行中被 GC** ⇒
  新增 `_BG_TASKS` 强引用池 + `_spawn_bg()`。

### 2.2 「移除在线曲目」被参数校验拒绝（v46 ~ v48）

`play-history/delete` 对在线曲目报「无效参数」。修复只动这一个端点，先备份 `play_history` JSON，测完原样还原。

### 2.3 「边听边存」为什么时有时无（v49）

探针确证：真实播放器按 **1MB 定长窗口**取流（`bytes=0-1048575` → `1048576-2097151` → …），
而旧的落盘逻辑要求「无 Range 或 `bytes=0-`」，且把 `os.replace` 放在**生成器尾部**
（客户端一断开就被取消）。两重门叠加 ⇒ 真机上几乎从不落盘。

---

## 三、收藏即下载（v50 ~ v51）

**需求**：只对**收藏**的在线歌曲「边听边存」，取消收藏即删除对应文件。

| 决策项 | 落地方式 |
|---|---|
| 触发时机 | **收藏即下载**（`favorite-track/create` 后立即后台整轨下载）+ **播放补漏**（开播时再补一次） |
| 清理策略 | **取消收藏即删**（`favorite-track/delete` 后删该曲目的音频 / `.lrc` / `.ref`） |
| 落盘位置 | 与数据库 `shared_library.path` 完全一致，无需改配置 |

改动要点：

1. **不再依赖客户端行为**：服务端独立整轨下载（`_download_favorite_media()`），
   不依赖 Range 形态与断开时序 —— 这是 v49 结论的直接对策；
2. **收藏集合**：读 `online_favorites/*.json` 的**并集**（本地读盘 + mtime 3s 缓存），热路径不打上游；
3. **tee 兜底双门槛**：非收藏曲目只写本地 `cache/`，**不碰云盘曲库**；
4. 标题解析不出就放弃落盘 —— 不再出现 `unknown.mp3`；
5. `.lrc` 改成**音频落盘成功后**才写；
6. `detect_library_dir()` 加 30s 缓存（此前**每个 `/stream` 都开一次 SQLite** 并对云盘挂载做 stat）。

新增开关（`.env` 可覆盖）：

```ini
FNMUSIC_TEE_FAVORITES_ONLY='true'      # 主开关：只对收藏落盘
FNMUSIC_FAV_DL_ON_FAVORITE=true        # 收藏即下载
FNMUSIC_FAV_DL_ON_PLAY=true            # 播放补漏
FNMUSIC_FAV_DELETE_ON_UNFAV=true       # 取消收藏即删文件
FNMUSIC_FAV_DL_CONCURRENCY=2           # 并发下载上限
FNMUSIC_FAV_DL_TIMEOUT_S=240
FNMUSIC_FAV_DL_MAX_BYTES=314572800     # 单曲上限 300MB
```

### v51 修的真 bug：陈旧 `.ref` 把文件写回已不存在的旧曲库

**症状**

```
[favdl] fail err=FileNotFoundError:
  '/vol02/<vol-id>/music/online_netease_1827600686.<uuid>.part'
  -> '/vol2/1000/music/林达浪 _ h3R3 - 还是会想你.flac'
```

**根因**：`cache/<guid>.ref` 存的是「曲库文件词干（绝对路径）」。曲库目录一旦变更，
`cache/` 里就遗留一批**指向已不存在目录**的 `.ref`；而 `library_media_path()` 里的
`recalled_media_stem()` 是**无条件**复用旧词干的 ⇒ 目标目录不存在 ⇒ `os.replace` 报 ENOENT。

**修法**：新增 `_same_dir(path, directory)`（realpath 比较），复用旧映射前必须确认与目标目录同目录；
另外给改名加 `shutil.move` 兜底（跨文件系统 `rename` 会抛 EXDEV）。

---

## 四、自动扫库（v52）

**问题**：下载 / 删除后飞牛音乐不会主动扫库，必须手动点一次「扫描」。

**根因（日志实证）**：飞牛音乐有两个扫描入口 —— ① 文件系统事件（inotify 驱动）；
② 手动/接口扫描。曲库迁到 **rclone WebDAV 云盘挂载点**后，
**FUSE 挂载不产生 inotify 事件** ⇒ 入口 ① 彻底失效（实测 FS 事件数 = 0），
写进云盘的文件对飞牛不可见，删掉的文件在 DB 里也不会被标记删除。

补充事实：扫描接口 `POST /music/api/v1/shared-library/scan` **必须鉴权**（无凭据一律 401），
且飞牛**没有**「定时扫描 / 自动扫描」设置项。

**方案：「谁有凭证谁去调」**

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

```ini
FNMUSIC_AUTO_SCAN='true'              # 总开关
FNMUSIC_AUTO_SCAN_DELAY_S='3'         # 合并窗口
FNMUSIC_AUTO_SCAN_AUTH_TTL_S='90'     # 凭证保鲜时长
FNMUSIC_AUTO_SCAN_SCAN_ALL='false'    # true = 永远走 scan-all（全量）
```

**实测**：下载落盘后 **9 秒**被飞牛扫到（`audioFileID=67` 入库，App 立刻能列出）；
取消收藏后扫描把该行标记 `is_physical_file_deleted=1`。

> 探针：`[scanreq] queue reason=… pending=N` / `[scanreq] call path=… status=… ok=…`
> 凭证只在内存中短时保留且**绝不打印其值**。

---

## 五、歌词归属与孤儿歌词自愈（v53）

**问题**：播放在线音乐会往云盘曲库留下**孤儿歌词**（只有 `.lrc`、没有音频）。

**根因**：`lyric_cache_path()` 在「音频尚未落盘」时把 **`detect_library_dir()`（云盘曲库）当兜底**：

```python
audio = find_cache_file(guid)
if audio:
    return os.path.splitext(audio)[0] + ".lrc"
d = detect_library_dir()          # ← 云盘曲库，写进去等于往云盘上传
os.makedirs(d, exist_ok=True)
...
return os.path.join(d, f"{library_basename(title, artist)}.lrc")
```

叠加两点导致孤儿**永久留存**：

1. **音频与歌词共用同一个 `.ref` 槽**：歌词先调用 `remember_media_path()` 记下自己的词干，
   音频落进 `cache/` 后又把它改写 ⇒ 歌词映射丢失；
2. **`cache_gc.purge_rolling()` 的 `_stem_has_file()` 把 `.lrc` 也算作「目标还在」**
   ⇒ 连指向孤儿歌词的 `.ref` 都不清理，自愈路径全被堵死。

**修法**

1. **歌词落地位置重定**：只有 `materialized_library_file(guid)`（音频真在曲库）才写同名 sidecar；
   否则一律写本地 `cache/<guid>.lrc`。**云盘曲库不再产出无主歌词**；
2. **歌词专用映射 `<guid>.lyricref`**：歌词不再与音频共用 `.ref`，`find_lyric_file()` 优先读它；
3. `write_lyric_cache` 收尾收敛：只在「歌词就是音频的同名 sidecar」时才更新 media `.ref`；
4. **`sweep_orphan_lyrics()` 自愈**：清「插件自己写下、音频已不存在」的曲库歌词；
5. 自愈时机：启动后 15 秒清一次 + 每 30 分钟复扫 + 每次取消收藏删除后顺带扫一次。

```ini
FNMUSIC_LYRIC_ORPHAN_GC='true'                 # 自愈总开关
FNMUSIC_LYRIC_ORPHAN_GC_MIN_AGE_S='120'        # 落盘后多久才允许判定为孤儿
FNMUSIC_LYRIC_ORPHAN_GC_INTERVAL_S='1800'      # 复扫间隔
```

**自愈的安全边界**（每条都有单元测试）：

- 词干必须来自 `cache/` 下的 `.ref` / `.lyricref` ⇒ 只可能是我方写下的文件；
- 词干必须落在**曲库目录**内 ⇒ `cache/` 内的歌词按滚动缓存语义交给 `purge_rolling`；
- 同名词曲（任一音频扩展名）还在 ⇒ 不是孤儿，跳过；
- `.lrc` mtime 未满 `min_age`（默认 120s）⇒ 放过，避开下载竞态；
- 删除走 `_safe_unlink_in_media_dirs()` ⇒ 只允许动曲库 / 缓存目录内的文件；
  **绝不碰飞牛自己管理的歌词**。

**实测**：重启后自愈清掉 2 个存量孤儿（曲库 `1 音频 + 3 歌词` → `1 音频 + 1 歌词`）；
写入侧对 3 个在线曲目请求歌词，全部只落 `cache/`，曲库文件数**前后不变**。

### 5.1 回归修复：歌词必须跟着音频走（v54）

v53 把「已有歌词优先」提到最前，引入了一个回归：只要**收藏之前播过一次**这首歌
（App 拉歌词 → 落 `cache/<guid>.lrc`），这个缓存副本就会劫持落点，
之后收藏整轨下载把音频落进曲库，**歌词再也不会贴身写到曲库**。

现在的不变式是：**音频到哪，歌词到哪。**

```
音频在曲库      → 歌词写成曲库同名 sidecar
音频只在 cache/ → 歌词写成 cache 内同名 sidecar
两处都没有      → 歌词落 cache/<guid>.lrc（绝不写云盘曲库）
```

三处配套改动：

1. `lyric_cache_path()` —— 「音频已在曲库」**优先于**任何缓存副本；
2. `write_lyric_cache()` —— 短路条件从「任意位置内容一致」改成「**目标位置**内容一致」，
   否则 cache 副本会永远短路成功、歌词无法被提升；
3. `_drop_shadow_lyric()` —— 写对位置后清掉 `cache/` 里的影子副本。

**存量自愈**：`promote_one_lyric()` / `promote_library_lyrics()` 把
「音频已在曲库、歌词却留在 `cache/`」的歌词搬成同名 sidecar
（启动后 12 秒 + 周期复扫 + 每次整轨落盘后），歌词读取路径也做幂等纠正。
开关 `FNMUSIC_LYRIC_PROMOTE`（默认开）。

安全性：词干必须来自 `cache/` 下 `.ref` 记过的**绝对路径**、必须落在**曲库目录**内、
且该词干下**确实存在音频**才动手 ⇒ 没有音频时绝不无中生有，也绝不碰飞牛自己的歌词。

**实测（真机端到端，真实 token 调真实收藏接口）**：

```
① 先播一次歌词 → cache/<guid>.lrc 生成（制造回归触发条件）
② 收藏          → {"code":0}
③ 音频落曲库    → 加木 - 两 难.mp3（19099195 B）
④ 歌词贴身      → 加木 - 两 难.lrc（同名同词干）
⑤ cache 影子     → 已清掉
⑥ 取消收藏      → 音频 + 歌词一起删除，收藏列表恢复原样
⑦ 原有收藏      → 毫发无伤
```

决定性探针（`lyric=False` ⇒ 走的正是 v54 新增的补位通道）：

```
[favdl] ok guid=online:netease:2163210456 ... lyric=False
[lyricpromo] /vol02/<vol-id>/music/加木 - 两 难.lrc
```

---

## 六、搜索结果有海报 + 高音质优先排序（v55）

**需求**（Request D）：「搜索歌曲能否优先将有海报并且高质量音源排在前面显示」。

上游 v1.6.0 的搜索入口本身没有「质量」维度：三音源（musicbox / musicdl / lxmusic）各自返回结果，
按**任务创建顺序**拼接，谁先返回谁靠前；在线条目天生没有封面 URL、音质是按扩展名硬编的展示值，
于是首屏基本是「没海报 + 标称 mp3」，且与质量无关。v55 在聚合流程里加三层（补全 → 排序 → 首屏免疫），
把「带封面且高码率/无损」的条目顶到第一屏。

三处根因与对策：

| # | 根因 | 对策 |
|---|---|---|
| 1 | 在线条目**天生没有封面 URL**（lx 恒空、musicdl 真机超时、musicbox 极少） | 补全层 `_enrich_search_items()` 回填封面 + 真音质 |
| 2 | 音质是**伪造**的：`bitrate = 1411000 if 无损扩展名 else 320000`，且 `ext` 恒为 mp3 ⇒ 任何歌都显示 320000 | 排序层用「扩展名推导」与「补全结果」**较大者**判定音质档，不改 `ext`、对取流零风险 |
| 3 | 首屏页码**钉在重排之前**（`netease_wait_s=3.0s` 到点就分配并按 guid 固定，之后重排改不了首屏） | 首屏四层保证（见下） |

### 6.1 补全层：一次批量请求拿到「封面 + 真音质」

| 来源 | 接口 | 实测 | 拿到什么 |
|---|---|---|---|
| 网易云（覆盖 `lx→wy` 与 `netease`） | `POST music.163.com/api/v3/song/detail?c=[{"id":N},…]` | **1 次请求 / 25 首 / 0.171s** | `al.picUrl`（25/25 有封面）+ `sq/hr/h/m/l` 音质对象（sq=20/25 无损，h.br=320000 全有） |
| 酷我（`lx→kw`） | `wapi.kuwo.cn/api/www/music/musicInfo?mid=<rid>` | 0.11s/首，`Semaphore(6)` 并发限流 | `pic`/`albumpic` + `hasLossless` |

`_enrich_search_items()` 流程：① 先用 `meta_cache.json` 回填（重启后仍有效，**0 请求**）；
② 网易云走**一次**批量详情（`_netease_detail_bulk`，带正/负缓存）；③ 酷我走单曲详情（`_kw_poster_quality`）；
④ 结果写回条目 + `meta_cache.json`（`cover_url` / `quality_rank` / `lossless` / `no_cover`）。
**只处理前 30 条**（第一屏），失败永不抛错 —— 补不上就是不补。

### 6.2 排序层：稳定重排，只动在线块

`rank_search_items()`：稳定排序（同档保持原始源顺序，`idx` 兜底 ⇒ 完全稳定），排序键 `(-有海报, -音质档, idx)`。

音质档 `_search_item_quality_rank()`（3 最高）：

```
3 = 无损扩展名（flac/wav/ape/wv/aiff/alac/dsf/dff/tta/tak）或码率 ≥ 900k
2 = 高码率有损扩展名（m4a/aac/opus/ogg/mp4）或码率 ≥ 256k
1 = 其它有损（mp3…）
0 = 未知
```

**关键细节**：档位取「扩展名推导」与「补全结果」的**较大者**。只信补全会出现自相矛盾 ——
真机踩到 `ext=aac` 被网易云 `l` 档标成 1，于是「显示 AAC 却排在 mp3 后面」。取 max 只影响排序、不改 `ext`。

### 6.3 首屏四层保证：排序一定作用在第一屏

针对根因 3，四层一起上：

1. **聚合内边收边排**：每收到一批源结果就 `deduplicate → rank_search_items → _resync_published_pages`，
   即使 `netease_wait_s` 到点时聚合还没跑完，第一页分配的也已是排好序的顺序；
2. **首屏多等一小会儿**：`search_rank_wait_s`（默认 1.5s），让「补全 + 重排」在分配页码之前完成；
3. **分页按 guid 去重分配**（替代下标切片）：池子重排后仍「永远取最好的剩余条目」，且不跨页重复；
4. **已发布页重排后对齐**（`_resync_published_pages()`）：只换序、不增删，任何一次下拉刷新即为排好序的顺序。

### 6.4 确定性：排序绝不依赖「缓存预热进度」

`_search_item_has_poster()` 只认「条目 / `meta_cache` 里的封面 URL」（= `build_online_track` 真正返回给 App 的 coverUrl），
**刻意不看磁盘封面缓存** —— 否则 `_prefetch_online_covers` 异步落盘过程中「有海报」判定会翻转，同关键词连搜两次顺序就不一致。
旧辅助函数 `_has_cached_poster()` 已删除。

### 6.5 可观测性探针（写 `access_probe.log`）

```
[searchrank]  n=71 poster(top30) 30->30 lossless(top10)=10 moved=0 head=稻香(治愈版)
[poolrank]    kw=稻香 13:稻香(治愈版) 13:《稻香》童声 …          ← 池子真实档位
[pagealloc]   kw=稻香 p=1 n=30 13:稻香(治愈版) …                ← 首屏真实档位
[searchenrich] cap=30 pending=9 ne=0 kw=1 resolved=22
```

格式 `<有海报><音质档>:<标题前 8 字>`，可直接肉眼核验「排序是否真的单调」。

新增开关（`.env` 可覆盖，全部有默认值，不配即生效）：

```ini
FNMUSIC_SEARCH_RANK='cover_quality'     # 排序策略：cover_quality（海报优先）/ quality_cover（音质优先）/ off
FNMUSIC_SEARCH_ENRICH='true'            # 是否补全搜索结果的海报与音质档
FNMUSIC_SEARCH_ENRICH_LIMIT=30          # 只补第一屏条数
FNMUSIC_SEARCH_ENRICH_WAIT_S=1.5        # 聚合末尾等补全的上限
FNMUSIC_SEARCH_RANK_WAIT_S=1.5          # 首屏等「补全 + 重排」落定的上限
```

**实测（真机端到端，搜索 周杰伦 / 稻香 / 空心）**：首屏有海报占比 **21~30 / 30**（top10 恒为 100%），
首屏前 10 位**全部 `ext=flac` + 有封面**（v54 为全部 `ext=mp3`、无封面），搜索页封面接口 `/static/cover`
**0.001~0.002s**（v54 现抓 ~4s → 手机端超时 → 占位图），同一关键词重复搜索顺序一致。
验收报告见 [`reports/fnmusic-v55-报告.md`](reports/fnmusic-v55-报告.md)。

---

## 验证与报告

| 脚本 | 内容 |
|---|---|
| [`patches/_v51_check.py`](patches/_v51_check.py) | 29 项沙箱单元（收藏集合、删除安全边界、陈旧 `.ref` 目录一致性） |
| [`patches/_v52_check.py`](patches/_v52_check.py) | 73 项（= v51 全部 + v52 新增 44 项：鉴权头识别/TTL、待办合并/兜底、探针不泄漏 cookie） |
| [`patches/_v53_check.py`](patches/_v53_check.py) | 117 项（= v52 全部 + v53 新增 44 项：歌词路由、映射、自愈安全边界） |
| [`patches/_v54_check.py`](patches/_v54_check.py) | **163 项**（= v53 全部 + v54 新增 46 项：歌词落点回归守卫、存量提升、越界安全边界） |
| [`patches/_v55_check.py`](patches/_v55_check.py) | **188 项**（= v54 全部 163 项 + v55 新增 25 项：补全 / 排序 / 首屏分页免疫 / 确定性判据） |
| [`patches/_v52_e2e_*.sh`](patches/) | 真机端到端（下载侧 / 删除侧 / 兜底通道） |
| [`patches/_v54_e2e.py`](patches/_v54_e2e.py) | 真机端到端：复现「收藏后歌词必须贴身」（真实 token 调真实收藏接口，含清理与状态还原） |
| [`patches/_v55_e2e.py`](patches/_v55_e2e.py) | 真机端到端：搜索 周杰伦 / 稻香 / 空心，首屏「有海报 + 无损」优先、顺序稳定、封面接口 200 真图（4/4 轮全绿） |
| [`patches/_build_v50.py`](patches/_build_v50.py) ~ [`_build_v55.py`](patches/_build_v55.py) | 带自校验断言的增量构建器（每步 `src.count()` 断言 + `compile()` 语法校验） |

验收报告（含真机证据、DB 前后对比、回滚步骤）见 [`reports/`](reports/)：

- [`reports/fnmusic-v51-报告.md`](reports/fnmusic-v51-报告.md) —— 收藏即下载 / 取消即删
- [`reports/fnmusic-v52-报告.md`](reports/fnmusic-v52-报告.md) —— 自动扫库
- [`reports/fnmusic-v53-报告.md`](reports/fnmusic-v53-报告.md) —— 歌词归属与孤儿自愈
- [`reports/fnmusic-v54-报告.md`](reports/fnmusic-v54-报告.md) —— 歌词贴身（修复 v53 回归）
- [`reports/fnmusic-v55-报告.md`](reports/fnmusic-v55-报告.md) —— 搜索结果有海报 + 高音质优先排序

### 沙箱里怎么跑单元测试

```bash
# 造一个沙箱 proxy 目录：真实 recommend.py / cache_gc.py 兄弟文件 + 假 music.db
mkdir -p /tmp/v53t/proxy && cd /tmp/v53t/proxy
cp <本仓库>/proxy/{app.py,recommend.py,cache_gc.py,version.py,__init__.py} .
# 关键：所有相对导入与 FNMUSIC_HOME 都指向沙箱，绝不碰真机状态
FNMUSIC_HOME=/tmp/v53t/home <插件虚拟环境>/bin/python ../_v53_check.py
```

---

## 与上游的关系

- 基线：上游 **v1.6.0**（本仓库 `VERSION` 文件未改，便于对照）；
- 差异：仅 `proxy/app.py`；
- 上游若后续发布新版本，合并方式是把 `proxy/app.py` 的改动按主题重新应用（`patches/_build_v5*.py`
  是带断言的增量构建器，可作为改动点的索引）。

## 已知限制

1. **自动扫库依赖 App 凭证**：若长时间完全没有带鉴权的 App 请求，待办会一直挂着直到下次 App 活动。
   这是上游扫描接口强制鉴权导致的，非本分支可绕过。
2. **收藏即下载**需要在 `.env` 里打开 `FNMUSIC_FAV_DL_ON_FAVORITE`；
   云盘曲库写入即上传，请留意流量与配额。
3. **孤儿歌词自愈**只处理「本插件写入」的歌词，不会清理飞牛自己下载的歌词。

## 许可

沿用上游 [LICENSE](LICENSE)。
