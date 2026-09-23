# 部署与运维指南（docker-compose）

适用环境：飞牛 NAS（fnOS）已安装并启动「飞牛音乐」官方应用。本项目采用无侵入接管设计，**完全不修改**飞牛官方 nginx 配置、不 Patch 官方 Go 二进制、不改动官方数据库。

> 🚀 **部署只有一种方式：docker-compose。** 一键脚本（install.sh / extend.sh / restore.sh）仅作为上游遗留的应急回退保留，正常部署不需要执行。

> 💡 **自动化部署提示**：若使用 AI Agent（如 OpenCode、Claude Code、Cursor 等）进行全流程自动化部署与自检验收，请直接查阅 [Agent 安装提示词](AGENT_INSTALL.md)。

---

## 0. 前置准备

- **操作系统**：fnOS（Debian 12 基础系统）；
- **基础运行组件**：Python 3.11+ 及 `python3-venv` 虚拟环境模块；
  ```bash
  sudo apt-get update && sudo apt-get install -y python3 python3-venv git
  ```
- **管理员权限**：具备 `sudo` 执行权限的管理员账号；
- **官方音乐应用**：必须先在 fnOS「应用中心」安装并启动「飞牛音乐」（确保存在 `/var/run/trim_music.socket`）；
- **Docker 环境（若选 Docker 模式）**：必须先在 fnOS「应用中心」安装好 Docker，**脚本绝不会擅自安装 Docker 引擎**；

克隆项目并进入根目录赋予执行权限：
```bash
git clone https://github.com/Sanmmz/fnos_music_ext.git fnmusic_ext
cd fnmusic_ext
chmod +x ensure_base_image.sh
```

---

## 1. 部署形态：只有 docker-compose 一种

> ⚠️ **本仓库只使用 docker-compose 部署。** 音源与核心代理全部由
> `docker compose up -d --build` 拉起，不再需要 `install.sh` / `extend.sh`，
> 也不再使用宿主机 systemd（host 模式仅作为应急回退保留）。
> 完整步骤以 [README](../README.md#部署步骤) 为准，本文补充运维细节。

```bash
cd fnmusic_ext              # 你的部署目录，例如 ~/docker/music

cp .env.example .env && chmod 600 .env
./ensure_base_image.sh      # 探测可用的 Docker 基础镜像（国内网络建议先跑）
sudo docker compose up -d --build
```

拉起后会得到 5 个容器：

| 容器 | 端口 | 作用 |
| :--- | :--- | :--- |
| `fnmusic-musicbox` | `0.0.0.0:8770` | 网易云（绑定 0.0.0.0 是为了手机扫二维码登录） |
| `fnmusic-musicdl` | `127.0.0.1:8768` | 酷我 / 咪咕 |
| `fnmusic-lxmusic` | `127.0.0.1:8772` | 洛雪免登录解析 |
| `fnmusic-xmly` | `127.0.0.1:8774` | 喜马拉雅有声书 |
| `fnmusic-proxy` | — | 核心代理，接管 `/var/run/trim_music.socket` |

核心代理容器以 `pid: host` + `network_mode: host` + 挂载 `/var/run`（rw）运行，
复用宿主机 PID 与 socket 命名空间，因此接管与复原官方 socket 的 fail-closed 安全机制
与宿主机 systemd 模式**完全一致**：`docker stop` 时自动复原，不会让飞牛音乐失联。

---

## 2. 日常运维

```bash
sudo docker compose ps                 # 看状态
sudo docker compose logs -f proxy      # 看核心代理日志
sudo docker compose restart musicbox   # 重启单个音源
sudo docker compose up -d --build      # 改了服务代码后重建全部
sudo docker compose up -d --build proxy # 只重建核心代理
```

---

## 3. 还原与卸载

```bash
# 还原官方原生直连：停掉核心代理即可，容器退出时自动复原官方 socket
sudo docker compose stop proxy

# 彻底卸载（连数据卷一起清）
sudo docker compose down -v
rm -rf fnmusic_ext
```

> 多副本部署提示：容器名与端口全局固定。核心代理启动时会检查 socket 归属，
> 遇到其他目录已接管的情况会**拒绝接管并保留现场**，禁止手工删除 socket。


---

## 4. 网易云登录扫码（若启用 musicbox）

网易云部分 VIP 或无损音质曲目需要用户登录。在局域网内任意设备的浏览器访问：
```text
http://<飞牛NAS的IP地址>:8770/api/v1/auth/login/qr.png
```
使用手机【网易云音乐 App】扫码确认登录即可，登录凭证自动持久化在本地 `musicbox-data/` 目录中，无需重复扫码。

---

## 5. 每日推荐工作机制

用户登录飞牛音乐 Web 端或 App，左侧歌单顶部将呈现专属「每日推荐」歌单，默认**优先采信音乐源原生推荐**（单链逐级补齐至 20 首）：
1. **网易真·每日推荐**：网易云音源启用且已登录时，通过 musicbox 直接调用网易云每日推荐（`weapi`，个性化推荐）；
2. **网易免登录榜单**：网易云启用但未登录（或第 1 级不足）时，自动降级为网易热歌榜等精选榜单；
3. **洛雪免登录榜单**：洛雪音源启用时，聚合酷狗 TOP500、酷我飙升榜与网易新歌速递（实测免登录可用，经媒体探活验证）；
4. **大模型兜底**：仅当网易音源未启用且在 `.env` 中配置了 `FNMUSIC_LLM_*` 时，通过 LLM 生成候选歌名再经音源搜索匹配；
5. **关键词兜底**：全链路失败时，根据用户听歌历史与本地曲库热门池关键词检索，确保歌单始终可用。

平台原生推荐直接返回曲目 ID，直连详情并复用真实可播性过滤，不再依赖大模型与模糊搜索匹配。

---

## 6. 健康检查与验收

在终端执行以下命令探测代理服务与上游各组件的连通状态：

```bash
# 探测代理端点健康状态
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz

# 运行本地自动化测试集（需先 pip install -r proxy/requirements.txt pytest qrcode pillow）
python3 -m pytest
```

`healthz` 响应正常示例：
```json
{"ok":true,"upstream":"ok","musicdl":"ok","musicbox":"ok","lxmusic":"ok","llm":"disabled","recommend":{"mode":"source-native","netease":true,"lx":true,"llm_fallback":false,"recent":{}}}
```
- `ok` 为 `true` 表示上游官方音乐后端连通正常，且至少有一个音源处于工作状态；
- 未启用的音源会显示为 `"disabled"`。

## 7. 常见问题排查

### 容器日志刷 `PermissionError: [Errno 13] Permission denied: '/app/app.py'`

v1.2.1 及更早版本的已知问题：镜像内源码文件权限继承了仓库检出时的 umask。
若曾在 umask 077 的环境（root shell、`sudo git clone` 等）下检出仓库，`app.py` 为 600，
容器内非 root 的 `appuser` 无法读取，uvicorn 启动失败并随 `restart: unless-stopped` 无限重启。

v1.2.2 起已修复（镜像内文件统一 `--chown=appuser` 且权限 644，与宿主机文件权限解耦）。
升级方法：

```bash
git pull && ./install.sh
```

Dockerfile 的变更会使对应构建层缓存失效，重新安装时会自动重建镜像，无需 `--no-cache`。

### 构建时报 `failed to resolve source metadata for python:3.13-slim ... 401 Unauthorized` 或拉取超时

多为 fnOS 等系统在 Docker daemon 全局配置的镜像加速器（如 `docker.fnnas.com`）异常所致：
BuildKit 解析 `python:3.13-slim` 元数据时会先经过该加速器，失败后不会自动回退官方 Docker Hub，
`docker compose up --build` 随即失败。

v1.2.3 起安装脚本会在构建前自动探测可用源：**国内镜像优先**（完整镜像源引用直连，绕开 daemon
加速器，真实拉取验证），逐个尝试 docker.1ms.run / docker.m.daocloud.io / docker.1panel.live /
hub.rat.dev，全部失败再兜底官方源，结果缓存到 `.env` 的 `FNMUSIC_BASE_IMAGE`。
全程不修改系统 Docker 配置，仅本应用构建生效。

手动指定（例如自动探测全部失败、或偏好特定镜像源时）：

```bash
# 方式一：安装时通过环境变量指定
BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim ./install.sh

# 方式二：写入 .env（之后所有重建自动沿用）
# FNMUSIC_BASE_IMAGE=docker.m.daocloud.io/library/python:3.13-slim
```

自定义国内镜像候选列表：设置环境变量 `FNMUSIC_DOCKER_MIRRORS`（空格分隔，按序尝试）。

Dockerfile 的变更会使对应构建层缓存失效，重新安装时会自动重建镜像，无需 `--no-cache`。
