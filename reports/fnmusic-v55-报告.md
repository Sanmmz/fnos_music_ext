# fnmusic-ext v55 变更与验收报告

**时间**：2026-09-18 19:05
**线上文件**：`~/fnmusic_ext/proxy/app.py`
**sha1**：`3d9a65a3f92ebdc808442569fcc33204c58b85ca`　**体积**：233256 B（5849 行）
**备份**：`app.py.bak.v54`（v54，210133 B，sha1 `2140acc7…`）
**健康检查**：`{"ok":true,"version":"1.6.0",...,"degraded":false,"failures":[]}`　服务 `active`

---

## 一、问题

> 「搜索歌曲能否优先将有海报并且高质量音源排在前面显示」

现象：搜索任意关键词，返回的在线结果**几乎全是「没有海报 + 标称 mp3」**，而且顺序是
「哪个音源先返回就排前面」，与「有没有封面、音质好不好」完全无关。

真机取证（v54 状态，`周杰伦` / `稻香`）：

```
0 [在线] 稻香(治愈版)      ext=mp3  cover=-   online:netEase
1 [在线] 稻香             ext=mp3  cover=-   online:lx:wy
...
在线 30 条：有海报 3，无损(ext) 0        ← 首屏基本没有封面、没有真无损
```

搜索引擎入口本身没有「质量」维度：三音源（musicbox / musicdl / lxmusic）各自返回结果，
按**任务创建顺序**拼接，谁先返回谁靠前。

---

## 二、根因

三个独立原因叠在一起，缺一不可。

### 2.1 在线条目天生没有封面 URL

| 音源 | 搜索响应里有没有封面 | 说明 |
|---|---|---|
| `lx`（洛雪，占结果大多数） | **恒为空** | 免登录搜索只回 `title/artist/album/duration/quality`，`cover_url` 一律空串 |
| `musicbox`（免登录网易云） | 有，但结果极少 | 需逐首再请求 `songs/detail` |
| `musicdl`（咪咕/酷我） | 有 | 但真机上 3.0s 内**必超时**，返回 0 条 |

于是「有没有海报」在搜索阶段根本无从判断 —— 只能靠**补全**。

与此同时，搜索页**没有接封面预热**：只有「每日推荐」做了 `_prefetch_online_covers`。
所以即便条目能拿到封面 URL，App 首次拉 `/static/cover/<guid>` 也要现问 lx 容器
（约 4 秒/张）→ 手机端超时 → 只渲染出占位图。

### 2.2 音质是「伪造」的

`build_online_track()` 里的音质是按扩展名硬编的：

```python
"bitrate": 1411000 if play_format in ("flac", "wav", "ape", "wv") else 320000,
```

而在线条目的 `ext` 又几乎恒为 `mp3`（音源默认值），所以**任何一首歌都显示 320000** ——
既不是真实码率，也无法用来排序。

### 2.3 首屏页码「钉」在重排之前（最隐蔽的一层）

v55 先在聚合流程末尾做「补全 + 重排」，再让分页按顺序取。但真机实测：

```
netease_wait_s = 3.0s   ← search_track 等聚合的预算
聚合真正结束      ≈ 3.0s + 几十~几百 ms（还要跑补全）
```

`search_track` 在 3.0s 到点、且 `entry["items"]` 已非空时就**直接分配页码**并按 guid 固定下来。
等聚合把顺序排好时，`pages[1]` 里那 30 个 guid 已经是「补全前的原始顺序」——
**之后无论怎么重排都改不了首屏**，表现就是 `[searchrank]` 探针为空、排序等于没做。

---

## 三、改动清单

改动**只涉及 `proxy/app.py`**，全部走「增量构建器 + 自校验断言」：

- [`patches/_build_v55.py`](patches/_build_v55.py)：以 v54（sha1 `2140acc7…`）为基线增量生成 v55，
  每一步都带 `src.count()` 锚点断言 + 结尾 `compile()` 语法校验 + 回归守卫。

### 3.1 新增配置（`.env`，全部有默认值，不配即生效）

| 键 | 默认 | 含义 |
|---|---|---|
| `FNMUSIC_SEARCH_RANK` | `cover_quality` | 排序策略：`cover_quality`（海报优先）/ `quality_cover`（音质优先）/ `off` |
| `FNMUSIC_SEARCH_ENRICH` | `true` | 是否补全搜索结果的海报与音质档 |
| `FNMUSIC_SEARCH_ENRICH_LIMIT` | `30` | 只补第一屏条数（控制首屏等待预算） |
| `FNMUSIC_SEARCH_ENRICH_WAIT_S` | `1.5` | 聚合末尾等补全的上限 |
| `FNMUSIC_SEARCH_RANK_WAIT_S` | `1.5` | 首屏等「补全 + 重排」落定的上限 |

### 3.2 补全层：一次批量请求拿到「封面 + 真音质」

| 来源 | 接口 | 实测 | 拿到什么 |
|---|---|---|---|
| 网易云（覆盖 `lx→wy` 与 `netease` 两个源） | `POST music.163.com/api/v3/song/detail?c=[{"id":N},…]` | **1 次请求 / 25 首 / 0.171s** | `al.picUrl`（25/25 有封面）+ `sq/hr/h/m/l` 音质对象（sq=20/25 无损，h.br=320000 全有） |
| 酷我（`lx→kw`） | `wapi.kuwo.cn/api/www/music/musicInfo?mid=<rid>` | 0.11s/首，`Semaphore(6)` 并发限流 | `pic`/`albumpic` + `hasLossless` |

新增 `_enrich_search_items()`：

1. 先用 `meta_cache.json` 回填（进程重启后依旧有效，**0 请求**）；
2. 网易云走**一次**批量详情（`_netease_detail_bulk`，带正/负缓存：服务端没有的 id 只问一次）；
3. 酷我走单曲详情（`_kw_poster_quality`）；
4. 结果写回条目 + `meta_cache.json`（`cover_url` / `quality_rank` / `lossless` / `no_cover`）。

补全**只处理前 30 条**（用户第一屏），且失败永不抛错 —— 补不上就是不补。

### 3.3 排序层：稳定重排，只动在线块

新增 `rank_search_items()`：稳定排序（同档保持原始源顺序，`idx` 兜底 ⇒ 完全稳定），
排序键 `(-有海报, -音质档, idx)`。

音质档 `_search_item_quality_rank()`（3 最高）：

```
3 = 无损扩展名（flac/wav/ape/wv/aiff/alac/dsf/dff/tta/tak）或码率 ≥ 900k
2 = 高码率有损扩展名（m4a/aac/opus/ogg/mp4）或码率 ≥ 256k
1 = 其它有损（mp3…）
0 = 未知
```

**关键细节**：档位取「扩展名推导」与「补全结果」的**较大者**。
只信补全会出现自相矛盾 —— 真机踩到 `ext=aac` 的曲目被网易云的 `l` 档标成 1，
于是「列表显示 AAC 却排在 mp3 后面」。取 max 只影响排序、不改 `ext`，对取流零风险。

「有海报」判据 `_search_item_has_poster()` 只认「条目 / meta 里的封面 URL」，
**刻意不看磁盘封面缓存** —— 详见 3.5。

### 3.4 首屏次序：四层保证「排序一定作用在第一屏」

针对 2.3 的竞态，四层一起上：

1. **聚合内边收边排**：每收到一批源结果就 `deduplicate → rank_search_items → _resync_published_pages`，
   即使 `netease_wait_s` 到点时聚合还没跑完，第一页分配的也已经是排好序的顺序；
2. **首屏多等一小会儿**：`search_rank_wait_s`（默认 1.5s），让「补全 + 重排」在分配页码之前完成；
3. **分页按 guid 去重分配**（替代原来的下标切片）：池子重排后仍「永远取最好的剩余条目」，
   且不跨页重复；
4. **已发布页重排后对齐**（`_resync_published_pages()`）：只换序、不增删，
   任何一次下拉刷新即为排好序的顺序。

### 3.5 确定性：排序绝不依赖「缓存预热进度」

`_search_item_has_poster()` 最初把「磁盘已有真实封面」也算作有海报。真机踩坑：
**同一关键词连搜两次顺序不一致** —— 因为 `_prefetch_online_covers` 是异步的，
封面在搜索之间陆续落盘，「有海报」判定随之翻转，顺序就跟着抖。

现在排序判据只认「条目 / meta 里的封面 URL」（= `build_online_track` 真正返回给 App 的 coverUrl），
磁盘预热进度**不参与排序**。旧辅助函数 `_has_cached_poster()` 已删除。

### 3.6 封面预热 + 封面兜底

- `search_track()` 返回前调 `_prefetch_online_covers()`（与每日推荐同款做法）；
- `build_online_track()` 的 `cover_url` 在为空时兜底读 `meta_cache`。

### 3.7 可观测性探针（写 `access_probe.log`）

```
[searchrank]  n=71 poster(top30) 30->30 lossless(top10)=10 moved=0 head=稻香(治愈版)
[poolrank]    kw=稻香 13:稻香(治愈版) 13:《稻香》童声 …          ← 池子真实档位（前 14）
[pagealloc]   kw=稻香 p=1 n=30 13:稻香(治愈版) …                ← 首屏真实档位（前 12）
[searchenrich] cap=30 pending=9 ne=0 kw=1 resolved=22
```

格式 `<有海报><音质档>:<标题前 8 字>`，可直接肉眼核验「排序是否真的单调」。

---

## 四、验收

### 4.1 单元：**188 / 188 PASS**（`_v55_check.py`）

= v54 的 163 项全部保留 + v55 新增 25 项：

| 分组 | 覆盖 |
|---|---|
| 配置 | 5 个新键默认值、`_search_rank_enabled()`（含 off） |
| 音质档 | `_ext_quality_rank` 全扩展名；`_search_item_quality_rank` 含「ext 与补全取 max」6 组新用例 |
| 封面判据 | 条目 URL / meta URL / **磁盘预热不算** / 占位图 / 非 online guid / 结果恒定 |
| 排序 | cover_quality / quality_cover / off / 稳定性 / 不改原列表 / 空值降级 |
| 网易云详情 | sq·hr·h·m·l·b 解析；一次批量请求；缓存命中不再请求；负缓存；异常降级 |
| 酷我详情 | 封面 + `hasLossless`（bool 与 `"1"`）；查不到不写负缓存 |
| 补全 | wy+kw 回填、幂等（第二次 0 请求）、meta 回填、cap、开关、失败降级 |
| **分页重排免疫** | guid 去重分配、`taken` 计数、已发布页对齐（集合不变只换序）、不跨页重复、幂等 |
| 端到端 | 25 首 wy 全无封面 → 补全后 25/25 有封面、20/25 无损、top10 全「有封面 + 无损」 |
| 源码守卫 | 排序必须在补全之后、必须在分配之前、只作用于在线块、本地结果仍在最前、v50~v54 补丁均在 |

### 4.2 真机端到端：**4 / 4 轮全绿**（`_v55_e2e.py`）

冷启动（重启服务后）+ 3 轮温跑，`FAILS = 0`。

```
=== 2) 搜索 '周杰伦' ===
  0 [在线] 山歌好比春江水.多谢了(Live)   ext=flac  cover=Y   online:netease
  1 [在线] 结尾曲-友谊地久天长(Live)    ext=flac  cover=Y   online:netease
  2 [在线] 周杰伦《烟花易冷》…           ext=flac  cover=Y   online:netease
  3 [在线] 屋顶                      ext=flac  cover=Y   online:lx:wy
  ...
 10 [在线] 夜曲 (mp3.2)              ext=aac   cover=Y   online:lx:kw
 11 [在线] 烟花易冷 (片段)              ext=aac   cover=Y   online:lx:kw
 12 [在线] 花海 (片段)                ext=aac   cover=Y   online:lx:kw
 13 [在线] 周杰伦"反方向的钟"hip-hop beat  ext=mp3   cover=Y   online:netease
  在线 30 条：有海报 29，无损(ext) 10
```

`_v55_e2e.py` 的核验维度分两层（这一层划分很关键）：

- **响应层**（能证明的）：封面单调不增、无海报全在最后、top10 全有海报、有海报占比过半、
  本地曲库结果仍在最前、封面接口 200 且是真实图片（非 1×1 占位图）、多次请求顺序稳定；
- **探针层**（真实音质档）：解析 `[poolrank]` / `[pagealloc]` 探针，断言
  **池子与首屏的 `(有海报, 音质档)` 都单调不增**。

> ⚠️ 响应里的 `audioSpec.bitrate` 是**展示用固定值**（无损 1411000 / 其余 320000），
> 不是真实音质档。第一版 e2e 用它推音质，把非无损条目全抬到 2 档，得到过**假失败**；
> 现在音质维度一律以探针为准。

### 4.3 关键指标（before → after）

| 指标 | v54 | v55 |
|---|---|---|
| 首屏有海报占比 | 约 3/30 | **21~30 / 30（top10 恒为 100%）** |
| 首屏前 10 位 | 全部 `ext=mp3`、无封面 | **全部 `ext=flac` + 有封面** |
| 搜索页封面接口 `/static/cover` | 现抓 ~4s → 手机端超时 → 占位图 | **0.001~0.002s**（已落盘缓存） |
| 补全成本 | — | 网易云 **1 次请求 / 25 首 / 0.171s**；酷我 0.11s/首（并发 6） |
| 首屏耗时 | 3.0s（netease_wait_s） | 3.0~4.5s（**预算未变**，补全与音源等待重叠） |
| 顺序稳定性 | — | 同一关键词重复搜索顺序一致（冷启动 4/4 轮验证） |
| 排序是否作用到首屏 | ❌ `[searchrank]` 探针为空 | ✅ `poster(top30) 15->30`、`moved` 可观测 |

---

## 五、现状与残留

1. **音质档语义**：`quality_rank` 表示「该曲目在该音源上的**最佳可用**档位」，
   不等于本次实际取流的码率（实际取流由播放时 lx 解析决定）。
   排序用它，语义上足够（更好的源就在那里）。
2. **`audioSpec.bitrate` 仍是展示用固定值**，本次**未改动**，以免影响 App 未知的渲染逻辑。
   如需让 UI 真的显示码率，是独立的一件事。
3. **首屏 3~4.5s**：`netease_wait_s` 等预算未变；补全与音源等待重叠，没有额外叠加等待。
4. **部分结果 TTL=30s**（v54 既有行为）：`partial=True` 时 30 秒后会再聚合一次，
   期间顺序可能微调；聚合完整后 TTL 回到 300s，顺序即固定。
5. `musicdl`（咪咕/酷我）在真机上 3.0s 内仍会超时返回 0，属于既有限制，本次未动。

---

## 六、回滚

v55 **只改了 `proxy/app.py` 一个文件**，回滚 = 覆盖回备份 + 重启：

```bash
D=~/fnmusic_ext/proxy
sudo cp -f "$D/app.py.bak.v54" "$D/app.py"     # 回到 v54（无任何 v55 代码）
sudo systemctl restart fnmusic-ext
curl -s --unix-socket /var/run/trim_music.socket http://localhost/_ext/healthz?deep=1
```

若只想关掉排序而不回滚：

```bash
# .env 中：
FNMUSIC_SEARCH_RANK=off        # 只关排序，保留补全（封面/音质照常补齐）
FNMUSIC_SEARCH_ENRICH=false    # 连补全一起关（回到 v54 的搜索行为）
```

备份链保留：`app.py.bak.v54`（= v55 的上一个版本，210133 B，sha1 `2140acc7…`）。
