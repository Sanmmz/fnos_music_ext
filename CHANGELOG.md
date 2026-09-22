# 更新日志 (Changelog)

本项目所有显著变更均记录于此文件。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.8.3] - 2026-09-22

### 修复

- **移除音源页冗余的「音源总开关」面板（v82 遗留）**：v82 在音源页底部新增的
  「音源总开关」面板，与每个音源详情页已有的「启用该音源」开关功能完全重复
  （同一 env、同一 `toggleSrc`）。用户反馈该面板属于多余重复配置，故删除面板
  及其 `.content{overflow-y:auto}` 配套样式；逐个音源的开关保留，开/关能力不丢。
  设置页顶部的去向说明仍指向「音源」页（现为逐音源的开关）。

## [1.8.2] - 2026-09-22

### 修复

- **设置页点击「展开」跳回顶部（v82）**：`toggleGroup()` 走 `render()` 整块替换
  `innerHTML`，滚动容器被销毁重建导致 `scrollTop` 归零。改为重绘前后存取
  `#content` / `#setWrap` / `#detailCol` / 窗口四处滚动位置并原样写回；
  保存配置触发的重绘同样不再跳顶。
- **手机端歌单行被挤成细缝（v82）**：`.pl-manage-row` / `.pl-src-row` 上
  「拖动把手 + 图标 + 名称描述 + 开关 + 按钮」全挤一行，375px 屏上名称描述只剩约 27px。
  行尾操作区统一包进 `.mact`，窄屏下整体换到第二行右对齐（当前歌单 / 音源歌单 /
  综合搜索三处）；描述允许正常换行并可按任意位置断行。
- **每日推荐描述在手机上显示怪异（v82）**：「每日生成推荐歌单；模式：source-native；
  LLM 兜底：关。」中英混排 + 全角分号，窄行断行位置别扭。改为纯中文短句
  「每日自动生成的推荐歌单，曲目取自各音源榜单。」/「…曲目由 AI 挑选。」，
  内置行的「模式 source-native」同步改为「榜单直取」/「AI 兜底」（`mode` 字段保留兼容）。

### 改进

- **与其他页面重复的设置项合并到对应页面**：原设置页「歌单管理」组的 4 个音源开关，
  与音源页逐个音源的「启用该音源」重复 —— 收进音源页新增的「音源总开关」面板
  （一屏开关全部音源，附当前状态）；`FNMUSIC_SOURCE_ORDER` 输入框与音源页
  `⋮⋮` 拖动排序重复，去掉输入框，拖动即写入。设置页顶部注明去向。
  过滤仅在 UI 侧，后端 schema 与 `/admin/api/config` 读写接口不变。

## [1.8.1] - 2026-09-22

### 修复

- **本地音乐播放失败（严重，v81）**：`forward_to_upstream` / `fetch_upstream_envelope`
  转发上游响应时把 `Content-Length` 一并剔除了。插件接管 socket 后本地曲库的播放请求
  也要经插件转发，于是音频变成无总长度的 `Transfer-Encoding: chunked`——
  播放器拿不到长度就无法确定时长/拖动进度，iOS 与部分安卓播放器会直接判定播放失败。
  实测对照（上游 socket 直连 vs 经插件）：上游 `Content-Length: 11833965`，修复前插件侧为
  **无**。改为只排除 `content-encoding`（纯透传场景下 body 未被改写，长度可信），
  修复后三种 Range 场景的 `Content-Length` / `Accept-Ranges` / `Content-Range` 与上游完全一致，
  整首实收字节 = 声明长度。
- **设置页底部显示不全（v81）**：`.content` 是 `overflow:hidden` 的 flex 列，
  设置页 6 个分组面板超出视口后滚不到底。新增 `.set-wrap` 滚动容器
  （手机端置为 `overflow:visible` 以免嵌套滚动）。
- **界面出现 `U0001F4BE` 乱码（v81）**：Python 的 8 位 Unicode 转义 `\U0001F4BE`
  被原样写进 JS 字符串，而 **JS 只认 4 位的 `\uXXXX`**，于是 `U0001F4BE` 被当成普通文字显示。
  已改为真实字符，`\u2699` / `\u25B2` 等残留转义一并清理。

### 改进

- 设置页分组**可折叠**：默认只展开「下载管理」，其余显示标题 + 项数 + 展开箭头，
  不再一次面对 40 多项；折叠行整行可点击。
- 窄屏下输入框 / 下拉独占一行，控件高度 ≥44px。

## [1.8.0] - 2026-09-22

### 新增

- **管理控制台「设置」页（v80）**：侧边栏（手机端为底部 Tab）新增 ⚙️ **设置** 入口。
  设置页**完全由后端 `/admin/api/config` 的 schema 驱动**，支持 bool / enum / text /
  int / float / MB / secret 全部类型，后续在后端加配置项无需再改前端。
- **四项下载行为开关首次可视化**（此前只能手改 `.env`）：
  - **收藏即下载** `FNMUSIC_FAV_DL_ON_FAVORITE` —— 收藏在线曲目时立即整轨下载到曲库；
  - **取消收藏即删除** `FNMUSIC_FAV_DELETE_ON_UNFAV` —— 取消收藏时删除对应音频 / 歌词 / 引用；
  - **播放即下载** `FNMUSIC_FAV_DL_ON_PLAY` —— 播放**已收藏**曲目时整轨下载；
  - **下载目录** `FNMUSIC_TEE_SAVE_DIR` —— 上述三种下载的统一落盘目录，留空则自动探测
    飞牛共享曲库；改动即时生效，不需要重启。
  - 「边听边存」`FNMUSIC_TEE_SAVE_ENABLED`（任意在线曲目边播边存）与「播放即下载」
    （仅限收藏曲目）是两个不同的开关，均已暴露，可只开其一。
- **下载目录实时状态卡**：新增 `GET /admin/api/settings/storage`（同时挂在 FastAPI 前缀与
  独立端口 8799 下），返回配置值 / **实际生效值** / 是否发生静默回退 / 是否存在 / 是否可写 /
  磁盘余量 / 目录内音频数量，并提供「填入探测到的曲库」「改为自动探测」快捷操作。
- 「下载管理」分组在设置页置顶，并把上述 4 项（含边听边存）排在组内最前；
  标记 `restart` 的配置显示「重启生效」徽标并提供「立即重启」按钮。

### 修复

- **云挂载下磁盘余量显示成 1PB**：曲库挂在 rclone / WebDAV 等网络盘时 `statvfs` 会返回
  不可信的容量（实测 `df` 报 1.0P / 已用 0）。现在超过 512TB 或 `free > total` 一律标记
  `space_unknown`，界面显示「可用空间未知（网络盘 / 云挂载）」而非 1048576 GB。

## [1.7.1] - 2026-09-22

### 修复

- **长篇小说只出 1800 集（v79，严重）**：喜马拉雅专辑曲目接口把翻页**写死成 60 页**，
  而 `getTracksList` 恒为 30 条/页 ⇒ `60 × 30 = 1800` 硬顶。《诡秘之主》实际 2070 集
  （需 69 页），末尾 270 集被静默丢弃。改动：
  - `xmly-service`：`max_pages` 默认 `60 → 0`（0 = 自动），按第 1 页返回的
    `trackTotalCount` 计算页数（多翻 1 页确认结束），第 2 页起**并发抓取**
    （`XMLY_TRACK_CONCURRENCY`，默认 8；单页失败重试 1 次），结果仍按 `pageNum`
    升序拼装保证章节顺序；新增硬顶 `XMLY_TRACK_MAX_PAGES`（默认 200 = 6000 集）。
  - `proxy`：`_xmly_album_rows()` 默认不再传 `max_pages`，只在显式上限时才传；
    搜索封面等场景仍传 `max_pages=1`，不会额外放大请求量。
  - 实测 2070 集：返回 **2070 / 2070**，`xmly-service` **1.11 s**（修复前串行 9.9 s），
    App 侧 `size=-1` **1.56 s / 3.77 MB**；落盘 `track_count` 自愈 1800 → 2070。
  - ⚠️ 已知运维点：`bundle_cache/<pid>.json`（2 h 磁盘缓存，属 root）会把修复前的
    1800 条快照固化，改完需 `sudo` 删除该文件并重启 `fnmusic-ext` 立即生效。

## [1.7.0] - 2026-09-22

本分支（[`Sanmmz/fnos_music_ext`](https://github.com/Sanmmz/fnos_music_ext)）在
上游 v1.6.0 基线上的增强版本。详细对照见 [`DIFFERENCES.md`](DIFFERENCES.md)，
逐版补丁说明见 [`v76-变更说明.md`](v76-变更说明.md)、
[`v77-变更说明.md`](v77-变更说明.md)、[`v78-变更说明.md`](v78-变更说明.md)、
[`v79-变更说明.md`](v79-变更说明.md)。

### 新增

- **喜马拉雅（有声书）音源 `xmly`（v76）**：
  - 新增独立服务 `xmly-service/`（容器端口 `8774`，宿主 `127.0.0.1:8774`），
    已加入 `docker-compose.yml`；
  - 模型：**一个歌单 = 一部小说 = 喜马拉雅的一个专辑**。
    歌单 guid `online:playlist:xmly:<albumId>`，单集 guid `online:xmly:<trackId>:<albumId>`；
  - 触发词劫持：搜索框输入 `小说 吞噬星空` / `喜马拉雅 …` / `xmly …`（大小写均可）
    才调用喜马拉雅，不带触发词时完全走原有音乐链路，互不干扰；
  - 播放地址走 `/mobile-playpage/track/v3/baseInfo` + **AES-128-ECB 解密**得到真实 m4a 直链；
  - **管理页扫码登录**：左侧「音源」→ 🎙️ 喜马拉雅 → 扫码登录（base64 PNG，2 秒轮询
    `pending → scanned → success/expired`）；登录 cookie 落 `xmly-data/`，用于 VIP / 付费专辑播放。
    按设计**暂不同步**订阅、收藏与收听历史；
  - 新增配置：`FNMUSIC_XMLY_ENABLED`、`FNMUSIC_XMLY_URL`。
- **Web 管理后台「综合搜索」（v77）**：
  - 分组标题由「歌单综合搜索」改为「**综合搜索**」，搜索栏**左侧**新增「**歌单 / 小说**」分段控件；
  - `kind=playlist`（默认，向后兼容）：网易云 + 酷我；`kind=novel`：**只查喜马拉雅**，
    返回集数 / 分类 / 简介 / 封面，可直接加入「当前歌单」；
  - 移动端自适应：整条搜索栏允许换行，控件与输入框 `min-height:44px`、字号 16px（防 iOS 自动缩放）。
- **docker-compose 部署方式**：README 新增完整章节——compose 只跑 4 个音源服务，
  核心代理（必须接管 Unix Socket）仍由宿主机 systemd 运行；含 `ensure_base_image.sh`
  镜像源探测、端口与数据卷对照表、日常运维命令。

### 修复

- **小说歌单打开后只有 49/50 集（v78，严重）**：客户端（手机 App / 网页）打开歌单时
  发的是 `page=1&size=-1`，`-1` 的语义是「不分页，一次给全」。旧代码先执行
  `if size < 1: size = 50`，导致后面的 `if size != -1` 成为**永不进入的死分支**，
  上千集的小说永远只吐前 50 集。改为新增 `_page_size()` / `_slice_page()`：
  `size <= 0` 视为「一次给全」并用 `_PAGE_ALL_CAP = 5000` 兜底，
  `page > 1` 且 `size <= 0` 返回空；`size` 缺失/非数字仍为 50，与旧行为一致。
  实测 313 集专辑 `size=-1` 现在返回 **313 / 313**（2.3 s，535 KB）。
- **小说搜索结果没有海报封面（v77）**：v76 把 `coverId` 直接填成了外链图片地址，
  `/static/cover` 解析不出音源 ⇒ 歌单卡片海报整块空白。
  改为 `coverId = 歌单 guid`（`online:playlist:xmly:<albumId>`），
  并在 `/static/cover` 的 `_online_info` **之前**插入 xmly 分支（缓存 → 抓图 → 兜底档位 → 占位 PNG）。
  > ★ 铁律：`coverId` 必须是 `/static/cover` 能解析的 **guid**，绝不能填外链 URL。
- **喜马拉雅海报体积过大（v77b）**：`_sized_cover_url()` 新增 `xmcdn.com` 分支，
  利用图床 `!op_type=3&columns=N&rows=N` 现算缩略图：原图 686 KB → `size=600` 47 KB → `size=300` 15 KB。
  （注意：不能加 `magick=png`，600 档会膨胀到 492 KB；URL 已有 `!...` 必须先剥掉再拼。）

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
