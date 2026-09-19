# 更新日志 (Changelog)

本项目所有显著变更均记录于此文件。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.6.0+patch3] - 2026-09-18

### 新增

- **搜索结果「有海报 + 高音质」优先排序**：搜索在线歌曲时，按 `(-有海报, -音质档, 原始顺序)`
  稳定重排，把「带封面且高码率/无损」的条目顶到第一屏前端。
  - **补全层** `_enrich_search_items()`：网易云**一次批量详情**
    （`POST music.163.com/api/v3/song/detail`，1 次请求 / 25 首 / 0.171s，拿到 `al.picUrl` 封面
    + `sq/hr/h/m/l` 真实音质档）+ 酷我单曲详情
    （`wapi.kuwo.cn/api/www/music/musicInfo?mid=`，0.11s/首，`Semaphore(6)` 并发），只补第一屏前 30 条；
  - **排序层** `rank_search_items()`：稳定排序，音质档取「扩展名推导」与「补全结果」**较大者**，
    避免「显示 AAC 却排到 mp3 后面」；
  - **首屏四层保证**：聚合内边收边排 + 首屏多等 `search_rank_wait_s`（1.5s）+ 分页按 guid 去重分配
    + 已发布页重排后对齐（`_resync_published_pages`），确保排序一定作用在第一屏；
  - **确定性**：「有海报」判据只认条目 / `meta_cache` 里的封面 URL，**不看磁盘预热进度**，
    同一关键词重复搜索顺序一致；搜索页接入与每日推荐同款的封面后台预取 `_prefetch_online_covers()`。

### 新增配置

- `FNMUSIC_SEARCH_RANK`（默认 `cover_quality`；可选 `quality_cover` / `off`）
- `FNMUSIC_SEARCH_ENRICH`（默认 `true`）、`FNMUSIC_SEARCH_ENRICH_LIMIT`（默认 `30`）、
  `FNMUSIC_SEARCH_ENRICH_WAIT_S`（默认 `1.5`）、`FNMUSIC_SEARCH_RANK_WAIT_S`（默认 `1.5`）

### 验收

- 沙箱单元 **188 / 188 PASS**（`patches/_v55_check.py`）；真机端到端 **4 / 4 轮全绿**
  （搜索 周杰伦 / 稻香 / 空心，首屏有海报占比 21~30/30、top10 恒为「有封面 + 无损」、顺序稳定）。
- 详见 [`reports/fnmusic-v55-报告.md`](reports/fnmusic-v55-报告.md)。

## [1.6.0+patch2] - 2026-09-18

### 修复

- **收藏后歌词不贴身（patch1 的回归，严重）**：「收藏前播过一次」的在线曲目，
  其歌词已存在 `cache/<guid>.lrc`；收藏整轨下载把音频落进曲库后，
  歌词仍被写回 `cache/`，**曲库音频旁边永远没有同名 `.lrc`**。
  根因是 `lyric_cache_path()` 把「已有歌词优先」放在了「音频已在曲库」之前，
  而 `find_lyric_file()` 会命中 `cache/` 里的旧副本 ⇒ 落点被劫持。
  现将「音频已在曲库」提到最前（**歌词跟着音频走**），并收紧 `write_lyric_cache()`
  的短路条件（从「任意位置内容一致」改为「目标位置内容一致」），
  同时清掉 cache 里的影子副本。

### 新增

- **歌词「贴身」自愈**：`promote_one_lyric()` / `promote_library_lyrics()` 把
  「音频已在曲库、歌词却留在 `cache/`」的**存量**歌词补成曲库同名 sidecar；
  启动后 12 秒补一次 + 周期复扫 + 每次整轨下载落盘后补位；
  歌词读取路径也做幂等纠正。开关 `FNMUSIC_LYRIC_PROMOTE`（默认开）。

## [1.6.0+patch] - 2026-09-18

> 增强分支。在上游 v1.6.0 基础上叠加，**改动仅涉及 `proxy/app.py`**。
> 详细说明见 [PATCHES.md](PATCHES.md)，验收报告见 [`reports/`](reports/)。

### 修复

- **手机 App 收藏/最近播放整列空白（严重）**：在线条目入库时标题为空、时长为 0、封面 URL 无效
  → 废弃 `stub_online_info`，改为多后端补齐元数据，**解析不出标题的在线曲目一律不入库**；
  同时修正时长（洛雪把酷狗「秒」当毫秒）与封面尺寸归一化（126.net 原图 7MB → `?param=300y300` 102KB）。
- **列表整列渲染超时（严重）**：在线封面是**现抓**的，约 4 秒/张 × N 条
  → 封面按 guid **落盘缓存** + 列表返回时后台预取 + 抓图共享长连接，实测 4000ms → **0~2ms**。
- **封面接口不再 404 / 不再返回 JSON**：四级兜底（`_online_info` → `meta_cache` → 网易云详情 → 占位 PNG），
  任何情况都返回 200 + 真实图片字节；尺寸按客户端 `size` 分档。
- **每日推荐 20 首有 15 首点播放 404（严重）**：取流走 musicbox 耗时 1.2~94 秒，被 4 秒硬超时打断
  → 新增网易云**直连取流快通道** + musicbox 原样兜底 + 取流地址缓存/预热，可播 **5/20 → 20/20**，
  单首首字节 **4000ms → ~800ms**。（`freeTrialInfo` 非空判失败，避免 VIP 曲目只放 30 秒试听片段。）
- **播放历史「移除在线曲目」被参数校验拒绝**：修复 `play-history/delete` 端点。
- **「边听边存」时有时无**：真实播放器按 1MB 定长窗口取流，旧逻辑只认 `bytes=0-`
  → 改为**服务端独立整轨下载**，不依赖 Range 形态与断开时序。
- **陈旧 `.ref` 把文件写回已不存在的旧曲库**（`FileNotFoundError`）：复用旧映射前校验同目录，
  并给改名加 `shutil.move` 兜底（跨文件系统 `rename` 会抛 EXDEV）。

### 新增

- **自动扫库**：曲库迁到 rclone 云盘挂载点后 FUSE 不产生 inotify 事件，飞牛自动扫描彻底失效
  → 落盘/删除成功后**借 App 凭证主动调飞牛扫描接口**（3s 合并窗口 + 下次带鉴权请求兜底消化）。
  实测下载后 **9 秒**被扫到并入库，删除后被标记 `is_physical_file_deleted=1`。
- **收藏即下载（可开关）**：只对收藏的在线曲目落盘，取消收藏即删除对应音频 / `.lrc` / `.ref`。
- **孤儿歌词自愈**：歌词落地归属重定（云盘曲库不再产出无主歌词）+ 歌词专用映射 `.lyricref`
  + 定时清扫存量孤儿（带最小年龄、目录白名单、同名词曲检查等安全边界）。

### 变更

- `_online_info` 增加 TTL 缓存（默认 300s）与并发去重，缓存键含凭据与音源配置；
  三后端元数据解析改为并发。
- `detect_library_dir()` 增加 30s 缓存（此前每个 `/stream` 都开一次 SQLite 并对云盘挂载做 stat）。
- 修掉两个真 bug：`_prefetch_online_meta` 因裸调用协程而**从未执行**；
  `asyncio.create_task()` 不留引用时任务可能被 GC（新增 `_BG_TASKS` 强引用池）。

## [1.6.0] - 2026-09-17

### 修复

- **官方应用升级后"还原/重装"死锁（严重）**：官方应用升级或重启时，新版实例会
  直接绑回原 socket 路径，而升级前的旧官方进程可能残活在上游路径
  （`trim_music_upstream.socket`）成为孤儿——重启官方应用也杀不掉它。此前
  `restore.sh`（含 `--full`）与重装在这种状态下都会拒绝执行，形成永久死锁。
  现在只要原路径已被内核验证为官方进程直连（播放不受任何影响），恢复与重装
  会直接回收上游孤儿 socket 文件：仅删除路径名、不动进程本身，日志会提示
  残留进程的 PID 供按需清理。真正无法识别身份的歧义布局仍保持拒绝。

### 新增

- **边听边存开关与保存路径配置**：新增 `FNMUSIC_TEE_SAVE_ENABLED`（默认开）、
  `FNMUSIC_TEE_SAVE_DIR`（默认空=自动探测飞牛共享曲库；配置后以配置为准，
  路径不可用自动回退默认并告警）。
- **关闭边听边存时的滚动试听缓存**：新增 `FNMUSIC_TEE_CACHE_MAX`（默认 2），
  仅在边听边存关闭时生效——完整试听的歌曲以 `online_源_歌曲id` 命名滚动缓存在
  cache 目录（重播秒开、不进官方曲库），超出数量自动淘汰最旧的；
  `restore.sh` 默认还原与 `--full` 均会清理这些滚动缓存（已存入曲库的歌曲与
  曲库引用 `.ref` 不受影响）。

## [1.5.0] - 2026-09-11

### 新增

- **音源原生每日推荐（取代大模型推荐）**：不再强制依赖大模型，
  每日推荐默认采信音乐源自身的推荐能力，按优先级单链逐级补齐 20 首：
  1. **网易真·每日推荐**：网易云音源启用且已登录时（支持手机 App 扫码），
     直接调用网易云每日推荐（`weapi`，个性化）；
  2. **网易免登录榜单**：网易云启用但未登录（或第 1 级不足）时，
     自动降级为网易热歌榜（NEMbox 内置 `toplist`，匿名可用）；
  3. **洛雪免登录榜单**：洛雪音源启用时，聚合酷狗移动端 TOP500、
     酷我 kbang 飙升榜与网易新歌速递（2026-09 全部实测免登录存活，
     经 Range 探活验证确保完整可播时长）；
  4. **大模型兜底**：仅当网易音源未启用且在 `.env` 中配置了
     `FNMUSIC_LLM_*` 时才调用大模型生成候选，保留随时切回能力；
  5. **关键词兜底**：全链路故障时由历史种子歌手与热门池搜索保底。
- **平台 ID 直连详情与可播性验证**：音源原生推荐直接返回平台歌曲 ID，
  直连批量详情接口并复用服务端 `filter_playable_song_ids`（自动过滤
  收费/VIP/仅试听片段），取代此前"大模型出歌名 → 三源搜索 → 模糊匹配"
  的高损耗链路，推荐曲目命中率与可播性大幅提升。
- **musicbox-service 新增推荐与榜单路由**：
  - `GET /api/v1/recommend/songs`：调用 NEMbox CLI `recommend songs`；
    未登录时结构化透传 `not_logged_in` 错误供代理降级；
  - `GET /api/v1/toplist`：支持查询榜单列表及指定榜单曲目水化。
- **lxmusic-service 新增免登录榜单路由**：
  - `GET /api/v1/recommend`：聚合 kg/kw/wy 三源免登录榜单，
    对齐现有 search item 统一曲目格式。
- **可观测性增强**：`/_ext/healthz` 与每日缓存 payload 记录各用户
  当日推荐的生效来源梯队（`tiers: ["netease-daily", ...]`），排障更清晰。

## [1.4.0] - 2026-09-11

### 新增

- **适配 2026-09-11 升级后的新版飞牛音乐**：核心接管机制与全部扩展功能
  （在线搜索合并、在线播放与边播边存、歌词、封面、多用户收藏、每日推荐、
  播放历史）与新版官方应用完全兼容，旧版官方应用同样可用。
  官方应用升级后若发现扩展未生效，重新执行 `./extend.sh` 即可恢复
  （会自动拉起音源并完成接管与验收）。

### 修复

- **重装不再清空大模型配置**：安装向导对"每日推荐"答 N 或留空时，
  此前会把 `.env` 中已保存的 `FNMUSIC_LLM_*` 以空值显式覆盖，导致每次重装都
  需重新填入 API Key。现改为：答 N/留空一律保留既有配置；仅显式传入
  `--disable-recommend` 才清除。
- **restore.sh 语义重设计**：
  - 默认：还原官方直连，同时停止并删除音源容器/宿主机服务，
    但完整保留 `.env` 与全部数据（网易云登录、缓存、在线收藏、播放历史、
    推荐缓存）——重新启用后无需重新填写任何配置；
  - `--full`：彻底清理——额外删除 `.env`（含历史备份）与上述全部数据目录
    及虚拟环境，仅保留代码本身，用于完全重置。

### 测试

- 新增：大模型配置保留语义、restore 清理范围（该删的删净、代码与未知文件保留）
  等行为级回归测试；修正个别用例在真实主机上的环境敏感断言。

## [1.3.1] - 2026-09-10

### 修复

- **修复接管中断后"装不上也还原不了"的死锁（严重）**：`takeover.py` 原先要求 socket 文件
  必须在归属记录（`ownership.json`）中登记过才允许删除，且只允许删除 `stale` 状态的文件。
  一旦上一次接管失败留下未登记的僵尸 socket 文件（官方 socket 已停放在
  `trim_music_upstream.socket`，而 `trim_music.socket` 是一个无人监听的残留文件），
  `publish` 与 `restore` 会同时以 `unrecorded/replaced socket; preserved` 失败并永久互锁。
  现改为：对**可证明不可达**的 socket 文件（`connect()` 返回 `ECONNREFUSED`、同一 inode
  连续两次观测一致）一律回收，并打印审计行；仍然拒绝删除任何存活（含代理自身）或身份不明
  的 socket 文件。安装与还原在任何历史失败残留之后都可继续。
- **修复 supervisor 回滚异常掩盖真实失败原因**：`supervise()` 的 `finally` 中执行回滚，
  回滚自身的异常会覆盖原始异常，导致日志只显示 `unrecorded/replaced socket; preserved`，
  真正的失败原因（例如拓扑冲突）被吞掉。现改为分别捕获并串行上报
  `<原始原因>; rollback failed: <回滚原因>`，且正常停止（SIGTERM）时不再被误判为失败。
- **修复归属记录不一致导致的死锁**：官方应用重启（PID/inode 变化）或代理崩溃后，
  `remember`/`publish`/`restore` 原先会以"归属变更"直接失败。因两类角色均由内核身份
  （`SO_PEERCRED` + `/proc/<pid>/exe`、`/_ext/livez` + peer PID）实证，记录仅作缓存，
  现改为带 `[takeover] note:` 审计行地采纳新的存活身份，并在移动后校验终态。
- **`restore-plan` 覆盖全部可恢复拓扑**：新增 `vacant-target-repair`（僵尸 target + 官方
  upstream）与 `official-direct`（含 upstream 僵尸文件清理）两类计划；`restore()` 会清理
  僵尸 upstream，并在确实不存在任何官方监听者时明确报错
  `no official listener present; restart the official music app`，不再留下自相矛盾的布局。
- **就绪窗口对齐**：supervisor 内部就绪判定由 30s 放宽到 55s，保持在 unit
  `ExecStartPost` 的 65s 之内，慢启动不再被内部判定抢先回滚。
- 回归测试同步更新：新增僵尸 target/upstream 回收、官方重启采纳、双活冲突下的
  错误串行上报等用例。

## [1.3.0] - 2026-09-09

### 新增

- **lxmusic 音源新增 tx（QQ音乐）与 kw（酷我）子源**：tx 搜索采用 QQ 官方免登录接口
  （musicu.fcg），kw 搜索采用酷我官方 r.s 老接口；直链解析经第三方链路 + Range 探活验证。
  `LX_SOURCES` 默认值由 `kg,wy,mg` 扩展为 `kg,wy,mg,tx,kw`。
- **第三方解析链路层（移植自洛雪社区聚合源 qdy v9.3 的链路清单与多链路回退架构）**：
  数据驱动注册表 + 连续失败熔断（3 次失败暂停 10 分钟）+ 链路健康状态暴露于 `/healthz`。
  2026-09-09 对 qdy 全部 10 条链路逐条实测后仅移植存活链路：长青 kw（302→酷我 CDN 无损
  FLAC）与溯音咪咕（320k 直链+歌词）；星海/念心/溯音 oiapi/汽水等 8 条已死链路不移植，
  后续复活时在注册表追加即可。`LX_THIRD_PARTY=0` 可整体关闭（退化为纯官方免登录直连）。
- **可播性验证（"搜得到必能播"）**：kg/wy 的 VIP/付费曲目不再按 pay_type/fee 元数据一票
  否决，改为"官方解析→第三方链路→Range 探活（200/206 且非 HTML）+ 试听碎片体积防护"全链路
  验证，通过才返回并携带 `verified: true` 标记；kw/tx/mg 搜索结果同样全部探活。试听片段、
  无版权、VIP 拦截（fail_process=4）等真不可播标记的过滤保持不变。

### 变更

- proxy 的 `is_playable_online_track` 对 `verified` 条目跳过收费元数据拦截（pay_type/price/
  fee），试听/无流/坏 URL 校验全部保留——服务端已用真实探活证明可播，元数据不再作为可播性
  的代理判断。
- mg 咪咕搜索的逐曲解析升级为完整"解析+探活"（官方接口失败自动回退溯音咪咕链路），此前仅
  校验 URL 存在性、未做存活性探测。
- lxmusic-service 版本号 1.0.0 → 1.1.0；extend.sh 子源探测清单扩展为 kg/wy/mg/kw/tx。

### 修复

- **升级合并不再丢失用户 .env 注释**：`env_merge` 此前在增量合并时静默丢弃用户手写的
  注释/非赋值行；现按原顺序去重后保留在文件末尾（连续合并不堆积），键值合并规则不变。

### 工程与测试

- **新增 GitHub Actions CI**（`.github/workflows/ci.yml`）：push 到 main/dev 与所有 PR 自动
  执行 shell 语法检查、Python 编译检查与全量 pytest（Python 3.11/3.13 矩阵）；另附
  shellcheck 静态检查（error 级，仅报告不阻断）。
- **测试套件单命令化**：新增 `pytest.ini`，`python -m pytest` 一条命令跑全部 proxy +
  各音源服务测试；修复 musicbox 与 lxmusic 测试的顶层 `app` 模块名冲突（改为独立模块名
  显式加载）；musicdl 假慢源线程改可中断睡眠，测试进程退出不再挂起约 26 秒（全套约 13 秒）。
- **版本断言测试去硬编码**：`test_version_env` 改为动态读取 `VERSION` 文件比对，升级版本
  不再需要同步修改测试。
- **补齐 musicbox 服务 9 项端点测试**：healthz、播放地址（音质白名单/CLI 参数）、歌曲信息、
  歌手/专辑/歌单、歌词（成功/上游异常）、登录状态与扫码轮询、上游失败 502 与超时 504 信封。

### 已知边界

- tx（QQ音乐）直链解析当前无存活第三方链路（qdy 的 4 条 tx 链路于 2026-09-09 实测全部失效），
  tx 搜索会因探活全部失败而返回空结果但不报错；第三方链路复活后在 `THIRD_PARTY_CHAIN`
  注册表登记即自动恢复。
- kw（酷我）免登录歌词接口已全部失效，歌词暂返回空，不影响播放。
- 第三方链路属社区公益性质，随时可能失效；熔断器保证失效链路自动旁路，最坏情况退化为
  现有官方免登录能力（kg/wy/mg 免费曲），不会出现"搜得到播不出"。

## [1.2.3] - 2026-09-08

### 新增

- **Docker 构建基础镜像源自动探测回退**：fnOS 等系统在 Docker daemon 全局配置的镜像加速器
  （如 `docker.fnnas.com`）异常（401/超时）时，BuildKit 解析 `python:3.13-slim` 元数据失败且不会
  回退官方源，`docker compose up --build` 随即失败（`failed to resolve source metadata ... 401 Unauthorized`）。
  新增 `ensure_base_image.sh`：**国内镜像优先**（完整镜像源引用直连对应仓库，绕开只拦截
  docker.io 短引用的 daemon 加速器，以真实 `docker pull` 验证），逐个尝试
  docker.1ms.run / docker.m.daocloud.io / docker.1panel.live / hub.rat.dev（`FNMUSIC_DOCKER_MIRRORS`
  可覆盖），全部失败再兜底官方 `python:3.13-slim`（daemon 加速器链路在国内网络下常慢/不稳），
  结果缓存到 `.env` 的 `FNMUSIC_BASE_IMAGE` 并由 compose `build.args` 自动读取；install.sh 安装、
  extend.sh 自愈重建、手动 `docker compose up -d --build` 三条路径全部生效，后续运行先验证缓存、
  失效自动重新探测。全程不修改系统 Docker 配置，仅本应用构建生效；也可通过 `BASE_IMAGE` 环境变量
  或直接编辑 `.env` 手动指定。

### 变更

- 三个音源镜像构建内 `pip install` 默认接入清华 PyPI 源（`.env` 的 `FNMUSIC_PIP_INDEX` 可覆盖），
  与宿主机模式安装惯例对齐。
- musicdl 镜像 apt 层默认接入清华镜像（`FNMUSIC_APT_MIRROR` 可覆盖）：仅在构建层内临时替换
  `deb.debian.org`，apt update 失败（15s 超时快速判定）自动回退官方源重试，安装完成后恢复
  官方源——最终镜像与宿主机 apt 配置不受影响；国内网络下 ffmpeg 及其依赖不再长时间卡在
  deb.debian.org 慢速下载。

## [1.2.2] - 2026-09-08

### 修复

- **Docker 安装在受限 umask 环境下启动失败的严重问题**：三个音源镜像此前直接继承仓库检出文件的权限位，
  在 umask 077 环境（root shell、`sudo git clone` 等）下检出的 `app.py` 为 600，进镜像后为
  `root:root 0600`，容器内非 root 的 `appuser` 无法读取，uvicorn 启动即抛
  `PermissionError: [Errno 13] Permission denied: '/app/app.py'` 并随 `restart: unless-stopped` 无限重启。
  现镜像内源码统一 `--chown=appuser:appuser` 且权限 0644，与宿主机文件权限完全解耦（`COPY --chmod`
  仅 BuildKit 支持，故采用兼容新旧构建器的 `--chown` + `RUN chmod` 方案）。
- 修正 musicbox Dockerfile 中 `chown` 早于 `COPY` 执行而对源码文件不生效的问题。

### 变更

- `install.sh` 构建前对服务源码做权限归一化（非致命兜底），避免受限 umask/属主影响构建上下文。
- `docs/INSTALL.md` 新增「常见问题排查」章节，含上述报错的说明与升级方法。

## [1.2.1] - 2026-09-08

### 修复

- 移除生产安装中多余的 pytest 测试依赖。

## [1.2.0] - 2026-09-07

### 新增

- 安装收尾集成网易云终端扫码登录流程（ASCII 二维码过期自动刷新 + 登录状态轮询）。

## [1.1.2] - 2026-09-07

### 新增

- 全音源严格可播过滤与直链探活防线校验升级。

### 优化

- 多音源搜索 3s 首屏与 5s 首响兜底机制，缓存延长至 7 天。

## [1.1.1] - 2026-09-07

### 修复

- 过滤收费不可播歌曲，重构酷狗直链解析。
- 安装/还原流程加固与多音源搜索容错增强。

## [1.1.0] - 2026-09-07

### 新增

- 第三音源：洛雪音乐源（lxmusic，酷狗/网易/咪咕免登录解析）。

## [1.0.1] - 2026-09-07

### 新增

- 项目版本管理与安装配置增量合并机制，音源超时与自适应降级。

### 修复

- 彻底修复网易云 XDG 目录缺失导致子进程崩溃，支持命令行终端直接显示登录二维码。
- 重构验收试播逻辑，支持多音源平等遍历与多关键词重试。
- extend 与端口 5667 解耦，通过 UDS 检查安全判定启用状态。

## [1.0.0] - 2026-09-04

### 新增

- fnmusic-ext 首个发布版本：musicdl / musicbox 双音源，Docker 与宿主机双模式部署，一键安装向导与 fnOS 代理扩展接管。
