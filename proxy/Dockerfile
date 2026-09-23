# fnmusic-ext 核心代理容器镜像
#
# 关键约束（来自 proxy/takeover.py）：
#   1. supervise() 硬编码以 <base>/.venv-proxy/bin/python 启动 uvicorn 子进程，
#      因此镜像必须在 /app/.venv-proxy 构建虚拟环境（<base> 在容器内为 /app）。
#   2. environment() 强制 FNMUSIC_HOME=/app，所以缓存/收藏/历史/推荐等数据目录
#      会解析到 /app/cache、/app/online_favorites、/app/play_history、
#      /app/recommend_cache；这些目录由宿主机 bind 挂载进来以持久化数据。
#   3. 代理通过 renameat2 接管宿主机 /var/run/trim_music.socket（fail-closed），
#      因此容器必须以 pid: host + 挂载 /var/run 运行（见 docker-compose.yml）。
#   4. 容器 PID 1 必须是 takeover.py（entrypoint 用 exec），这样 `docker stop`
#      发出的 SIGTERM 才能到达 supervise() 的 finally，自动复原官方 socket。
# 基础镜像可由 .env 的 FNMUSIC_BASE_IMAGE 覆盖（默认用 docker.1ms.run 镜像，
# 因为 fnOS 直连 docker.io 经常超时；与音乐源服务的构建保持一致）。
ARG BASE_IMAGE=docker.1ms.run/library/python:3.13-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 复制代理代码（.dockerignore 已排除 .venv-proxy / __pycache__ / tests 等）
COPY . /app/proxy/

# 构建 takeover.py supervise() 硬编码要求的 /app/.venv-proxy
# 注意：fnOS 上清华 PyPI 返回 403，统一使用阿里云镜像。
RUN python3 -m venv /app/.venv-proxy \
 && /app/.venv-proxy/bin/pip install --no-cache-dir -r /app/proxy/requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple

RUN chmod +x /app/proxy/entrypoint.sh

# PID 1 = takeover.py run；docker stop 触发 supervise() finally -> state.restore()
CMD ["/app/proxy/entrypoint.sh"]
