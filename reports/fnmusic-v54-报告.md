# fnmusic-ext v54 变更与验收报告

**时间**：2026-09-18 17:30
**线上文件**：`/home/<user>/fnmusic_ext/proxy/app.py`
**sha1**：`2140acc7f6413476632cca16cc26d8673db207b5`　**体积**：210133 B（5342 行）
**备份**：`app.py.bak.v53`（v53，202492 B，sha1 `586d7f88…`）
**健康检查**：`{"ok":true,"version":"1.6.0","upstream":"ok","degraded":false,"failures":[]}`

---

## 一、问题

> 「上一次修改后，造成收藏音乐之后歌曲下载到了本地，但是歌词没有存储到本地」

现象：收藏一首在线歌曲 → 音频整轨下载进云盘曲库 ✅，但曲库里的音频旁边**没有同名 `.lrc`**；
歌词被写进了插件自己的 `cache/` 目录 ❌。

---

## 二、根因（v53 自己引入的回归）

### 真机取证（`access_probe.log` 日志顺序）

```
GET /music/api/v1/lyric/list?trackGUID=online%3Anetease%3A2163210456&lan=zh-CN
    | 200 | 1ms | len=2377                   ← 1ms = 命中本地缓存（歌词早已在 cache/）
[favdl] ok guid=online:netease:2163210456
    dest=/vol02/<vol-id>/music/加木 - 两 难.flac ... lyric=True
[favdel] guid=online:netease:2163210456 removed=4
    /home/<user>/fnmusic_ext/cache/online_netease_2163210456.lrc   ← 歌词在 cache ❌
    /vol02/<vol-id>/music/加木 - 两 难.flac                  ← 音频在曲库 ✅
    /home/<user>/fnmusic_ext/cache/online_netease_2163210456.ref
    /home/<user>/fnmusic_ext/cache/online_netease_2163210456.lyricref
```

`lyric=True` 说明歌词**抓到了**，只是**落点**错了。
对照 `online:netease:1973665667`（收藏前没播放过、cache 里没有歌词副本）：
歌词正确地落在了 `/vol02/.../马也_Crabbit - 海屿你.lrc`。

### 逻辑根因

v53 的 `lyric_cache_path()` 优先级是：

```
已有歌词 → 曲库音频同名 sidecar → 缓存音频同名 sidecar → 本地 cache/
```

而 `find_lyric_file()` 的第 4 档会去 `iter_media_dirs()`（= `[曲库, cache/]`）里找
`<safe-guid>.lrc` ⇒ **命中 `cache/` 里的旧歌词副本**。

于是只要「**收藏之前播过一次**这首歌」（App 拉歌词 → 写进 `cache/<guid>.lrc`），
收藏整轨下载把音频落进曲库之后，歌词仍然写回 `cache/` —— **永远不贴身**。

v53 之前 `find_lyric_file()` 不是第一档，走的是 `.ref` 词干
（音频在曲库 ⇒ 歌词自然贴身），所以没有这个问题。v53 为了修「孤儿歌词」
把「已有歌词优先」提到最前，顺手打破了这个不变式。

---

## 三、改动清单

| # | 改动 | 说明 |
|---|---|---|
| 1 | **`lyric_cache_path()` 选路重排** | **「音频已在曲库」提到最前**，优先于任何缓存副本 ⇒ 歌词跟着音频走。最后一档仍是本地 `cache/`，v53「绝不把无主歌词写进云盘曲库」保持不变 |
| 2 | **`write_lyric_cache()` 短路条件收紧** | 从「**任意位置**内容一致即返回」改成「**目标位置**内容一致才返回」，否则 cache 副本会永远短路成功、歌词无法被提升 |
| 3 | **`_drop_shadow_lyric()`** | 歌词写对位置后，清掉 `cache/` 里同一 guid 的影子副本（只动我方缓存目录） |
| 4 | **`_promote_lyric_file()` / `promote_one_lyric()`** | 单个 guid 的存量自愈：音频在曲库、歌词只在 cache ⇒ 搬成曲库同名 sidecar；曲库已有则只清影子副本 |
| 5 | **`promote_library_lyrics()`** | 批量扫 `cache/*.ref`，把所有错位的歌词一次性补位 |
| 6 | **`_lyric_promote_loop()`** | 启动后 12 秒补一次，之后按与孤儿清扫相同的间隔（默认 1800s）复扫 |
| 7 | **整轨落盘后补位** | `[favdl]` 成功且 `info` 未带歌词时，调 `promote_one_lyric()` —— 正面堵住本次回归 |
| 8 | **歌词读取路径幂等自愈** | `resolve_online_lyric()` 命中缓存时也走一次 `write_lyric_cache()`：目标正确则零副作用，位置错了就纠正 |
| 9 | 探针 | 新增 `[lyricpromo] <path>`、`[lyricpromo] dropped-shadow <src>`、`[lyricshadow] removed <path>` |

新增开关（`.env` 可覆盖）：

```ini
FNMUSIC_LYRIC_PROMOTE='true'      # 歌词「贴身」自愈总开关
```

**自愈的安全边界**（每条都有单元测试）：

- 词干必须来自 `cache/` 下 `.ref` 记过的路径，且必须是**绝对路径**；
- 词干必须落在**曲库目录**内 ⇒ `cache/` 内的歌词不归它管（交给 `purge_rolling`）；
- 该词干下**确实存在音频**（任一扩展名）才动手 ⇒ 没有音频时绝不无中生有；
- 歌词源文件必须是 `cache/` 里我方的 `<safe-guid>.lrc`；
- 搬运走「写 `.part` → `os.replace`」⇒ 不产生半截文件。

---

## 四、验收

### 4.1 单元：**163 / 163 PASS**（`_v54_check.py`）

= v53 全部 117 项 + v54 新增 46 项。新增部分核心断言：

| 断言 | 结果 |
|---|---|
| 音频在曲库 + cache 有旧副本 → 目标**仍是曲库 sidecar** | PASS |
| 写入后 cache 影子副本被清掉 / `.lyricref` 指向曲库 | PASS |
| 无音频 + cache 有副本 → 目标**仍是 cache**（不反向回归到写曲库） | PASS |
| 无音频时曲库不出现「歌手 - 歌名.lrc」 | PASS |
| `promote_one_lyric` 正确提升、内容原样、源文件移走、无 `.part` 残留 | PASS |
| 无曲库音频 → 一根汗毛都不动 | PASS |
| 曲库已有歌词 → 只清影子副本，**不覆盖** | PASS |
| 批量扫：曲库外词干 / 越界 guid **绝不被搬动** | PASS |
| 开关 `lyric_promote=false` → 单个与批量都空操作 | PASS |
| 源码级：`lyric_cache_path` 中「音频在曲库」排在「已有歌词」之前 | PASS |
| 源码级：`write_lyric_cache` 已弃用旧短路条件 | PASS |
| 回归保护：v53 孤儿自愈 / v52 自动扫库 / v51 整轨下载接线全部仍在 | PASS |

沙箱探针实录：

```
[lyricshadow] removed /tmp/v54t/home/cache/online_netease_8200001.lrc
[lyricpromo] /tmp/v54t/lib/自愈丙 - 有音频.lrc
[lyricpromo] dropped-shadow /tmp/v54t/home/cache/online_netease_8200005.lrc
[lyricpromo] /tmp/v54t/lib/批量己 - 有音频.lrc
[lyricpromo] /tmp/v54t/lib/批量庚 - 有音频.lrc
[lyricpromo] /tmp/v54t/lib/开关辛 - 有音频.lrc
```

### 4.2 真机端到端：**全绿**（`_v54_e2e.py`）

严格复现你的操作路径（用真实 `music-token` 调真实收藏接口）：

| 步骤 | 结果 |
|---|---|
| ① 先播一次歌词（制造 cache 副本 = 本次回归的触发条件） | `cache/online_netease_2163210456.lrc` 已生成 ✅ |
| ② 收藏该曲目（`favorite-track/create`）→ 触发整轨下载 | `{"code":0,"msg":"","data":null}` |
| ③ 音频落曲库 | `加木 - 两 难.mp3`（19099195 B，字节数与 `content-length` 相符）✅ |
| ④ ★ **歌词贴身** | `加木 - 两 难.lrc`，**与音频同名词干**、内容非空 ✅ |
| ⑤ cache 影子副本 | 已清掉 ✅ |
| ⑥ 映射 | `.ref` 与 `.lyricref` 均指向 `/vol02/…/music/加木 - 两 难` ✅ |
| ⑦ 取消收藏 | 音频 + 歌词一起删除；收藏列表恢复原样 ✅ |
| ⑧ 你原有的收藏《空心》 | 音频 + 同名歌词**毫发无伤** ✅ |

决定性探针（`lyric=False` ⇒ 走的是 v54 新补位通道）：

```
[favdl] ok guid=online:netease:2163210456
    dest=/vol02/<vol-id>/music/加木 - 两 难.mp3 bytes=19099195 exp=19099195 ext=mp3 lyric=False
[lyricpromo] /vol02/<vol-id>/music/加木 - 两 难.lrc      ← v54 补位生效
[scanreq] call path=/music/api/v1/shared-library/scan guid=<lib-guid>… status=200 ok=True
```

即便在 v53 时代已经下载过的存量歌（音频在曲库、歌词在 cache），
也会被启动后的 `_lyric_promote_loop()` 自动补位。

---

## 五、现状与残留

- 曲库 `/vol02/<vol-id>/music`：`黄霄雲 _ 刘端端 - 空心 (Live版).mp3` + **同名 `.lrc`** —— 干净。
- `cache/` 内 `<guid>.lrc` 属于插件自己的滚动缓存（对应曲目没有音频时，这是**正确**位置）；
  `.lyricref` 也按滚动缓存语义被 `purge_rolling()` 识别（它以 `.ref` 结尾）。
- healthz `degraded=false`；journal 内 v54 部署后**无任何异常**
  （17:20 的两条 `Failed to write` / `Stream startup failed` 在部署前，属客户端中断取流）。

---

## 六、回滚

```bash
sudo cp -f /home/<user>/fnmusic_ext/proxy/app.py.bak.v53 /home/<user>/fnmusic_ext/proxy/app.py
sudo systemctl restart fnmusic-ext
```

（v54 未改数据结构；回滚后「收藏后歌词留在 cache/、不贴身」的行为会回来。）
