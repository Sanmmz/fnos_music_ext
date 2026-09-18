# patches/ —— 构建器与验收脚本

这里的脚本是 `proxy/app.py` 那批改动的**生成器与证据**，不是运行时依赖。
生产环境只需要 `proxy/app.py` 一个文件。

> ⚠️ **这些脚本里带 `<...>` 的路径是脱敏占位符**，运行前请按你的环境替换
> （或通过同名环境变量覆盖）。对应关系见下表。

## 占位符对照

| 占位符 | 含义 | 示例真实值 |
|---|---|---|
| `<user>` | NAS 上的用户名 | `/home/`**`youruser`**`/fnmusic_ext` |
| `<vol-id>` | 云盘卷挂载点标识（每台机器不同） | `/vol02/`**`<vol-id>`**`/music` |
| `<lib-guid>` | 曲库 `shared_library.guid` | 从 `music.db` 的 `shared_library` 表查得 |
| `<workspace>` / `<local-home>` | 构建时所用的开发机路径 | 与运行无关，仅供追溯 |
| `<nas-ip>` | NAS 内网地址 | `192.168.x.x` |

脚本已尽量改成环境变量驱动，可用这些变量覆盖：

```bash
export FNMUSIC_HOME=/home/<user>/fnmusic_ext          # 插件安装目录
export FNMUSIC_LIBRARY_DIR=/vol02/<vol-id>/music      # 曲库目录
export FNMUSIC_SOCK=/var/run/trim_music.socket        # 接管后的 UDS
export FNMUSIC_MUSIC_DB=/usr/local/apps/@appdata/trim.music/db/music.db
```

## 脚本清单

### 增量构建器（v49 → v54，逐个串起来）

| 脚本 | 作用 |
|---|---|
| `_build_v50.py` | v49 → v50：只对收藏的在线曲目落盘 |
| `_build_v51.py` | v50 → v51：修陈旧 `.ref` 把文件写回已不存在的旧曲库 |
| `_build_v52.py` | v51 → v52：落盘/删除后主动触发飞牛扫库 |
| `_build_v53.py` | v52 → v53：歌词归属 + 孤儿歌词自愈 |
| `_build_v54.py` | v53 → v54：歌词「跟着音频走」（修 v53 回归） |

每个构建器都是**幂等的字符串替换 + 断言自检 + `compile()` 语法校验**，
在文件尾部的 `checks` 字典里断言关键符号出现次数，并含**回归守卫**——
例如 `_build_v54.py` 会断言 `lyric_cache_path()` 里「音频已在曲库」必须排在「已有歌词」之前，
以及旧的短路条件 `if text == read_lyric_cache(guid):` 必须已消失。

> 用法：改好路径常量后直接 `python _build_v54.py`。它会输出字数与 `OK ... self-check passed`。

### 单元验收（沙箱，不碰真机状态）

| 脚本 | 项数 | 覆盖 |
|---|---|---|
| `_v51_check.py` | 29 | 收藏集合、删除安全边界、陈旧 `.ref` 目录一致性 |
| `_v52_check.py` | 73 | = v51 全部 + 鉴权头识别/TTL、待办合并/兜底、探针不泄漏 cookie |
| `_v53_check.py` | 117 | = v52 全部 + 歌词路由/映射/自愈安全边界 |
| `_v54_check.py` | **163** | = v53 全部 + 歌词落点回归守卫、存量提升、越界安全边界 |
| `_v47_check.py` | — | 播放历史「移除在线曲目」参数修复（先备份 JSON，测完还原） |

配套 `_v5X_check.sh` 负责搭沙箱：造 `/tmp/v5Xt/proxy`（真实的 `recommend.py` /
`cache_gc.py` 兄弟文件 + 假的 `music.db`），并在 `FNMUSIC_HOME` 指向沙箱的前提下运行。

```bash
# 需要插件自带的虚拟环境（由 install.sh 创建）
bash _v54_check.sh
```

### 真机端到端

| 脚本 | 作用 |
|---|---|
| `_v54_e2e.py` | ★ 复现「收藏后歌词必须贴身」：先播一次造出缓存副本 → 收藏 → 断言音频与歌词**同词干** → 清理并还原状态 |
| `_v52_e2e_favdl.sh` / `_v52_e2e_unfav.sh` | 下载侧 / 删除侧触发扫库 |
| `_v52_e2e_close.sh` / `_v52_final_state.sh` | 收尾与状态核对 |
| `_v54_recon.py` | 前置侦查：token 表结构、在线历史模式、当前收藏 |

`_v54_e2e.py` 用 `music.db` 里 `user_token` 表的**真实未过期 token** 调真实收藏接口
（`cookie: music-token=<token>`），因此会**临时收藏/取消收藏一首歌**——
脚本末尾会自动清理并核对「收藏列表恢复原样」，跑之前请确认可以接受这一点。

### 结构对比

| 脚本 | 作用 |
|---|---|
| `_diff_upstream.py` | 对比上游与本分支的 `proxy/app.py`：规模、新增/移除的函数、路由、配置项。用于**复现** `DIFFERENCES.md` 里的量化表 |

```bash
python _diff_upstream.py --upstream /path/to/upstream/proxy/app.py
# 预期输出：移除函数 0 个、移除路由 0 个（纯增量补丁）
```

## 注意

- 这些脚本假设运行在 **fnOS 上、以 root 权限**（`cache/*.ref` 归 root 所有）。
- 沙箱脚本一律把 `FNMUSIC_HOME` 指向 `/tmp/v5Xt/home`，**绝不会写真机的 `cache/`**。
- 涉及删除的脚本都带 `--dry-run` 或先备份，且只允许操作曲库目录 / 缓存目录内的文件。
