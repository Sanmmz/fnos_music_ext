#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# fnmusic-ext 代理容器入口
#
# 设计要点：
#   * 代理以 renameat2 接管宿主机 /var/run/trim_music.socket，并在退出时（SIGTERM）
#     通过 supervise() 的 finally 块复原官方 socket（fail-closed 安全机制）。
#   * `docker stop` 默认向容器 PID 1 发送 SIGTERM。本入口用 exec 让 takeover.py
#     成为 PID 1，因此信号直达 supervise()，确保退出时官方 socket 被复原，
#     不会把飞牛音乐留在「代理消失、官方未接管」的不可达状态。
#   * --state-dir /app/.runtime：接管状态（ownership.json 等）落在项目持久化目录，
#     与 docker-compose.yml 的 .runtime 挂载一致，容器重建后仍可校验身份。
#   * --base /app：代码与 .env 均在 /app，environment() 会强制 FNMUSIC_HOME=/app。
# ==============================================================================

# 确保状态目录归 root 所有（容器以 root 运行，state 锁要求目录属主 == euid）
mkdir -p /app/.runtime
if [ "$(id -u)" -eq 0 ] && [ -O /app/.runtime ]; then
    : # 已属 root
fi

exec /app/.venv-proxy/bin/python /app/proxy/takeover.py run --base /app --state-dir /app/.runtime
