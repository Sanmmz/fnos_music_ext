# 与原版的区别

> 本仓库是 [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) 的**增强分支**，
> 基线为上游 **v1.6.0**。
>
> 一句话概括：**上游把「在线曲库」接通了，本分支把它修到「手机 App 上真的能用」。**

---

## 目录

- [TL;DR：差异量化](#tldr差异量化)
- [上游的问题清单（本分支针对什么）](#上游的问题清单本分支针对什么)
- [逐项对比](#逐项对比)
  - [A. 手机 App 可用性（整列空白 / 加载超时）](#a-手机-app-可用性整列空白--加载超时)
  - [B. 播放取流（点播放 404）](#b-播放取流点播放-404)
  - [C. 收藏落盘与清理](#c-收藏落盘与清理)
  - [D. 云盘曲库适配（自动扫库）](#d-云盘曲库适配自动扫库)
  - [E. 歌词归属](#e-歌词归属)
  - [F. 可观测性](#f-可观测性)
  - [G. 搜索结果排序（有海报 + 高音质优先）](#g-搜索结果排序有海报--高音质优先)
  - [H. 喜马拉雅有声书音源（v76–v78）](#h-喜马拉雅有声书音源v76v78)
- [本分支没有改的地方（边界）](#本分支没有改的地方边界)
- [兼容性与安装](#兼容性与安装)
- [哪些改动适合直接回馈上游](#哪些改动适合直接回馈上游)
- [致谢与许可](#致谢与许可)

---

## TL;DR：差异量化

| 指标 | 上游 v1.6.0 | 本分支（v55 / v78） | 说明 |
|---|---|---|---|
| 改动文件 | — | `proxy/app.py` + `proxy/admin_ui.html` + `docker-compose.yml` + **新增 `xmly-service/`** | v55 阶段只改 `proxy/app.py`；v76 起新增喜马拉雅音源服务 |
| `app.py` 行数 | 3,080 | 5,849 | +90% |
| 函数个数 | 117 | 228 | **+111 个新增** |
| HTTP 路由 | 28 | 29 | **+1**（`/music/api/v1/play-history/delete`） |
| 配置项（`CONF`） | 29 | 52 | **+23 个**（全部有默认值，可不配） |
| **删除的函数** | — | **0** | 上游函数**全部保留、无一改名** |
| **删除的路由** | — | **0** | 完全向后兼容 |

> 这个对照表可以用 [`patches/_diff_upstream.py`](patches/_diff_upstream.py) 自动复现
> （把上游 `proxy/app.py` 与 `VERSION` 换成新版本即可再跑一遍）。

**结论：这是一个纯增量补丁 —— 上游原有逻辑一个都没删。**

---

## 上游的问题清单（本分支针对什么）

按用户实际感受到的顺序：

| # | 现象 | 上游根因 |
|---|---|---|
| 1 | 手机 App 里收藏 / 最近播放**整列空白消失** | 在线条目入库时标题为空、时长为 0、封面 URL 无效 |
| 2 | 列表能出但要等几十秒，或干脆超时 | 在线封面**现抓**，约 4 秒/张 × N 条 |
| 3 | 每日推荐 20 首里**15 首点播放 404** | 网易云取流走 musicbox，耗时 1.2~94 秒，被 4 秒硬超时打断 |
| 4 | 播放历史**删不掉在线曲目** | `play-history/delete` 对在线 guid 报「无效参数」 |
| 5 | 「边听边存」**时有时无** | 落盘逻辑要求客户端发 `bytes=0-`，且 `os.replace` 放在被取消的生成器尾部 |
| 6 | 下载 / 删除后**曲库不自动刷新** | 曲库在 rclone 云盘挂载点上，FUSE **不产生 inotify 事件** |
| 7 | 播放在线音乐往曲库留**孤儿歌词** | 歌词落盘路径把云盘曲库当兜底目录 |
| 8 | 收藏后音频进了曲库，**歌词不贴身** | 歌词落点被 `cache/` 里的旧副本劫持（本分支 v53 的回归，v54 已修） |

---

## 逐项对比

### A. 手机 App 可用性（整列空白 / 加载超时）

| 项 | 上游 v1.6.0 | 本分支 |
|---|---|---|
| 封面缺失时 | **直接返回 404** ⇒ App 渲染整行失败，**整列消失** | **四级兜底**，恒返回 `200` + 真实图片字节：`_online_info` → `meta_cache` → 网易云详情 → **内置占位 PNG** |
| 封面接口异常/未授权 | 可能返回 JSON 错误体 | 任何情况都**不返回 JSON、不返回 404**（占位图带 6 小时过期，真实封面恢复后不会被永久灰图盖住） |
| 封面尺寸 | 按客户端 `size` 逐个现抓（`120/400/800` 各抓一次） | 统一按 **300px 落盘缓存**，一份缓存覆盖所有档位；尺寸按 `size` 分档返回 |
| 封面获取时机 | **等客户端来拉时才现抓**（约 4s/张） | **落盘缓存** + 列表返回时**后台预取** + 抓图走**共享长连接** |
| 实测封面耗时 | ~4,000 ms | **0~2 ms** |
| 在线条目元数据 | `stub_online_info` 录制，只写 `{guid,"","",source}` ⇒ **title 空 / duration 0** | `_resolve_record_meta` 多后端补齐（`_online_info` → 搜索缓存 → 酷我 `musicInfo?mid=` → 网易云 `api/v3/song/detail`） |
| 解析不出标题的在线曲目 | 照常入库 ⇒ 一条空标题拖垮整列 | **一律不入库**（宁可少一条） |
| 时长归一化 | 无 | `_norm_duration_s`：洛雪把酷狗的「秒」当毫秒返回（0.309 秒 → 309 毫秒），自动 ×1000 |
| 封面 URL 归一化 | 无 | `_normalize_cover_url`：126.net 原图 7 MB → `?param=300y300` 后 102 KB |
| 在线元数据查询 | 每次操作**逐个串行往后端查** | `_online_info` 外层 **TTL 缓存（默认 300s）** + 并发去重 + 三后端 `asyncio.gather` 并发 + 列表返回时预热 |
| 在线历史模式 | 只有一种（在线条目必进收藏/历史） | 新增 `ONLINE_HISTORY_MODE`（`full` / `off`），**改文件 5 秒内生效、无需重启**，出问题可一键退回「只有本地曲目」 |

**新增配置**：`FNMUSIC_INFO_TTL`

---

### B. 播放取流（点播放 404）

| 项 | 上游 v1.6.0 | 本分支 |
|---|---|---|
| 网易云播放地址解析 | **只走 musicbox**（1.2~94 秒/次）⇒ 被 `track/stream` 的 4 秒硬上限打断，**每日推荐 20 首里 15 首 404** | **新增直连快通道**：`POST music.163.com/api/song/enhance/player/url`（**借** musicbox 的登录态 cookie），musicbox **原样保留为兜底** |
| 取流地址缓存 | 无 | `_STREAM_URL_CACHE`：TTL 600s、上限 512、**cookie 变化即整体作废** |
| 取流地址预热 | 无 | `_prefetch_stream_urls()`：挂在收藏 / 歌单曲目 / 历史三个列表端点，默认 20 条、并发 3，**只走直连**（绝不触发慢兜底） |
| VIP 试听片段 | 无判断 ⇒ 可能给用户放**只有 30 秒的试听片段** | **`freeTrialInfo` 非空一律判失败**，回落 musicbox（比 404 更糟的坑，已堵） |
| 实测 | 可播 **5/20**，单首首字节 ~4000 ms | 可播 **20/20**（全 FLAC），单首首字节 **~800 ms** |
| musicbox 其余职责 | — | **一行都没改**：搜索 / 批量详情+可播过滤 / 歌曲详情 / 歌词 / 个性化日推 / 榜单 全部原样 |

**新增配置**：`FNMUSIC_NETEASE_DIRECT`、`FNMUSIC_NETEASE_COOKIE_FILE`、`FNMUSIC_STREAM_URL_TTL`

---

### C. 收藏落盘与清理

| 项 | 上游 v1.6.0 | 本分支 |
|---|---|---|
| 何时落盘 | **所有**在线曲目播放时都尝试 tee 进曲库 | **只对收藏的曲目落盘**（`FNMUSIC_TEE_FAVORITES_ONLY`），非收藏只写本地滚动缓存，**不碰云盘** |
| 落盘方式 | 依赖客户端行为：要求**无 Range 或 `bytes=0-`**，且 `os.replace` 放在生成器尾部 | **服务端独立整轨下载**（`_download_favorite_media`），不依赖 Range 形态与断开时序 ⇒ 确定性落盘 |
| 触发时机 | 仅播放时 | **收藏即下载** + **播放补漏** 双触发 |
| 文件名 | 解析不出标题可能写出 `unknown.mp3` | 解析不出标题**直接放弃落盘**，绝不写 `unknown` |
| 取消收藏 | **不删文件**（曲库里留垃圾） | **取消即删**：音频 + 同名 `.lrc` + `.ref`/`.lyricref`（`delete_materialized_media`），且只在曲库/缓存目录内动手 |
| 曲库目录变更 | `recalled_media_stem()` **无条件**复用旧词干 ⇒ 曲库换目录后 `os.replace` 报 `FileNotFoundError` | `_same_dir()`（realpath 比较）确认同目录才复用；跨文件系统 rename 抛 EXDEV 时用 `shutil.move` 兜底 |
| `/stream` 开销 | **每个请求都开一次 SQLite** 并对云盘挂载做 stat | `detect_library_dir()` 加 **30 秒缓存** |

**新增配置**：`FNMUSIC_TEE_FAVORITES_ONLY`、`FNMUSIC_FAV_DL_ON_FAVORITE`、`FNMUSIC_FAV_DL_ON_PLAY`、`FNMUSIC_FAV_DL_DELETE_ON_UNFAV`、`FNMUSIC_FAV_DL_CONCURRENCY`、`FNMUSIC_FAV_DL_TIMEOUT_S`、`FNMUSIC_FAV_DL_MAX_BYTES`

---

### D. 云盘曲库适配（自动扫库）

| 项 | 上游 v1.6.0 | 本分支 |
|---|---|---|
| 曲库自动刷新 | 依赖 `sharedLibraryFSEventHandler`（inotify 驱动）。**曲库在 rclone WebDAV 挂载点上时 FUSE 不产生 inotify 事件**（实测事件数 = 0）⇒ 新增/删除的文件飞牛完全不知道，**必须手点「扫描」** | **落盘 / 删除成功后主动调飞牛扫描接口** |
| 扫描接口鉴权 | — | 接口强制鉴权（无凭据 401），插件自己签不出 token ⇒ 设计成**「谁有凭证谁去调」**：① 触发它那次请求头还新（≤90s）就 3 秒后自己发；② 否则留给**下一次带鉴权的 App 请求**兜底消化（App 打开时会持续轮询，必然命中） |
| 实测 | 下载后**不刷新** | 下载后 **9 秒**被扫到并入库；取消收藏后该行被标记 `is_physical_file_deleted=1` |

**新增配置**：`FNMUSIC_AUTO_SCAN`、`FNMUSIC_AUTO_SCAN_DELAY_S`、`FNMUSIC_AUTO_SCAN_AUTH_TTL_S`、`FNMUSIC_AUTO_SCAN_SCAN_ALL`

> 凭证只在内存中短时保留，且**绝不写进日志**（单元测试有专门断言）。

---

### E. 歌词归属

这是本分支改动最曲折的一块 —— 因为「歌词该放哪」在上游是**没有定义**的。

| 项 | 上游 v1.6.0 | 本分支 |
|---|---|---|
| 歌词落点规则 | `lyric_cache_path()` 在没有已落盘音频时，**把 `detect_library_dir()`（云盘曲库）当兜底** ⇒ 播放任意在线曲目都会往云盘**上传一个没有音频的 `.lrc`**（用户手工清过 56 个孤儿歌词） | 明确三档归属，**音频到哪，歌词到哪**：<br>音频在曲库 → 曲库同名 sidecar<br>音频只在 `cache/` → `cache/` 内同名 sidecar<br>都没有 → `cache/<guid>.lrc`（**绝不写云盘曲库**） |
| 歌词路径映射 | 与音频**共用同一个 `.ref` 槽** ⇒ 后写的覆盖前写的，歌词映射丢失（表现为反复重抓歌词 + 孤儿永久留存） | 歌词占**独立映射** `<guid>.lyricref`；`find_lyric_file()` 优先读它 |
| 孤儿歌词清理 | 无 | `sweep_orphan_lyrics()`：只清「**插件自己写下、且音频已不存在**」的曲库歌词（靠自维护的 `.ref`/`.lyricref` 识别，**绝不动飞牛自己管理的歌词**）；启动后 15s 一次 + 每 30 分钟复扫 + 取消收藏后顺带扫 |
| 歌词落点被缓存副本劫持 | — | `promote_one_lyric()` / `promote_library_lyrics()` 把「音频已在曲库、歌词却在 `cache/`」的歌词**提升**成曲库同名 sidecar（启动后 + 周期 + 每次整轨落盘后 + 歌词读取时幂等纠正） |
| 删除 | 不处理 | 取消收藏时音频与歌词一起删（含 `.lyricref`） |

**新增配置**：`FNMUSIC_LYRIC_ORPHAN_GC`、`FNMUSIC_LYRIC_ORPHAN_GC_MIN_AGE_S`、`FNMUSIC_LYRIC_ORPHAN_GC_INTERVAL_S`、`FNMUSIC_LYRIC_PROMOTE`

> 自愈的安全边界（每条都有单元测试）：词干必须来自 `cache/` 下的 `.ref`（只可能是我方写的）、
> 必须落在**曲库目录**内、该词干下**确实存在音频**才动手、删除只允许发生在曲库/缓存目录内。

---

### F. 可观测性

| 项 | 上游 v1.6.0 | 本分支 |
|---|---|---|
| 请求探针 | 无 | `_access_probe` 中间件 → `~/fnmusic_ext/access_probe.log`，记录 `方法 路径 \| 状态码 \| 耗时 \| Content-Type \| len`（>12 MB 自动清空）。**排查手机端问题的第一入口**：App 请求带 `?lan=zh-CN`，可据此区分 App 与网页/脚本请求 |
| 业务探针 | 无 | `[tee]` `[favdl]` `[favdel]` `[scanreq]` `[lyricgc]` `[lyricpromo]` `[lyricshadow]` 等，每步都能对账 |
| `healthz` | 基础状态 | 增加 `netease_direct` 统计块（`cookie_ok` 主动读一次 cookie.txt，否则懒加载恒 false 会误导排障） |
| 删除曲目 | 无接口 | 新增路由 `POST /music/api/v1/play-history/delete`（修复上游对在线 guid 报「无效参数」） |

---

### G. 搜索结果排序（有海报 + 高音质优先）

这是 v55 新增的一块 —— 上游搜索没有「质量」维度，结果按音源返回顺序拼接。

| 项 | 上游 v1.6.0 | 本分支 |
|---|---|---|
| 搜索结果顺序 | 按任务创建顺序拼接，**谁先返回谁靠前**；在线条目**天生没有封面 URL**（lx 恒空、musicdl 真机超时）、音质是**伪造展示值**（任何歌都显示 320000）⇒ 首屏基本「没海报 + 标称 mp3」 | 按 `(-有海报, -音质档, 原始顺序)` **稳定重排**，把「带封面且高码率/无损」顶到第一屏 |
| 封面 / 音质来源 | 无 | **补全层** `_enrich_search_items()`：网易云**一次批量详情**（`POST music.163.com/api/v3/song/detail`，1 次 / 25 首 / 0.171s，拿到 `al.picUrl` + `sq/hr/h/m/l` 真音质档）+ 酷我单曲详情（`wapi.kuwo.cn/api/www/music/musicInfo?mid=`，0.11s/首，`Semaphore(6)` 并发），只补第一屏前 30 条 |
| 音质档判定 | 只信扩展名硬编的展示值 | `_search_item_quality_rank()`：取「扩展名推导」与「补全结果」**较大者**（无损 ext / 码率 ≥900k=3，高码有损 ext / ≥256k=2，其它有损=1），只影响排序、不改 `ext`、对取流零风险 |
| 首屏是否真的被重排 | 序号在聚合末尾分配，重排改不了首屏 | **首屏四层保证**：聚合内边收边排 + 首屏多等 `search_rank_wait_s`（1.5s）+ 分页按 guid 去重分配 + 已发布页重排后对齐（`_resync_published_pages`） |
| 顺序稳定性 | — | 「有海报」判据**只看条目 / `meta_cache` 的封面 URL，不看磁盘预热进度** ⇒ 同一关键词重复搜索顺序一致 |
| 搜索页封面 | 现抓（约 4s/张）→ 手机端超时 → 占位图 | 接入与每日推荐同款的 `_prefetch_online_covers()` 后台预取，封面接口 0.001~0.002s |

**新增配置**：`FNMUSIC_SEARCH_RANK`（默认 `cover_quality`；可选 `quality_cover` / `off`）、
`FNMUSIC_SEARCH_ENRICH`（默认 `true`）、`FNMUSIC_SEARCH_ENRICH_LIMIT`（默认 `30`）、
`FNMUSIC_SEARCH_ENRICH_WAIT_S`（默认 `1.5`）、`FNMUSIC_SEARCH_RANK_WAIT_S`（默认 `1.5`）

**实测**：搜索 周杰伦 / 稻香 / 空心，首屏有海报占比 **21~30 / 30**（top10 恒为 100%），
首屏前 10 位**全部 `ext=flac` + 有封面**（v54 为全部 `ext=mp3`、无封面）。单元 **188 / 188 PASS**，真机端到端 **4 / 4 轮全绿**。

---

### H. 喜马拉雅有声书音源（v76–v78）

这是本分支**唯一一个上游完全没有的能力**——上游只有音乐音源，没有有声书。
逐版说明见 [`v76-变更说明.md`](v76-变更说明.md)、[`v77-变更说明.md`](v77-变更说明.md)、
[`v78-变更说明.md`](v78-变更说明.md)。

| 项 | 上游 v1.6.0 | 本分支 |
|---|---|---|
| 有声书 | **无** | 新增 `xmly-service/`（独立容器，宿主 `127.0.0.1:8774`）+ `docker-compose.yml` 里的 `xmly` service |
| 内容模型 | — | **一个歌单 = 一部小说 = 喜马拉雅的一个专辑**；歌单 guid `online:playlist:xmly:<albumId>`，单集 guid `online:xmly:<trackId>:<albumId>` |
| 与音乐搜索的关系 | — | **触发词劫持**：`小说 …` / `喜马拉雅 …` / `xmly …` 才走喜马拉雅；不带触发词时与原有链路完全隔离，互不干扰 |
| 播放取流 | — | `/mobile-playpage/track/v3/baseInfo` 返回加密串 → **AES-128-ECB 解密**得到真实 m4a 直链 |
| VIP | — | 管理页「音源」→ 🎙️ 喜马拉雅 → **扫码登录**（base64 二维码 + 2s 轮询），cookie 落 `xmly-data/` |
| 小说海报 | — | v77 修：`coverId` 必须是 `/static/cover` 能解析的 **guid**，不能填外链 URL；`xmcdn.com` 走 `!op_type=3&columns=N&rows=N` 现算缩略图（686 KB → 47 KB → 15 KB） |
| 管理页搜索 | 「歌单综合搜索」 | 「**综合搜索**」+ 搜索栏左侧「**歌单 / 小说**」分段控件（`kind=playlist` / `kind=novel`） |
| 歌单曲目分页 | `size=-1`（一次给全）被 `if size < 1: size = 50` 兜成 50 ⇒ **上千集的小说只显示前 50 集** | v78 修：`_page_size()` / `_slice_page()`，`size <= 0` 视为一次给全（`_PAGE_ALL_CAP=5000` 兜底） |

**新增配置**：`FNMUSIC_XMLY_ENABLED`、`FNMUSIC_XMLY_URL`、`FNMUSIC_ADMIN_PORT`

> ★ 两个值得记住的坑：
> ① `coverId` 必须是 guid，填外链会让 App 的封面请求解析不出音源 ⇒ 海报整块空白；
> ② `size=-1` 这类「约定值」必须在 `if size < 1: size = 50` 之类的兜底**之前**处理，
>    否则约定分支会变成永不进入的死代码。

---

## 本分支没有改的地方（边界）

明确说明**没做什么**，便于你判断风险：

1. **没有修改官方程序与数据库结构**：仍然沿用上游的 inode/socket 接管方式，官方程序与 DB schema 一行未动。
2. **没有替换 musicbox**：直连**只替代它一个函数**（解析网易云播放地址）；其余 6 类职责（搜索 / 批量详情+可播过滤 / 歌曲详情 / 歌词 / 个性化日推 / 榜单）**一行未改**，它仍是登录态持有者与 weapi 签名方。
3. **没有动上游的任何函数名与路由**：0 删除、0 改名，纯增量。
4. **没有改变 App 端的渲染逻辑**：那些是官方 App 的行为，服务端无法修改；本分支只保证**服务端返回的数据是干净、完整、及时的**。
5. **没有内置任何账号凭据**：网易云登录仍走上游的 `./netease_login.sh` 扫码；直连只是**借用**那份 cookie。

---

## 兼容性与安装

- **安装方式与上游完全一致**：`install.sh` / `extend.sh` / `restore.sh` 等一个字节都没改；
  `docker-compose.yml` 只**追加**了 `xmly` 一个 service 块，其余未动。
- **也可以用 docker-compose 部署**：compose 负责 4 个音源服务，核心代理（需接管 Unix Socket）
  仍由宿主机 systemd 运行。完整步骤见 README 的「方式二：docker-compose 部署」。
- **新增的 23 个配置项全部有默认值**，`.env` 不填也能跑；想用「只对收藏落盘」「自动扫库」「搜索结果排序」按 `PATCHES.md` 里的清单打开即可。
- **回滚很简单**：换回上游的 `proxy/app.py` 并重启服务即可（本分支未改数据结构）。
- 本分支自带 4 份验收报告（[`reports/`](reports/)）与可复现脚本（[`patches/`](patches/)），
  单元验收共 **188 项**（`patches/_v55_check.py`）。

---

## 哪些改动适合直接回馈上游

按「是否属于纯 bugfix、是否会影响上游原有设计」分类：

**适合直接提 PR（纯 bugfix，无设计争议）**

1. 封面接口不能返回 404 / 不能返回 JSON —— 这是 App 整列消失的直接原因；
2. `_prefetch_online_meta` 是 `async def` 但调用点全是**裸调用**（无 `await` 也无 `create_task`）⇒ 协程从未执行，**元数据预热一直是死代码**；
3. `asyncio.create_task()` 不留引用时任务可能被 GC ⇒ 需要强引用池；
4. `detect_library_dir()` 每个 `/stream` 都开一次 SQLite ⇒ 加短 TTL 缓存；
5. 时长（秒/毫秒）与封面 URL 尺寸两个归一化；
6. `play-history/delete` 对在线 guid 的参数校验；
7. 曲库目录变更后陈旧 `.ref` 导致 `FileNotFoundError`。

**建议先讨论再提（涉及设计取舍）**

8. 网易云直连快通道（上游坚持「只走 musicbox」，直连是**加一条**而非替换，但仍需上游认可）；
9. 「只对收藏落盘」的默认行为（上游是所有曲目都 tee）；
10. 歌词三档归属 + 自愈机制（上游此前没有明确定义）；
11. 借 App 凭证触发扫库（上游扫描接口强制鉴权，属架构性选择）。

**v55 新增（设计取舍明显，建议先讨论）**

12. 搜索结果「有海报 + 高音质优先排序」：改变了上游「按音源返回顺序拼接」的默认行为，
    且依赖网易云 / 酷我批量详情补全（上游搜索本身不补封面与真音质）；
    若上游接纳，建议保留 `FNMUSIC_SEARCH_RANK=off` 这个**完全回退**开关。

---

## 致谢与许可

- **上游项目**：[javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext)（本分支的全部基础能力都来自它）；
  以及它致谢的 [musicbox](https://github.com/darknessomi/musicbox)、
  [musicdl](https://github.com/CharlesPikachu/musicdl) 与洛雪风格解析生态。
- **许可**：沿用上游 [LICENSE](LICENSE)。本分支对 `proxy/app.py` 的改动同样以该许可发布。
- 上游 README 的免责与版权声明对本分支**同样适用**，请一并遵守。
