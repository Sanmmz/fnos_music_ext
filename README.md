# fnmusic-ext 飞牛音乐扩展代理

`fnmusic-ext` 是专为 fnOS（飞牛私有云）自带音乐应用（`trim.music`）量身定制的**无侵入式增强扩展**。
通过接管系统后端通信入口，在**完全不修改官方程序与数据库**的前提下，让原生飞牛音乐秒变全能音乐播放器。

> 本仓库是 [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) 的**增强分支**，基线为上游 **v1.6.0**。
> 一句话概括差异：**上游把「在线曲库」接通了，本分支把它修到「手机 App 上真的能用」**，
> 并额外接入了**喜马拉雅有声书音源**。完整对照见 [`DIFFERENCES.md`](DIFFERENCES.md)。

---

## 🎵 核心功能

- **全网在线聚合搜播**：在官方搜索框直接输入歌名/歌手，聚合搜索网易云、酷狗、酷我、咪咕等平台曲库，即点即播；
- **🎙️ 喜马拉雅有声书（本分支新增）**：一个歌单 = 一部小说 = 喜马拉雅的一个专辑。
  在搜索框输入 `小说 三体` 即触发；支持管理页**扫码登录**获取 VIP 播放权限；
  VIP / 付费专辑登录后可完整收听，未登录也能搜能听免费章节；
- **精准歌词与高清封面**：自动补齐在线歌曲的动态滚动 LRC 歌词与高清专辑封面；
  **封面接口恒不返回 404 / JSON**（上游返回 404 会导致手机 App 整列消失），并有占位图兜底；
- **智能边播边存（无感离线）**：在线听歌时后台自动缓存音频，再次播放直接走本地，省流量且秒开；
- **全平台原生无感适配**：飞牛网页端、官方手机 App、车载端开箱即用，无需安装任何第三方插件；
- **多用户隔离收藏**：家庭成员各自点「红心」收藏在线歌曲，数据独立隔离，与本地曲库融合展示；
- **音源原生每日推荐**：默认采信音源原生推荐（网易每日推荐/榜单 + 洛雪免登录榜单）；
  网易未启用时可选接入大模型兜底；
- **搜索结果「有海报 + 高音质」优先排序**：把带封面且高码率/无损的条目顶到第一屏
  （`FNMUSIC_SEARCH_RANK`，可 `off` 完全回退）；
- **Web 管理后台**：`http://<NAS_IP>:8799/admin`，可改配置、管歌单、**综合搜索（歌单 / 小说）**、
  网易云与喜马拉雅扫码登录；
- **多音源自由组合**（可多选，至少启用一个）：

  | 音源 | 端口 | 说明 |
  | :--- | :--- | :--- |
  | [musicbox](https://github.com/darknessomi/musicbox) | `8770` | 网易云高品质解析，支持扫码登录 VIP/收藏 |
  | [musicdl](https://github.com/CharlesPikachu/musicdl) | `8768` | 酷我 / 咪咕等平台聚合 |
  | lxmusic | `8772` | 洛雪风格免登录解析：默认 kg / wy / mg / kw |
  | **xmly（喜马拉雅）** | `8774` | 本分支新增：有声书 / 小说专辑 |

  > 解析结果会进行有限媒体探活；这不能保证完整歌曲、账户权限或直链后续始终可用，
  > 请仅访问您有权收听的内容。

---

## 🚀 两种部署方式，任选其一

| | **方式一：一键脚本**（推荐新手） | **方式二：docker-compose**（推荐想自己掌控容器的人） |
| :--- | :--- | :--- |
| 音源服务 | `install.sh` 自动 `docker run` 创建 | `docker compose up -d --build` |
| 核心代理 | 宿主机 systemd（`fnmusic-ext`） | **同样是宿主机 systemd** |
| 适合 | 一路回车装完 | 想改端口、改镜像源、单独重建某个音源、把栈纳入自己的 compose 体系 |
| 章节 | [方式一](#方式一一键脚本安装) | [方式二](#方式二docker-compose-部署) |

> ⚠️ **核心代理（fnmusic-ext）无论哪种方式都以宿主机 systemd 运行**，
> 因为它必须接管 `/var/run/trim_music.socket`——这个 Unix Socket 在容器里是够不到的。
> docker-compose 只负责跑**音源服务**。

---

## 前置准备（两种方式都需要）

1. 已在 fnOS「应用中心」安装并启动官方 **【飞牛音乐】** 应用
   （v1.4.0 起已适配 2026-09-11 升级后的新版官方应用，旧版同样兼容；
   官方应用升级后若扩展未生效，重新执行 `./extend.sh` 即可恢复）；
2. 宿主机已安装基础依赖：
   ```bash
   sudo apt-get update && sudo apt-get install -y python3 python3-venv git
   ```
3. 若使用容器（两种方式都涉及容器），需在 fnOS 应用中心安装 **Docker**。

---

## 方式一：一键脚本安装

```bash
# 1. 克隆仓库
git clone https://github.com/Sanmmz/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext

# 2. 赋予脚本执行权限
chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh ensure_base_image.sh

# 3. 运行交互式向导
./install.sh
```

向导会引导选择：

- **安装模式**：`docker`（音源容器化，推荐）/ `host`（宿主机 venv，免装 Docker）；
- **音源选择**：可多选，如 `1,2,3` 全开或 `1,3` 自由组合
  （`1 = musicbox` / `2 = musicdl` / `3 = lxmusic`）；
- **每日推荐**：默认使用音源原生推荐，无需配置。

> 💡 一行静默安装：
> ```bash
> ./install.sh --non-interactive --mode docker --sources=1,2,3 --extend
> ```

若向导中未自动启用，随时手动接管：

```bash
./extend.sh        # 一键接管并启用（含全链路自动化验收测试）
```

---

## 方式二：docker-compose 部署

### 2.1 这个方式做了什么

```
┌──────────────────────────────────────────────────────────────┐
│ docker compose（音源服务栈，4 个容器）                        │
│   fnmusic-musicbox  8770  → 网易云                            │
│   fnmusic-musicdl   8768  → 酷我 / 咪咕                       │
│   fnmusic-lxmusic   8772  → 洛雪免登录解析                     │
│   fnmusic-xmly      8774  → 喜马拉雅有声书（本分支新增）        │
└──────────────────────────────────────────────────────────────┘
                          ▲ HTTP（127.0.0.1）
                          │
┌──────────────────────────────────────────────────────────────┐
│ fnmusic-ext 核心代理（宿主机 systemd）                        │
│   接管 /var/run/trim_music.socket，转发到上面 4 个音源         │
│   管理后台：0.0.0.0:8799                                      │
└──────────────────────────────────────────────────────────────┘
```

### 2.2 步骤 1：获取代码

```bash
git clone https://github.com/Sanmmz/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext
chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh ensure_base_image.sh
```

### 2.3 步骤 2：准备 `.env`

```bash
cp .env.example .env
chmod 600 .env
```

`.env` 里这些键会影响 compose 构建，**国内网络建议先确认**：

| 键 | 默认 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_BASE_IMAGE` | *(空)* | 基础镜像引用。留空时 `ensure_base_image.sh` 自动探测可用镜像并写回此键。**若 `docker pull python:3.13-slim` 报 401/超时，务必先跑一次探测** |
| `FNMUSIC_PIP_INDEX` | 清华 | 容器内 pip 源。`mirrors.aliyun.com/pypi/simple` 是常用备选 |
| `FNMUSIC_APT_MIRROR` | 清华 | 仅 musicdl 镜像构建层的 apt 源，失败自动回退官方源 |

自动探测基础镜像（**强烈建议先跑**，能绕开 fnOS daemon 加速器异常导致的 `401 Unauthorized`）：

```bash
./ensure_base_image.sh
# 结果会写回 .env 的 FNMUSIC_BASE_IMAGE，compose 通过 build.args 自动读取
```

### 2.4 步骤 3：构建并启动音源容器栈

```bash
sudo docker compose up -d --build
```

首次构建约需几分钟（4 个镜像都要拉基础镜像 + pip 装包）。完成后：

```bash
sudo docker compose ps
```

预期输出（4 个 `Up (healthy)`）：

```
NAME                STATUS              PORTS
fnmusic-musicbox    Up (healthy)        0.0.0.0:8770->8000/tcp
fnmusic-musicdl     Up (healthy)        127.0.0.1:8768->8000/tcp
fnmusic-lxmusic     Up (healthy)        127.0.0.1:8772->8000/tcp
fnmusic-xmly        Up (healthy)        127.0.0.1:8774->8000/tcp
```

逐个验证健康接口：

```bash
for p in 8770 8768 8772 8774; do
  printf "%s -> " "$p"
  curl -s -m 5 "http://127.0.0.1:${p}/healthz" || echo "FAIL"
  echo
done
```

### 2.5 步骤 4：安装并接管核心代理

```bash
sudo ./install.sh --non-interactive --mode docker --sources=1,2,3 --extend
```

- install.sh 会**复用** compose 已创建的同名容器（靠 `com.docker.compose.project.working_dir`
  标签判定归属，**不会** `rm -f` 掉它们），然后只把核心代理装成宿主机 systemd 服务并完成接管。
- 如果它报「容器不属于当前目录」，说明你之前在**别的目录**起过同名容器，
  请先到那个目录 `docker compose down`，或换个目录重新 clone。

验证接管：

```bash
sudo systemctl is-active fnmusic-ext
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
```

### 2.6 步骤 5：喜马拉雅扫码登录（可选）

打开 `http://<NAS_IP>:8799/admin` → 左侧「音源」→ 🎙️ **喜马拉雅（有声书）** → **扫码登录**，
用喜马拉雅 App 扫码。登录态写在 `xmly-data/xmly_cookie.json`（已挂进容器，重启不丢）。

### 2.7 日常运维

```bash
cd fnmusic_ext

sudo docker compose ps                      # 看状态
sudo docker compose logs -f xmly            # 看某个音源日志
sudo docker compose restart musicbox        # 重启单个音源
sudo docker compose up -d --build           # 改了服务代码后重建
sudo docker compose up -d --build xmly      # 只重建喜马拉雅
sudo docker compose down                    # 停并删除容器（数据卷 xmly-data / musicbox-data 保留）

sudo systemctl status fnmusic-ext           # 核心代理状态
sudo journalctl -u fnmusic-ext -f           # 核心代理日志
```

> **排障第一入口**：`~/fnmusic_ext/access_probe.log` 记录了每个请求的
> `方法 路径 | 状态码 | 耗时 | Content-Type | len`。手机 App 的请求带 `?lan=zh-CN`，
> 可据此把它和网页/脚本请求区分开。

### 2.8 端口与数据卷

| 服务 | 宿主端口 | 绑定 | 数据卷 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `musicbox` | `8770` | `0.0.0.0` | `./musicbox-data` | 绑定 `0.0.0.0` 是因为网易云扫码登录要用手机访问二维码 |
| `musicdl` | `8768` | `127.0.0.1` | — | |
| `lxmusic` | `8772` | `127.0.0.1` | — | |
| `xmly` | `8774` | `127.0.0.1` | `./xmly-data` | 登录 cookie 落在这里 |
| **管理后台** | `8799` | `0.0.0.0` | — | 由核心代理提供，**不是** compose 服务 |

> 端口全局固定。多副本部署会冲突，install/restore 会检查归属并拒绝接管他人容器。

### 2.9 只想要部分音源？

直接注释掉 `docker-compose.yml` 里不需要的 service 块即可，并同步把 `.env` 里对应的
`FNMUSIC_*_ENABLED` 设为 `false`，避免代理去连一个不存在的端口。

---

## 怎么用

### 搜音乐

打开飞牛音乐 Web 端或手机 App，搜索框直接输入歌名（如「晴天」），即点即播。

### 听小说（喜马拉雅）

在搜索框加触发词：

```
小说 吞噬星空
```

- 「歌曲」维度：每行 = 一部小说，点开直接播第 1 集；
- 「歌单」维度：每行 = 一部小说 = 一个歌单，点开就是完整章节列表。

触发词有三个：`小说` / `喜马拉雅` / `xmly`（大小写均可），后面留一个空格再写书名。
**不带触发词时完全走原来的网易云 / 洛雪 / 音乐猫链路，与常规音乐搜索互不干扰。**

### 管理后台

`http://<NAS_IP>:8799/admin`

- **音源**：各音源开关与健康状态、网易云 / 喜马拉雅扫码登录；
- **歌单**：**综合搜索**（搜索栏左侧可切「歌单 / 小说」），搜索结果可直接加入「当前歌单」；
- **配置**：改 `.env` 项（部分需重启，页面会提示）。

### 网易云扫码（若启用 musicbox）

```bash
./netease_login.sh          # 终端 ASCII 二维码，过期自动刷新
# 或：./install.sh --qr  /  ./extend.sh --qr
# 局域网图片版：http://<NAS_IP>:8770/api/v1/auth/login/qr.png
```

### 健康检查

```bash
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
```

---

## 维护与一键还原

```bash
./restore.sh          # 还原官方原生直连（秒级切回；停删音源容器，保留 .env 与全部数据）
./restore.sh --full   # 彻底卸载（额外删除 .env、登录态、缓存、收藏、历史）
```

> **多副本部署提示**：容器名（`fnmusic-musicdl/musicbox/lxmusic/xmly`）与端口
> （8768/8770/8772/8774）全局固定。安装/恢复会检查部署归属并串行化操作；
> 遇到其他目录的服务或容器会拒绝接管，不再自动删除。请在现有部署目录维护服务。
> 身份不明或官方 socket 已改变时，恢复会保留现场并报告未完成，**禁止手工猜测后删除 socket**。

---

## 环境变量配置

根目录 `.env` 由安装向导生成与维护（`chmod 600`，**绝不提交**）。主要项：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_MODE` | `docker` | 运行模式：`docker` 或 `host` |
| `FNMUSIC_NETEASE_ENABLED` | `true` | 是否启用网易云音源（musicbox） |
| `FNMUSIC_MUSICBOX_URL` | `http://127.0.0.1:8770` | 网易云音源服务地址 |
| `FNMUSIC_MUSICDL_ENABLED` | `true` | 是否启用聚合音源（musicdl） |
| `FNMUSIC_MUSICDL_URL` | `http://127.0.0.1:8768` | 聚合音源服务地址 |
| `FNMUSIC_LX_ENABLED` | `true` | 是否启用洛雪免登录音源（lxmusic） |
| `FNMUSIC_LX_URL` | `http://127.0.0.1:8772` | 洛雪音源服务地址 |
| **`FNMUSIC_XMLY_ENABLED`** | `true` | **是否启用喜马拉雅音源（xmly）** |
| **`FNMUSIC_XMLY_URL`** | `http://127.0.0.1:8774` | **喜马拉雅音源服务地址** |
| **`FNMUSIC_ADMIN_PORT`** | `8799` | **Web 管理后台端口** |
| `LX_SOURCES` | `kg,wy,mg,kw` | lxmusic 子源列表 |
| `LX_THIRD_PARTY` | `1` | lxmusic 第三方解析链路总开关（关闭后退化为官方免登录直连） |
| `LX_RESOLVER_TIMEOUT` | `4.0` | 第三方链路单次解析超时（秒） |
| `FNMUSIC_ONLINE_SOURCES` | `MiguMusicClient,KuwoMusicClient` | musicdl 启用的子平台列表 |
| `FNMUSIC_TEE_SAVE_ENABLED` | `true` | 边听边存开关 |
| `FNMUSIC_TEE_SAVE_DIR` | *(空)* | 边听边存保存路径；留空=自动探测飞牛共享曲库 |
| `FNMUSIC_TEE_CACHE_MAX` | `2` | 关闭边听边存时滚动保留的最新试听缓存条数 |
| `FNMUSIC_SEARCH_TIMEOUT` | `3.0` | 多音源并发搜索常规等待预算（秒） |
| `FNMUSIC_SEARCH_RANK` | `cover_quality` | 搜索排序：`cover_quality` / `quality_cover` / `off` |
| `FNMUSIC_SEARCH_RANK_WAIT_S` | `1.5` | 首屏等「补全 + 重排」落定的上限（秒） |
| `FNMUSIC_LLM_BASE_URL` | *(空)* | 大模型 Base URL（仅网易音源未启用时作为每日推荐兜底） |
| `FNMUSIC_LLM_API_KEY` | *(空)* | 大模型 API Key（仅网易音源未启用时使用） |
| `FNMUSIC_LLM_MODEL` | `gpt-4o-mini` | 兜底推荐生成模型 |
| `FNMUSIC_BASE_IMAGE` | *(空)* | Docker 基础镜像引用，留空自动探测 |
| `FNMUSIC_PIP_INDEX` | 清华 | 容器内 pip 源 |
| `FNMUSIC_APT_MIRROR` | 清华 | musicdl 镜像构建层 apt 源 |
| `FNMUSIC_VERSION` | `1.7.0` | 当前安装的版本号 |

完整清单见 [`.env.example`](.env.example)。

---

## 目录结构

```
fnmusic_ext/
├── install.sh  extend.sh  restore.sh   # 安装 / 接管 / 还原
├── ensure_base_image.sh                # Docker 基础镜像源自动探测
├── docker-compose.yml                  # 音源服务栈（4 容器）
├── proxy/                              # 核心代理（接管 socket + 管理后台）
│   ├── app.py                          #   主程序
│   ├── admin_ui.html                   #   管理后台前端
│   ├── takeover.py                     #   socket 接管与归属判定
│   └── tests/                          #   单元测试
├── musicbox-service/                   # 网易云音源   8770
├── musicdl-service/                    # 酷我/咪咕音源 8768
├── lxmusic-service/                    # 洛雪音源     8772
├── xmly-service/                       # 喜马拉雅音源 8774（本分支新增）
├── musicbox-data/  xmly-data/          # 登录态数据卷（不入库）
├── patches/  reports/                  # 补丁脚本与验收报告
├── DIFFERENCES.md                      # 与上游 v1.6.0 的逐项差异
├── PATCHES.md                          # 补丁清单
└── v76-变更说明.md  v77-变更说明.md  v78-变更说明.md
```

---

## 实现原理

### 1. Inode 接管与零侵入无缝串联

飞牛官方架构中，前端 Nginx 通过本地 Unix Domain Socket（`/var/run/trim_music.socket`）
与官方 Go 编写的后端服务通信。`fnmusic-ext` 利用 Linux 文件系统的 Socket Inode 机制：

1. 将官方套接字平滑重命名为 `trim_music_upstream.socket`；
2. 代理服务在原路径 `/var/run/trim_music.socket` 建立同名监听并赋予相同权限；
3. 官方 Nginx 与客户端对此完全无感知。

```text
[飞牛音乐客户端 (Web/App)]
          │
          ▼
    [飞牛 Nginx 代理]
          │ (通过 Unix Socket 请求)
          ▼
┌─────────────────────────────────────────────────────────────┐
│  fnmusic-ext 扩展代理 (/var/run/trim_music.socket)          │
│  ├─ 本地接口透传 ──► 官方后端 (trim_music_upstream.sock)     │
│  ├─ 在线搜索聚合 ──► 并发调度 musicbox/musicdl/lx/xmly      │
│  ├─ 边播边落盘   ──► 流式 Tee 写入本地 cache/ 目录           │
│  └─ 每日推荐歌单 ──► 注入音源原生推荐/榜单（或 LLM 兜底）      │
└─────────────────────────────────────────────────────────────┘
```

### 2. 核心拦截与增强逻辑

- **搜索拦截（`/music/api/v1/search/track`）**：透传给官方服务取本地歌曲，
  同时并发调度已启用的在线音源；按 `(title, artist)` 去重合并后渐进式返回。
  带喜马拉雅触发词时整条链路劫持到 xmly。
- **流媒体播放与边播边存（`/music/api/v1/track/stream`）**：拦截 `online:...` GUID，
  解析真实直链后返回 `206 Partial Content` 流式切片，后台 Tee Task 异步落盘。
- **多用户隔离在线收藏（`/music/api/v1/favorite/track`）**：按当前登录用户 GUID 独立记录在
  `online_favorites/`，读取时与官方本地收藏合并展示。
- **封面（`static/cover`）**：恒返回 `200` + 真实图片字节，四级兜底
  （`_online_info` → `meta_cache` → 音源详情 → 内置占位 PNG），**绝不 404、绝不返回 JSON**。
- **容灾与自动降级**：任一音源异常即隔离并仅返回可用数据；代理进程异常退出时自动切回官方直连。

---

## 免责与版权声明

### 1. 技术研究与非商业用途

- 本项目（`fnmusic-ext`）基于 **MIT 许可证** 开源发布，立项初衷仅为个人开发者探讨
  Linux Unix Domain Socket 机制、透明反向代理技术、流式媒体传输与多协程并发架构的
  技术验证与学习交流。
- 本项目严格限定于**个人技术研究与非商业用途**。任何个人、团队或商业实体严禁将本项目、
  其衍生版本或相关工具用于任何形式的商业营利、付费订阅、软硬件捆绑销售或非法牟利行为。

### 2. 致谢上游开源项目与无侵权声明

- 本项目在线音源检索与元数据抓取能力依赖于社区优秀的开源组件：
  - [CharlesPikachu/musicdl](https://github.com/CharlesPikachu/musicdl)
  - [darknessomi/musicbox](https://github.com/darknessomi/musicbox)
  - 洛雪音乐（LX Music）社区音源思路与 [pdone/lx-music-source](https://github.com/pdone/lx-music-source)
    社区聚合音源的链路清单思路（本仓库 `lxmusic-service` 为独立 Python 实现，仅移植其多链路回退架构，
    不包含其脚本代码）
  - 喜马拉雅相关能力仅为**本地私有云环境下的协议中继与数据适配**，参考了社区对公开 Web 接口的
    研究结论，不包含任何破解逻辑。
  在此向上游开源项目的原作者与贡献者致以崇高的敬意。
- 本项目仅在本地私有云环境充当**协议中继与数据适配胶水层**，本身不具备任何音源破解或版权规避逻辑，
  主观上绝无任何侵犯各音乐平台、唱片公司或第三方知识产权的意图。

### 3. 音频及视听数据版权归属

- **音频及元数据版权全权归属各原始版权方**（包括但不限于各唱片公司、独立音乐人及各在线音乐服务平台）。
- **零托管、零存储原则**：本项目服务器及开源代码仓库**不托管、不分发、不直接存储任何受版权保护的
  音频、视频、歌词或专辑封面文件**。所有音频流与图文元数据均系客户端发起请求时，
  由代理服务实时转发自公开网络接口或源站 CDN。
- **登录凭据仅在本地**：网易云 / 喜马拉雅的登录 cookie 只写入本机 `.env` 与数据卷目录
  （`musicbox-data/` / `xmly-data/`，均已被 `.gitignore` 排除），**不会上传、不会出现在日志里**。
  扫码登录仅用于获取您本人账号已有的收听权限。
- **本地缓存试听合规要求**：边播边落盘功能所生成的本地临时缓存文件（Cache），仅供个人离线技术分析、
  音频标签兼容性测试与学习评估。**使用者请在试听或测试后 24 小时内自行删除相关音频文件**。
- **倡导正版**：请大家支持正版数字音乐事业！如需长期收听、收藏或获得更高品质的音乐体验，
  请前往网易云音乐、酷我音乐、咪咕音乐、喜马拉雅等官方平台开通正版会员并购买正版专辑。

### 4. 免责与使用者风险自担

- 使用者在下载、部署或运行本项目前，应充分知悉并自愿遵守所在国家/地区的法律法规，
  以及第三方服务平台的用户协议。
- **风险自担**：由于使用者滥用、恶意传播、商业化使用或不当配置本项目而导致的一切法律责任、
  版权纠纷、账号封禁、IP 拦截或连带经济损失，**概由使用者本人自行承担全部责任**，
  本项目发起人、维护者及社区贡献者不承担任何直接、间接或连带的法律责任。
- **权利人联系通道**：若相关版权权利人认为本项目的代码实现或接口中继涉嫌侵犯其合法权益，
  请通过 GitHub Issue 或电子邮件向项目维护团队提交权属证明通知。
  我们将在收到通知并核实后的第一时间积极配合，并及时下架、修改或删除涉嫌侵权的代码与功能。
