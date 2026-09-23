# fnmusic-ext · 飞牛音乐在线曲库扩展

`fnmusic-ext` 是给 **fnOS（飞牛私有云）自带音乐应用**（`trim.music`）做的**无侵入式增强扩展**。

它不修改官方程序的任何一行代码、不碰官方数据库，只是接管官方前后端之间的那个 Unix Socket，
在中间做一层透明代理，于是官方音乐 App 就多了**全网在线曲库、每日推荐、在线收藏、
在线歌单增删、收藏/播放自动落盘、喜马拉雅有声书**这些原生没有的能力。

网页端、官方手机 App、车机端都是开箱即用 —— **不用装插件，不用改客户端**。

---

## 📌 项目来源

本项目的**最初来源**是 [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext)
（作者 [@javycoder](https://github.com/javycoder)，MIT 协议）。

本仓库（[Sanmmz/fnos_music_ext](https://github.com/Sanmmz/fnos_music_ext)）在它之上做了大量
可用性修复与功能扩展，相关说明见文末「与上游的关系」。

---

## ✨ 功能特点

### 1. 无侵入接管，随时可退

通过 Unix Socket 的 inode 交换接管官方通信入口（详见[实现原理](#-实现原理)）：

- **不改官方代码、不改官方数据库**，官方应用照常升级；
- 容器被 `docker stop` 时**自动把官方 socket 复原**，飞牛音乐立刻回到原生状态；
- 若检测到 socket 已被别的部署接管，会**拒绝接管并保留现场**（fail-closed），不会把音乐搞坏。

### 2. 全网在线聚合搜播

在官方搜索框直接输入歌名 / 歌手，**即点即播**。内置 4 个音源容器，可自由开关：

| 音源 | 端口 | 覆盖 |
| :--- | :--- | :--- |
| `musicbox` | `8770` | 网易云高品质解析，支持扫码登录（VIP / 收藏） |
| `musicdl` | `8768` | 酷我 / 咪咕 等平台聚合 |
| `lxmusic` | `8772` | 洛雪风格免登录解析：酷狗 / 网易 / 咪咕 / 酷我，多链路自动回退 |
| `xmly` | `8774` | 喜马拉雅有声书 / 小说专辑 |

多音源**并发调度**，按 `(标题, 歌手)` 去重合并；哪个音源挂了就单独隔离，其余照常出结果。
接口还会返回 `warnings[]`，管理台用橙色提示告诉你**哪个音源超时了**，不再「静默只剩一个源」。

### 3. 🎙️ 喜马拉雅有声书（一个歌单 = 一部小说）

在搜索框输入触发词即切换链路：

```
小说 吞噬星空
```

触发词三个：`小说` / `喜马拉雅` / `xmly`（大小写均可），后面留空格再写书名。

- **歌曲**维度：每行 = 一部小说，点开直接播第 1 集；
- **歌单**维度：每行 = 一部小说 = 一个歌单，点开就是完整章节列表；
- **不带触发词时完全走常规音乐链路**，与普通搜索互不干扰；
- VIP / 付费专辑在管理台**扫码登录**后完整收听，未登录也能搜、能听免费章节。

### 4. 每日推荐歌单

启动后在歌单列表最上方注入「每日推荐」，默认采信**音源原生推荐**：

- 网易云每日推荐 / 官方榜单；
- 洛雪免登录榜单（酷我多榜等）。

网易音源未启用时，可选接入任意 OpenAI 兼容大模型做兜底推荐（`FNMUSIC_LLM_*`）。

### 5. 在线收藏 & 播放历史（多用户隔离）

在线歌曲点「红心」即进收藏，**按当前登录用户 GUID 独立记账**，家庭成员互不干扰；
收藏与播放历史与官方本地曲库**融合展示**在一个列表里。

> 开关：`FNMUSIC_ONLINE_HISTORY_MODE`（`full` = 在线曲目可进收藏 / 历史；`off` = 只对本地曲目生效）。
> 「收藏即下载、播放即下载、取消收藏即删除」都依赖它。

### 6. 在线歌单也能增删单曲

官方飞牛只认自己曲库里的曲目，`online:` 前缀的曲目在官方歌单表里**没有对应记录**，
所以原生操作会静默失败（返回 200 但什么都没发生）。本项目补上了这两半：

| 场景 | 行为 |
| :--- | :--- |
| 在**在线歌单**（每日推荐 / 我的歌单 / 喜马拉雅专辑）里移除单曲 | 本地记账（`online_favorites/playlist_removed.json`），三处列表同步扣数 |
| 把**在线曲目**加入**自建歌单** | 本地附加表托管（`online_favorites/playlist_extra.json`），读时合并回去 |
| 从自建歌单里移除在线曲目 | 从附加表摘除 |
| 官方歌单里加**本地曲目** | 完全走原路径，行为一字未改 |

歌单详情、曲目列表、歌单列表三处的 `trackCount` 都会跟着变，**点进去看到的和列表上的数字一致**。

### 7. 下载与落盘

「下载管理」收敛为**两个正交维度**，不再是一堆互相打架的开关：

| 维度 | 环境变量 | 取值 |
| :--- | :--- | :--- |
| **下载范围** | `FNMUSIC_DOWNLOAD_SCOPE` | `off` 不自动下载 / `favorites` 只下收藏（推荐）/ `all` 播过的全下 |
| **下载时机** | `FNMUSIC_DOWNLOAD_TRIGGER` | `favorite` 收藏那刻 / `favorite_play` 收藏时 + 播放补漏（推荐）/ `play` 只要播放就下 |

配套还有：

- **边听边存**：在线听歌时后台流式落盘，下次直接走本地，秒开且省流量；
- **取消收藏即删除**：`FNMUSIC_FAV_DELETE_ON_UNFAV`，保持曲库干净；
- **歌词贴身**：`FNMUSIC_LYRIC_PROMOTE`，歌词始终跟着音频落到曲库同名 sidecar；
- **孤儿歌词清理**：`FNMUSIC_LYRIC_ORPHAN_GC`，定期回收缓存里已无对应音频的歌词；
- 下载并发 / 超时 / 单曲大小上限都可配。

> 落盘目录 `FNMUSIC_TEE_SAVE_DIR` 留空 = 自动探测飞牛共享曲库。
> 曲库在 **rclone 云盘（fuse 挂载）** 上也能写，实测约 1.5 MB/s；嫌慢可以改指本地盘
> （compose 已预挂 `./downloads`），再把该目录加进飞牛音乐库即可。

### 8. 搜索快、排序好、封面不 404

- **双层 TTL 缓存**：音源服务侧 + 代理聚合侧各一层，反复搜同一个词从秒级掉到**毫秒级**；
  只缓存**成功且有结果**的响应，降级 / 空结果不缓存（避免一次抖动被缓存住）；
- **排序策略** `FNMUSIC_SEARCH_RANK`：把「有海报 + 高音质 / 无损」的条目顶到第一屏；
- **封面恒 200**：`static/cover` 四级兜底（在线元信息 → 元数据缓存 → 音源详情 → 内置占位图），
  **绝不返回 404、绝不返回 JSON** —— 上游返回 404 会让手机 App 整列变空白；
- **歌词与高清封面自动补全**：在线歌曲的动态滚动 LRC + 高清专辑图。

### 9. Web 管理后台

`http://<NAS_IP>:8799/admin`（端口可用 `FNMUSIC_ADMIN_PORT` 改）

- **音源**：各音源开关、健康状态、**网易云 / 喜马拉雅扫码登录**；
- **歌单**：**综合搜索**（可切「歌单 / 小说」维度），结果可一键加入当前歌单；
- **配置**：直接改 `.env` 里的项，能热生效的立即生效，需重启的页面会提示；
- **诊断**：容器内 `/app/access_probe.log` 逐条记录请求的
  `方法 路径 | 状态码 | 耗时 | Content-Type | len`，
  手机 App 的请求会带 `?lan=zh-CN`，可据此把手机端 / 网页端 / 脚本请求区分开。

### 10. 自动扫库与容灾

- **自动扫库**：曲库在 rclone 云盘上时 fuse 不产生 inotify 事件，
  代理会在下载 / 删除后主动触发官方扫描，新歌立刻可见（`FNMUSIC_AUTO_SCAN`）；
- **容灾降级**：任一音源异常即隔离；代理进程异常退出自动切回官方直连。

---

## 🚀 部署：只用 docker compose

整个项目**只使用 docker compose 部署**，一条命令拉起 5 个容器，**不需要 systemd**：

```
┌──────────────────────────────────────────────────────────────┐
│ docker compose up -d --build                                 │
│                                                              │
│   fnmusic-musicbox   8770  →  网易云                          │
│   fnmusic-musicdl    8768  →  酷我 / 咪咕                     │
│   fnmusic-lxmusic    8772  →  洛雪免登录解析                   │
│   fnmusic-xmly       8774  →  喜马拉雅有声书                   │
│   fnmusic-proxy        ——   →  核心代理（接管官方 socket）      │
│   （管理后台 8799 由 fnmusic-proxy 提供）                       │
└──────────────────────────────────────────────────────────────┘
```

核心代理容器以 `pid: host` + `network_mode: host` + 挂载 `/var/run`（rw）运行，
复用宿主机 PID 与 socket 命名空间，所以 fail-closed 接管机制**与宿主机直接跑完全一致**。

> 仓库里的 `install.sh` / `extend.sh` / `restore.sh` / `fnmusic-ext.service` 是上游遗留的
> 应急回退脚本（宿主机 systemd 模式），**正常部署不需要执行**。

### 前置准备

1. 已在 fnOS「应用中心」安装并启动官方 **【飞牛音乐】**（已适配 2026-09-11 升级后的新版官方应用）；
2. 宿主机已装好 **Docker**（fnOS 应用中心一键安装）与 `git`；
3. `cd` 到一个可写目录作为部署根（下文以 `~/docker/music` 为例）。

### 步骤 1 · 获取代码

```bash
cd ~/docker/music
git clone https://github.com/Sanmmz/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext
```

### 步骤 2 · 准备 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

`.env` 里只有几个键会影响**镜像构建**，国内网络建议先确认：

| 键 | 默认 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_BASE_IMAGE` | *(空)* | 基础镜像引用。留空时由 `ensure_base_image.sh` 探测可用镜像并写回 |
| `FNMUSIC_PIP_INDEX` | 清华 | 容器内 pip 源（`mirrors.aliyun.com/pypi/simple` 常用备选） |
| `FNMUSIC_APT_MIRROR` | 清华 | 仅 musicdl 镜像构建层的 apt 源，失败自动回退官方源 |

**强烈建议先跑一次基础镜像探测**（能绕开 fnOS daemon 加速器异常导致的 `401 Unauthorized`）：

```bash
./ensure_base_image.sh
# 探测结果会写回 .env 的 FNMUSIC_BASE_IMAGE，compose 通过 build.args 自动读取
```

### 步骤 3 · 构建并启动

```bash
sudo docker compose up -d --build
```

首次构建几分钟（5 个镜像都要拉基础镜像 + 装依赖）。完成后：

```bash
sudo docker compose ps
```

预期 5 个容器全部 `Up (healthy)`：

```
NAME                STATUS              PORTS
fnmusic-musicbox    Up (healthy)        0.0.0.0:8770->8000/tcp
fnmusic-musicdl     Up (healthy)        127.0.0.1:8768->8000/tcp
fnmusic-lxmusic     Up (healthy)        127.0.0.1:8772->8000/tcp
fnmusic-xmly        Up (healthy)        127.0.0.1:8774->8000/tcp
fnmusic-proxy       Up (healthy)
```

逐个探活音源：

```bash
for p in 8770 8768 8772 8774; do
  printf "%s -> " "$p"; curl -s -m 5 "http://127.0.0.1:${p}/healthz"; echo
done
```

### 步骤 4 · 确认接管成功

```bash
docker ps --filter name=fnmusic-proxy
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
```

预期：容器 `Up (healthy)`，健康检查返回 `{"ok":true,...}`。
此时打开飞牛音乐 App，搜索一个歌星名字，能看到在线结果即成功。

> **接管不上怎么办**：先看 `sudo docker compose logs --tail 100 proxy`。
> 若提示 socket 已被其他部署接管，请到原部署目录 `docker compose down`，
> **不要手工删除 socket**。

### 步骤 5 · 扫码登录（可选，但不登录就只有免费内容）

打开 `http://<NAS_IP>:8799/admin` → 左侧「音源」：

- **网易云**：扫码登录后可听 VIP 音质、拿到你自己的每日推荐；
- **喜马拉雅**：扫码登录后可听 VIP / 付费专辑。

登录态分别落在 `musicbox-data/` 与 `xmly-data/`（已挂进容器与代理，重启不丢）。

### 日常运维

```bash
cd ~/docker/music/fnmusic_ext

sudo docker compose ps                      # 看状态
sudo docker compose logs -f proxy           # 看核心代理日志（排障第一入口）
sudo docker compose logs -f xmly            # 看某个音源
sudo docker compose restart musicbox        # 重启单个音源
sudo docker compose up -d --build           # 改了代码后重建全部
sudo docker compose up -d --build xmly      # 只重建喜马拉雅
sudo docker compose down                    # 停并删容器（数据卷保留）
```

### 端口与数据卷

| 服务 | 宿主端口 | 绑定 | 数据卷 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `musicbox` | `8770` | `0.0.0.0` | `./musicbox-data` | 绑 `0.0.0.0` 是因为扫码要用手机访问二维码 |
| `musicdl` | `8768` | `127.0.0.1` | — | |
| `lxmusic` | `8772` | `127.0.0.1` | — | |
| `xmly` | `8774` | `127.0.0.1` | `./xmly-data` | 登录 cookie 落这里 |
| `proxy` | — | — | `./cache` `./online_favorites` `./play_history` `./recommend_cache` `./downloads` `./.runtime` `./.env` | 接管官方 socket，**不占 TCP 端口** |
| **管理后台** | `8799` | `0.0.0.0` | — | 由 `fnmusic-proxy` 提供 |

核心代理还额外挂了两个**必须存在**的宿主路径：

| 宿主路径 | 挂载点 | 为什么必须 |
| :--- | :--- | :--- |
| `/var/run` | `/var/run:rw` | 接管官方 socket 需要原子 rename，必须可写 |
| `/usr/local/apps/@appdata/trim.music/db/music.db` | 同名 `:ro` | 读官方曲库（只读，安全） |
| 曲库所在存储池（如 `/vol02`） | 同名 **`:rw`** | 「收藏下载 / 播放下载 / 边听边存」的音频要落到飞牛共享曲库。**挂成 `:ro` 会直接 Read-only file system，表现就是「开了下载却一首都没下」** |

> 曲库若在 `/vol01`、`/vol03` 等别的存储池，照样子在 `docker-compose.yml` 里加一行即可。
> 注意**别用 `dd` 验证能否写入** —— `dd` 在 rclone 的 fuse 挂载上会返回 0 字节，是工具假象；
> 用 `python` 分块写或直接 `cp` 一个真实文件验证。

### 只想要部分音源？

注释掉 `docker-compose.yml` 里不需要的 service 块，并把 `.env` 里对应的
`FNMUSIC_*_ENABLED` 设为 `false`，避免代理去连一个不存在的端口。

---

## 🎧 怎么用

| 想做什么 | 怎么做 |
| :--- | :--- |
| 搜在线音乐 | 官方搜索框直接输入歌名 / 歌手 |
| 听小说 / 有声书 | 搜索框输入 `小说 三体`（触发词 `小说` / `喜马拉雅` / `xmly`） |
| 收藏在线歌 | 点播放页的「红心」 |
| 把在线歌加进自建歌单 | 在官方界面「添加到歌单」，即点即生效 |
| 从在线歌单移除单曲 | 官方界面点「移除」，刷新不会再回来 |
| 自动下载 | 管理台「下载管理」设好下载范围 + 下载时机 |
| 扫码登录 | 管理台「音源」页，网易云 / 喜马拉雅各一个入口 |

---

## ⚙️ 环境变量

根目录 `.env` 从 `.env.example` 复制后自行维护（`chmod 600`，**绝不提交**）。
管理台「配置」页改的就是这个文件。主要项：

### 音源

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_NETEASE_ENABLED` | `true` | 网易云音源（musicbox）+ 每日推荐 |
| `FNMUSIC_MUSICBOX_URL` | `http://127.0.0.1:8770` | 网易云服务地址 |
| `FNMUSIC_MUSICDL_ENABLED` | `true` | 聚合音源（musicdl） |
| `FNMUSIC_MUSICDL_URL` | `http://127.0.0.1:8768` | 聚合音源服务地址 |
| `FNMUSIC_ONLINE_SOURCES` | `MiguMusicClient,KuwoMusicClient` | musicdl 启用的子平台 |
| `FNMUSIC_LX_ENABLED` | `true` | 洛雪音源（lxmusic） |
| `FNMUSIC_LX_URL` | `http://127.0.0.1:8772` | 洛雪服务地址 |
| `LX_SOURCES` | `kg,wy,mg,kw` | 洛雪子源列表 |
| `LX_THIRD_PARTY` | `1` | 洛雪第三方解析总开关（关掉退化为官方免登录直连） |
| `FNMUSIC_XMLY_ENABLED` | `true` | 喜马拉雅有声书音源 |
| `FNMUSIC_XMLY_URL` | `http://127.0.0.1:8774` | 喜马拉雅服务地址 |
| `FNMUSIC_ADMIN_PORT` | `8799` | Web 管理后台端口 |
| `FNMUSIC_SOURCE_ORDER` | `netease,lx,musicdl` | 音源结果排序权重 |

### 收藏 / 历史 / 下载

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_ONLINE_HISTORY_MODE` | `off` | `full`=在线曲目可进收藏与播放历史（收藏即下载等依赖它）；`off`=不写入 |
| `FNMUSIC_DOWNLOAD_SCOPE` | `favorites` | 下载范围：`off` / `favorites` / `all` |
| `FNMUSIC_DOWNLOAD_TRIGGER` | `favorite_play` | 下载时机：`favorite` / `favorite_play` / `play` |
| `FNMUSIC_TEE_SAVE_DIR` | *(空)* | 落盘目录；留空=自动探测飞牛共享曲库 |
| `FNMUSIC_TEE_CACHE_MAX` | `2` | 关闭边听边存时滚动保留的最新试听缓存条数 |
| `FNMUSIC_FAV_DELETE_ON_UNFAV` | `true` | 取消收藏即删除对应音频 / 歌词 / 引用 |
| `FNMUSIC_FAV_DL_CONCURRENCY` | `2` | 同时下载的曲目数 |
| `FNMUSIC_FAV_DL_TIMEOUT_S` | `300` | 单首下载超时（秒） |
| `FNMUSIC_FAV_DL_MAX_BYTES` | `0` | 单首大小上限（0=不限） |
| `FNMUSIC_FAV_DIR` | `$FNMUSIC_HOME/online_favorites` | 在线收藏元数据目录 |

### 搜索与播放

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_SEARCH_TIMEOUT` | `15` | 多音源并发搜索等待预算（秒） |
| `FNMUSIC_SEARCH_CACHE_TTL` | `600` | 综合搜索结果缓存 TTL（秒），只缓存成功且有结果的响应 |
| `FNMUSIC_SEARCH_CACHE_MAX` | `200` | 缓存条目上限 |
| `FNMUSIC_SEARCH_RANK` | `cover_quality` | `cover_quality` 海报优先 / `quality_cover` 音质优先 / `off` 关闭 |
| `FNMUSIC_SEARCH_RANK_WAIT_S` | `1.5` | 首屏等「补全 + 重排」落定的上限（秒） |
| `FNMUSIC_SEARCH_ENRICH` | `true` | 是否补全搜索结果的海报与音质档 |
| `FNMUSIC_SEARCH_ENRICH_LIMIT` | `30` | 只补第一屏条数（控制首屏等待） |
| `FNMUSIC_NETEASE_DIRECT` | `true` | 网易云取流走直连快通道（需挂 `musicbox-data` 拿 cookie） |
| `FNMUSIC_NETEASE_QUALITY` | `lossless` | 网易云音质档（`lossless` / `exhigh` / `higher` / `standard`） |
| `FNMUSIC_STREAM_URL_TTL` | `600` | 取流直链缓存时长（秒） |

### 库维护与构建

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_AUTO_SCAN` | `true` | 下载 / 删除后主动触发官方扫库（云盘曲库必需，fuse 不发 inotify） |
| `FNMUSIC_LIBRARY_DIR` | *(空)* | 曲库根目录，留空自动探测 |
| `FNMUSIC_LYRIC_PROMOTE` | `true` | 歌词贴身落盘到曲库同名 sidecar |
| `FNMUSIC_LYRIC_ORPHAN_GC` | `true` | 定时清理孤儿歌词 |
| `FNMUSIC_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | *(空)* | 每日推荐的大模型兜底（仅网易音源未启用时生效） |
| `FNMUSIC_BASE_IMAGE` | *(空)* | Docker 基础镜像引用，留空自动探测 |
| `FNMUSIC_PIP_INDEX` | 清华 | 容器内 pip 源 |
| `FNMUSIC_APT_MIRROR` | 清华 | musicdl 构建层 apt 源 |
| `FNMUSIC_PROXY_MODE` | `host` | 仅影响上游遗留脚本（`extend.sh` / `restore.sh`）的模式判定；docker compose 部署下代理始终是容器 |
| `FNMUSIC_VERSION` | 见 `VERSION` | 当前版本号 |

完整清单与逐项注释见 [`.env.example`](.env.example)。

---

## 📁 目录结构

```
fnmusic_ext/
├── docker-compose.yml                  # 部署入口：4 个音源 + 核心代理
├── ensure_base_image.sh                # 基础镜像源自动探测
├── .env.example                        # 环境变量模板
├── proxy/                              # 核心代理
│   ├── app.py                          #   主程序（接管、聚合搜索、收藏、落盘、歌单增删）
│   ├── admin_ui.html                   #   管理后台前端
│   ├── takeover.py                     #   socket 接管与归属判定（fail-closed）
│   ├── Dockerfile  entrypoint.sh        #   容器化构建与启动
│   └── tests/                          #   单元测试
├── musicbox-service/                   # 网易云音源         -> 8770
├── musicdl-service/                    # 酷我 / 咪咕音源     -> 8768
├── lxmusic-service/                    # 洛雪音源           -> 8772
├── xmly-service/                       # 喜马拉雅音源       -> 8774
├── musicbox-data/  xmly-data/          # 登录态数据卷（不入库）
├── cache/  online_favorites/  play_history/  recommend_cache/  downloads/
├── install.sh  extend.sh  restore.sh   # 上游遗留脚本（仅宿主机模式应急回退）
├── CHANGELOG.md                        # 更新日志
├── vNN-变更说明.md                      # 各版本详细变更说明
├── DIFFERENCES.md                      # 与上游的逐项差异
└── patches/  reports/                  # 补丁脚本与验收报告
```

---

## 🔧 实现原理

### 1. Unix Socket 接管

飞牛官方架构里，前端 Nginx 通过本地 Unix Domain Socket（`/var/run/trim_music.socket`）
与官方 Go 后端通信。本项目利用 inode 机制无缝插入：

1. 把官方 socket 重命名为 `trim_music_upstream.socket`；
2. 代理在原路径 `/var/run/trim_music.socket` 建立同名监听，权限一致；
3. Nginx 与客户端完全无感知。

```text
[飞牛音乐客户端 (Web / App / 车机)]
          │
          ▼
    [飞牛 Nginx]
          │  Unix Socket
          ▼
┌─────────────────────────────────────────────────────────────┐
│  fnmusic-ext 代理 (/var/run/trim_music.socket)               │
│  ├─ 本地接口透传 ──► 官方后端 (trim_music_upstream.socket)     │
│  ├─ 在线搜索聚合 ──► 并发调度 musicbox / musicdl / lx / xmly   │
│  ├─ 在线曲目取流 ──► 解析直链，206 分片下发 + 后台 Tee 落盘      │
│  ├─ 收藏 / 历史  ──► online_favorites/（多用户隔离，合并展示）    │
│  └─ 每日推荐     ──► 注入音源原生推荐 / 榜单（或 LLM 兜底）       │
└─────────────────────────────────────────────────────────────┘
```

### 2. 主要拦截点

| 接口 | 做什么 |
| :--- | :--- |
| `/music/api/v1/search/track` | 透传本地结果 + 并发聚合在线音源，去重合并、渐进式返回；带喜马拉雅触发词时整条链路切到 xmly |
| `/music/api/v1/track/stream` | 拦截 `online:` guid，解析真实直链后返回 `206 Partial Content`，后台 Tee 异步落盘 |
| `/music/api/v1/favorite/track` | 按当前用户 GUID 独立记账在 `online_favorites/`，读取时与官方本地收藏合并 |
| `/music/api/v1/playlist/add-track` | 在线曲目 → 本地附加表；本地曲目 → 转发上游 |
| `/music/api/v1/playlist/remove-track` | 在线歌单 → 本地记账并过滤；本地曲目 → 转发上游 |
| `/music/api/v1/playlist/detail` · `track/playlist-detail/list` · `playlist/list` | 合并附加曲目、过滤已移除曲目、同步 `trackCount` |
| `static/cover` | 恒返回 `200` + 真实图片字节（四级兜底 + 占位图），绝不 404、绝不返回 JSON |

### 3. 容灾

- 任一音源异常即隔离，只返回可用数据，接口带 `warnings[]` 提示；
- 代理进程异常退出时自动切回官方直连，音乐不会失联；
- 接管失败（socket 归属他人）时**拒绝接管并保留现场**。

---

## 与上游的关系

本仓库基于 [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) 持续维护，
**纯增量补丁**：上游原有函数与路由一个都没删，完全向后兼容。

主要方向是「把在线曲库修到手机 App 上真的能用」，包括但不限于：
手机端收藏 / 历史整列空白、点播放 404、列表加载超时、封面 404 拖垮整列、
在线歌单增删单曲、下载开关语义重叠、云盘曲库自动扫库、歌词归属、
搜索加速与结果排序、喜马拉雅有声书音源接入、核心代理容器化。
逐项对照见 [`DIFFERENCES.md`](DIFFERENCES.md)，每个版本的详细说明见 `vNN-变更说明.md`。

---

## ⚠️ 免责与版权声明

### 1. 技术研究与非商业用途

本项目基于 **MIT 许可证**开源，立项初衷是个人开发者对 Linux Unix Domain Socket 机制、
透明反向代理、流式媒体传输与多协程并发架构的学习与技术验证。
**严格限定于个人技术研究与学习交流**，严禁任何形式的商业营利、付费订阅或软硬件捆绑销售。

### 2. 致谢上游开源项目

在线音源检索与元数据抓取依赖社区优秀的开源组件：

- [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext)（本项目的来源）
- [CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl)
- [darknessomi/musicbox](https://github.com/darknessomi/musicbox)
- 洛雪音乐（LX Music）社区音源的**多链路回退思路**（`lxmusic-service` 为独立 Python 实现，
  不包含其脚本代码）

在此向上游项目的原作者与贡献者致以敬意。

### 3. 版权归属

- **音频与元数据版权全权归属各原始版权方**（唱片公司、独立音乐人、各在线音乐平台等）。
- **零托管、零存储**：本仓库**不托管、不分发、不存储任何受版权保护的音频、视频、歌词或封面文件**。
  所有音频流与图文元数据均由客户端请求时代理实时转发自公开网络接口或源站 CDN。
- **登录凭据仅在本地**：网易云 / 喜马拉雅的登录 cookie 只写入本机 `.env` 与数据卷目录
  （`musicbox-data/` / `xmly-data/`，均被 `.gitignore` 排除），**不上传、不进日志**。
  扫码登录仅用于获取你本人账号已有的收听权限。
- **本地缓存合规**：落盘产生的音频文件仅供个人离线技术分析与标签兼容性测试，
  **请在 24 小时内自行删除**。
- **倡导正版**：请支持正版音乐。长期收听请前往网易云音乐、酷我音乐、咪咕音乐、喜马拉雅等
  官方平台开通会员。

### 4. 风险自担

使用者应自行遵守所在国家 / 地区的法律法规及第三方平台的用户协议。
因滥用、恶意传播、商业化使用或不当配置导致的一切法律责任、版权纠纷、账号封禁、
IP 拦截或经济损失，**概由使用者本人承担**，本项目发起人、维护者及贡献者不承担任何责任。

**权利人联系通道**：若权利人认为本项目涉嫌侵犯其合法权益，请通过 GitHub Issue 提交权属证明，
我们将在核实后第一时间配合下架、修改或删除相关代码与功能。
