#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fnmusic-musicbox 网易云登录态守卫（v75）。

背景：musicbox 容器里的 uvicorn 常驻进程会把 NetEase() 实例缓存起来，
一旦磁盘上的 cookie 在启动之后被刷新（重新扫码登录），进程内实例仍持有旧
cookie —— check_is_logged_in() 恒为 False，filter_playable_song_ids() 于是
把所有 fee 不在 (0, 8) 的曲目（VIP / 付费）全部剔除，表现为「网易云明明有
这首歌，搜索结果里却没有」（实测 50 条只剩 8 条，甚至 0 条）。

守卫逻辑（每 30 分钟一次）：
  1. GET http://127.0.0.1:8770/api/v1/recommend/songs?limit=10 看 logged_in；
  2. 为 true  -> 只确认自愈补丁还在位（不在就拷进去，下次重启生效）；
  3. 为 false -> 等 45s 复检一次，仍为 false 才把补丁拷进容器并重启它。

日志：<项目目录>/mbguard.log
"""
import json
import os
import subprocess
import time
import urllib.request

# 项目目录：优先 FNMUSIC_HOME，缺省为本脚本所在目录（不写死任何具体路径）
HOME = os.environ.get("FNMUSIC_HOME") or os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HOME, "mbguard.log")
CT = "fnmusic-musicbox"
SRC = os.path.join(HOME, "musicbox-data", "netease_ext.py.v75")
URL = "http://127.0.0.1:8770/api/v1/recommend/songs?limit=10"


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def sh(args, timeout=60):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def logged_in():
    try:
        with urllib.request.urlopen(URL, timeout=25) as r:
            d = json.loads(r.read() or b"{}")
        return bool(d.get("logged_in")), (d.get("logged_in") if "logged_in" in d else "missing")
    except Exception as e:
        return None, "err:%s" % str(e)[:80]


def patch_present():
    rc, out, _ = sh(["docker", "exec", CT, "grep", "-c", "_LOGIN_STATE", "/app/netease_ext.py"])
    return rc == 0 and out.strip().isdigit() and int(out.strip()) > 0


def ensure_patch():
    if patch_present():
        return True
    if not os.path.exists(SRC):
        log("补丁源文件缺失: %s" % SRC)
        return False
    rc, out, err = sh(["docker", "cp", SRC, "%s:/app/netease_ext.py" % CT])
    log("补打补丁 -> rc=%s %s%s" % (rc, out[:80], err[:120]))
    return rc == 0


def main():
    ok, raw = logged_in()
    if ok is True:
        ensure_patch()
        log("ok logged_in=%s" % raw)
        return 0
    log("登录态异常 logged_in=%s，45s 后复检" % raw)
    time.sleep(45)
    ok2, raw2 = logged_in()
    if ok2 is True:
        log("复检恢复 logged_in=%s，无需重启" % raw2)
        return 0
    ensure_patch()
    rc, out, err = sh(["docker", "restart", CT], timeout=120)
    log("已重启 %s -> rc=%s %s%s" % (CT, rc, out[:80], err[:120]))
    return 0


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("EXCEPTION %s" % str(e)[:200])
