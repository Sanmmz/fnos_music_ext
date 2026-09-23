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

## 🚀 部署方式：docker-compose

本项目**只使用 docker-compose 部署**：4 个音源容器 + 核心代理容器（`fnmusic-proxy`）
由一条 `docker compose up -d --build` 全部拉起。不需要 systemd，也不需要 `install.sh` / `extend.sh`。

```
┌──────────────────────────────────────────────────────────────┐
│ docker compose（一条命令拉起全部，5 个容器）                   │
│   fnmusic-musicbox  8770  → 网易云                            │
│   fnmusic-musicdl   8768  → 酷我 / 咪咕                       │
│   fnmusic-lxmusic   8772  → 洛雪免登录解析                     │
│   fnmusic-xmly      8774  → 喜马拉雅有声书                     │
│   fnmusic-proxy     ——    → 核心代理（接管官方 socket）         │
└──────────────────────────────────────────────────────────────┘
```

核心代理容器以 `pid: host` + `network_mode: host` + 挂载 `/var/run`（rw）运行，
复用宿主机 PID 与 socket 命名空间，因此接管 `/var/run/trim_music.socket` 的
fail-closed 安全机制**与 systemd 模式完全一致**：`docker stop` 时容器自动复原官方 socket，
绝不会让飞牛音乐失联。

> 仓库里的 `install.sh` / `extend.sh` / `restore.sh` 仍保留，仅作为上游遗留的应急回退脚本，
> **正常部署不需要执行它们**。

## 前置准备

1. 已在 fnOS「应用中心」安装并启动官方 **【飞牛音乐】** 应用
   （v1.4.0 起已适配 2026-09-11 升级后的新版官方应用，旧版同样兼容；
   官方应用升级后若扩展未生效，`docker compose restart proxy` 即可恢复接管）；
2. 宿主机已安装 **Docker**（fnOS 应用中心一键安装）与 `git`；
3. 建议先 `cd` 到准备存放部署文件的目录（例如 `~/docker/music`，任意可写目录均可）。

---

## 部署步骤

### 整体结构

即上面那张图：音源各占一个容器，核心代理容器接管官方 socket 后对外提供增强能力，
管理后台由核心代理在 `8799` 端口提供。

### 步骤 1：获取代码

```bash
git clone https://github.com/Sanmmz/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext
chmod +x install.sh extend.sh restore.sh proxy/run_proxy.sh ensure_base_image.sh
```

### 步骤 2：准备 `.env`

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

### 步骤 3：构建并启动（含核心代理）

```bash
sudo docker compose up -d --build
```

首次构建约需几分钟（5 个镜像都要拉基础镜像 + pip 装包）。完成后：

```bash
sudo docker compose ps
```

预期输出（5 个 `Up (healthy)`）：

```
NAME                STATUS              PORTS
fnmusic-musicbox    Up (healthy)        0.0.0.0:8770->8000/tcp
fnmusic-musicdl     Up (healthy)        127.0.0.1:8768->8000/tcp
fnmusic-lxmusic     Up (healthy)        127.0.0.1:8772->8000/tcp
fnmusic-xmly        Up (healthy)        127.0.0.1:8774->8000/tcp
fnmusic-proxy       Up (healthy)
```

> `proxy` 服务就是核心代理，与音源一起被上面这条命令拉起；单独重建它用
> `sudo docker compose up -d --build proxy`。

逐个验证健康接口：

```bash
for p in 8770 8768 8772 8774; do
  printf "%s -> " "$p"
  curl -s -m 5 "http://127.0.0.1:${p}/healthz" || echo "FAIL"
  echo
done
```

### 步骤 4：确认接管成功

```bash
docker ps --filter name=fnmusic-proxy      # 应看到 Up (healthy)
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
```

预期：容器 `Up (healthy)`，健康检查返回 `{"ok":true,...}`。

> 若健康检查连不上，先看日志：`sudo docker compose logs --tail 100 proxy`。
> 容器启动时若检测到官方 socket 已被别的部署接管，会**拒绝接管并保留现场**（fail-closed），
> 这时请先到原有部署目录 `docker compose down`，**不要手工删除 socket**。

### 步骤 5：喜马拉雅扫码登录（可选）

打开 `http://<NAS_IP>:8799/admin` → 左侧「音源」→ 🎙️ **喜马拉雅（有声书）** → **扫码登录**，
用喜马拉雅 App 扫码。登录态写在 `xmly-data/xmly_cookie.json`（已挂进容器，重启不丢）。

### 日常运维

```bash
cd fnmusic_ext

sudo docker compose ps                      # 看状态
sudo docker compose logs -f xmly            # 看某个音源日志
sudo docker compose restart musicbox        # 重启单个音源
sudo docker compose up -d --build           # 改了服务代码后重建
sudo docker compose up -d --build xmly      # 只重建喜马拉雅
sudo docker compose down                    # 停并删除全部容器（数据卷保留）
```

> **排障第一入口**：`sudo docker compose logs -f proxy`，以及代理容器内 `/app/access_probe.log`
> （记录了每个请求的 `方法 路径 | 状态码 | 耗时 | Content-Type | len`）。手机 App 的请求带 `?lan=zh-CN`,
> 可据此把它和网页/脚本请求区分开。

### 端口与数据卷

| 服务 | 宿主端口 | 绑定 | 数据卷 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| `musicbox` | `8770` | `0.0.0.0` | `./musicbox-data` | 绑定 `0.0.0.0` 是因为网易云扫码登录要用手机访问二维码 |
| `musicdl` | `8768` | `127.0.0.1` | — | |
| `lxmusic` | `8772` | `127.0.0.1` | — | |
| `xmly` | `8774` | `127.0.0.1` | `./xmly-data` | 登录 cookie 落在这里 |
| `proxy` | — | — | `./downloads` 等 | 核心代理。接管官方 socket，**不占 TCP 端口** |
| **管理后台** | `8799` | `0.0.0.0` | — | 由核心代理容器 `fnmusic-proxy` 提供 |

> 端口全局固定。多副本部署会冲突，核心代理启动时会检查 socket 归属并拒绝接管他人容器。

### 只想要部分音源？

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

打开管理后台 `http://<NAS_IP>:8799/admin` →「音源」→ 网易云 → **扫码登录**即可。
（也可以直接访问 musicbox 的二维码图片：<http://<NAS_IP>:8770/api/v1/auth/login/qr.png>）

### 健康检查

```bash
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz
```

---

## 维护与还原

```bash
cd fnmusic_ext
sudo docker compose ps                 # 看状态
sudo docker compose logs -f proxy      # 看核心代理日志
sudo docker compose restart proxy      # 重启核心代理
sudo docker compose down               # 停并删除全部容器（数据卷保留）
```

**还原官方原生直连**：停止核心代理容器即可 —— 容器退出时会自动把
`/var/run/trim_music.socket` 复原为官方 socket，飞牛音乐立刻回到原生状态：

```bash
sudo docker compose stop proxy
```

想彻底卸载（连数据卷一起清）：

```bash
sudo docker compose down -v            # 删除容器 + 数据卷（登录态 / 缓存 / 收藏 / 历史）
rm -rf fnmusic_ext                     # 删除代码目录
```

> **多副本部署提示**：容器名（`fnmusic-musicdl/musicbox/lxmusic/xmly/proxy`）与端口
> （8768/8770/8772/8774）全局固定。核心代理启动时会检查 socket 归属，
> 遇到其他目录已接管的情况会拒绝接管并保留现场，**禁止手工猜测后删除 socket**。

---

## 环境变量配置

根目录 `.env` 由你从 `.env.example` 复制后自行维护（`chmod 600`，**绝不提交**）。主要项：

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `FNMUSIC_PROXY_MODE` | `docker` | 代理运行模式：`docker`（compose 容器）或 `host`（宿主机 systemd，仅应急回退） |
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
| `FNMUSIC_TEE_SAVE_DIR` | *(空)* | 音频落盘目录；留空=自动探测飞牛共享曲库。**曲库若是 rclone 云盘（fuse），容器里写不进去**，此时请改指向一个本地目录并把该目录加进飞牛音乐库 |
| `FNMUSIC_TEE_CACHE_MAX` | `2` | 关闭边听边存时滚动保留的最新试听缓存条数 |
| `FNMUSIC_SEARCH_TIMEOUT` | `3.0` | 多音源并发搜索常规等待预算（秒） |
| `FNMUSIC_ONLINE_HISTORY_MODE` | `off` | 在线曲目是否写入收藏 / 播放历史：`full`=写入（收藏即下载、播放即下载、取消收藏即删除均依赖它），`off`=不写入 |
| `FNMUSIC_SEARCH_RANK` | `cover_quality` | 搜索排序：`cover_quality` / `quality_cover` / `off` |
| `FNMUSIC_SEARCH_RANK_WAIT_S` | `1.5` | 首屏等「补全 + 重排」落定的上限（秒） |
| `FNMUSIC_LLM_BASE_URL` | *(空)* | 大模型 Base URL（仅网易音源未启用时作为每日推荐兜底） |
| `FNMUSIC_LLM_API_KEY` | *(空)* | 大模型 API Key（仅网易音源未启用时使用） |
| `FNMUSIC_LLM_MODEL` | `gpt-4o-mini` | 兜底推荐生成模型 |
| `FNMUSIC_BASE_IMAGE` | *(空)* | Docker 基础镜像引用，留空自动探测 |
| `FNMUSIC_PIP_INDEX` | 清华 | 容器内 pip 源 |
| `FNMUSIC_APT_MIRROR` | 清华 | musicdl 镜像构建层 apt 源 |
| `FNMUSIC_VERSION` | `1.9.0` | 当前安装的版本号 |

完整清单见 [`.env.example`](.env.example)。

---

## 目录结构

```
fnmusic_ext/
├── docker-compose.yml                  # 部署入口（音源 + 核心代理全栈）
├── ensure_base_image.sh                # Docker 基础镜像源自动探测
├── install.sh  extend.sh  restore.sh   # 上游遗留脚本（仅应急回退，正常部署不用）
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
