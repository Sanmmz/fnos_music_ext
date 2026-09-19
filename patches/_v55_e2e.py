#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v55 真机端到端：搜索结果「有海报 + 高音质」是否真的排到前面。

1) /_ext/healthz 深度自检
2) 搜索 → 顺序 / 封面 / 音质分布 / 首屏耗时
3) 首条封面走 /static/cover 是否秒回真实图片（而非占位图 / 4 秒超时）
4) 再次搜索 → 顺序稳定、无额外请求（幂等）
5) 含本地曲库命中的关键词 → 本地结果仍在最前
6) [searchrank] 探针
"""
import json
import os
import re
import subprocess
import sqlite3
import time
from collections import Counter
from urllib.parse import quote

SOCK = "/var/run/trim_music.socket"
DB = "file:/usr/local/apps/@appdata/trim.music/db/music.db?mode=ro"
PROBE = "/home/sanmmz/fnmusic_ext/access_probe.log"
COVER_DIR = "/home/sanmmz/fnmusic_ext/cover_cache"

con = sqlite3.connect(DB, uri=True)
TOK = (con.execute("select token from user_token limit 1").fetchone() or [""])[0]
con.close()

fails = []


def ck(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  >> " + str(extra)) if not cond else ""))
    if not cond:
        fails.append(name)


def call(path, method="GET", timeout=60, extra=None):
    cmd = ["curl", "-s", "--unix-socket", SOCK, "-m", str(timeout),
           "-w", "\n__HTTP__%{http_code}__CT__%{content_type}__SZ__%{size_download}__T__%{time_total}"]
    if extra:
        cmd += extra
    if TOK:
        cmd += ["-H", "Authorization: " + TOK]
    cmd += ["http://localhost" + path]
    out = subprocess.run(cmd, capture_output=True)
    raw = out.stdout.decode("utf-8", "replace")
    m = re.search(r"\n__HTTP__(\d+)__CT__(.*?)__SZ__(\d+)__T__([\d.]+)$", raw)
    if not m:
        return "?", "", b"", 0.0, raw
    body = raw[: m.start()].encode("utf-8", "replace")
    return m.group(1), m.group(2), body, float(m.group(4)), None


def raw_bytes(path, timeout=30):
    """拿原始字节（图片用，不能当文本解码）。"""
    cmd = ["curl", "-s", "--unix-socket", SOCK, "-m", str(timeout), "-o", "/tmp/_v55img.bin",
           "-w", "%{http_code} %{content_type} %{size_download} %{time_total}"]
    cmd += ["-H", "Authorization: " + TOK, "http://localhost" + path]
    out = subprocess.run(cmd, capture_output=True, text=True)
    parts = (out.stdout or "").split()
    code, ct, size, t = (parts + ["", "", "0", "0"])[:4]
    with open("/tmp/_v55img.bin", "rb") as f:
        body = f.read()
    return code, ct, int(size or 0), float(t or 0), body


def search(kw, size=50):
    return call("/music/api/v1/search/track?q=" + quote(kw) + "&page=1&size=%d" % size)


def _guids(kw):
    """当前首屏的 guid 序列（不变则说明聚合 + 补全 + 重排已落定）。"""
    code, _, body, _, _ = search(kw)
    if code != "200":
        return []
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return []
    return [str(x.get("guid")) for x in (((obj.get("data") or {}).get("list")) or [])]


def _await_settled(kw, timeout=75.0):
    """轮询直到连续 3 次 guid 序列完全一致，返回该序列。"""
    prev = None
    same = 0
    cur = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = _guids(kw)
        if cur and cur == prev:
            same += 1
            if same >= 2:
                return cur
        else:
            same = 0
        prev = cur
        time.sleep(1.5)
    return cur


print("=" * 74)
print("=== 1) /_ext/healthz 深度自检 ===")
code, ct, body, t, _ = call("/_ext/healthz?deep=1", timeout=40)
print("  http=%s %.2fs" % (code, t))
try:
    h = json.loads(body.decode("utf-8", "replace"))
    print("  ", json.dumps(h, ensure_ascii=False)[:600])
except Exception as e:
    print("  解析失败", e, body[:200])
    h = {}
ck("healthz 200", code == "200", code)

def _rank_key(x):
    """仅用于打印参考：(有海报, 扩展名档位)。**真实音质档以第 6 节探针为准。**"""
    cov = 1 if str(x.get("cover_url") or "") else 0
    e = str(x.get("ext") or "").lower().lstrip(".")
    if e in ("flac", "wav", "ape", "aiff", "alac", "dsf", "dff", "tta", "tak", "wv"):
        q = 3
    elif e in ("m4a", "aac", "opus", "ogg", "mp4"):
        q = 2
    elif e:
        q = 1
    else:
        q = 0
    return (cov, q)


print()
print("=" * 74)
for kw in ("周杰伦", "稻香"):
    print("=== 2) 搜索 %r ===" % kw)
    t0 = time.time()
    code, ct, body, elapsed, _ = search(kw)
    print("  http=%s  首屏耗时 %.2fs  bytes=%d" % (code, elapsed, len(body)))
    if code != "200":
        ck("%s 搜索成功" % kw, False, code)
        continue
    obj = json.loads(body.decode("utf-8", "replace"))
    lst = ((obj.get("data") or {}).get("list")) or []
    online = [x for x in lst if str(x.get("guid") or "").startswith("online:")]
    local = [x for x in lst if not str(x.get("guid") or "").startswith("online:")]
    total = (obj.get("data") or {}).get("total")
    print("  list=%d（本地 %d / 在线 %d） total=%s" % (len(lst), len(local), len(online), total))
    ck("%s 首屏耗时 < 8s" % kw, elapsed < 8.0, "%.2fs" % elapsed)
    ck("%s 有在线结果" % kw, len(online) > 0, len(online))

    def _pref(g):
        p = str(g).split(":")
        return "online:lx:" + p[2] if len(p) >= 4 and p[1] == "lx" else "online:" + (p[1] if len(p) > 1 else "?")
    print("  源分布:", dict(Counter(_pref(x.get("guid")) for x in online)))

    print("  --- 最终展示顺序（前 15）---")
    for i, x in enumerate(lst[:15]):
        cov = str(x.get("cover_url") or "")
        tag = "本地" if not str(x.get("guid") or "").startswith("online:") else "在线"
        print("   %2d [%s] %-28s ext=%-5s cover=%-3s %s" % (
            i, tag, str(x.get("title"))[:26], str(x.get("ext") or "-"),
            "Y" if cov else "-", _pref(x.get("guid"))))

    if online:
        # ★ 响应体里只有「有没有封面」是可核验的：audioSpec.bitrate 是展示用的
        # 固定值（无损 1411000 / 其余 320000），**不是**真实音质档，用它推音质会
        # 把所有非无损条目都抬到 2 档，得出假结论（旧版踩过这个坑）。
        # 真实音质档由 proxy 内部探针在第 6 节核验。
        cov_bits = [1 if str(x.get("cover_url") or "") else 0 for x in online]
        cov_n = sum(cov_bits)
        ll_n = sum(1 for x in online if str(x.get("ext") or "").lower().lstrip(".")
                   in ("flac", "wav", "ape", "aiff", "alac", "dsf", "dff", "tta", "tak", "wv"))
        print("  在线 %d 条：有海报 %d，无损(ext) %d" % (len(online), cov_n, ll_n))
        ck("%s 封面维度单调不增（有海报的都在前）" % kw,
           all(cov_bits[i] >= cov_bits[i + 1] for i in range(len(cov_bits) - 1)), cov_bits[:20])
        ck("%s 无海报条目全部排在最后" % kw,
           cov_bits == sorted(cov_bits, reverse=True), cov_bits[:20])
        ck("%s top10 全部有海报" % kw, all(b == 1 for b in cov_bits[:10]), cov_bits[:10])
        ck("%s 有海报条目占比过半" % kw, cov_n * 2 >= len(online), "%d/%d" % (cov_n, len(online)))
        print("  无损条目占比 %.0f%%（真实音质档排序见第 6 节探针）" % (100.0 * ll_n / len(online)))

        # --- 封面接口：首条 guid 是否秒回真实图片 ---
        print("=== 3) 首条封面 /static/cover ===")
        g = str(online[0].get("guid"))
        c2, ct2, sz, tt, b = raw_bytes("/music/api/v1/static/cover/" + quote(g, safe="") + "?size=300")
        is_img = b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n" or b[:4] == b"RIFF"
        # 注意：网易云专辑封面本身就是 PNG（magic 89504e47…），不能用「PNG magic」判占位图。
        # 真正的占位图是 1x1 透明 PNG，体积恒定 <1KB；真实封面动辄几十 KB。
        is_ph = (is_img and sz < 1024) or sz == 0
        print("  guid=%s http=%s ct=%s bytes=%d %.3fs  magic=%s" % (
            g, c2, ct2, sz, tt, b[:8].hex()))
        ck("封面 200", c2 == "200", c2)
        ck("封面是真实图片（非 1x1 占位图）", is_img and not is_ph, (ct2, b[:8].hex(), sz))
        ck("封面响应 < 1.5s", tt < 1.5, "%.3fs" % tt)
        ck("封面已落盘缓存", os.path.exists(os.path.join(
            COVER_DIR, re.sub(r"[^a-zA-Z0-9_.:-]", "_", g)[:120] + ".img")), "")

    # --- 幂等：等聚合/补全/重排彻底落定后，连搜两次比对 ---
    print("=== 4) 再搜一次（幂等 / 顺序稳定）===")
    # 冷启动时聚合最慢 15s（search_timeout）+ 补全，且已发布页会随结果增多而
    # 「行长」并在重排后对齐；另外 partial 结果的 TTL 只有 30s，会触发再聚合。
    # 因此幂等性必须在**结果连续两次完全一致**之后再测，
    # 否则测到的是「聚合还在跑」的中间态，必然误报（旧版踩过这个坑）。
    settled = _await_settled(kw)
    time.sleep(0.8)
    g2 = _guids(kw)
    time.sleep(0.8)
    g3 = _guids(kw)
    ck("%s 聚合落定后顺序稳定" % kw,
       len(settled) > 0 and g2 == g3 == settled,
       (len(settled), len(g2), len(g3), settled[:4], g2[:4], g3[:4]))
    print("  落定条数 %d" % len(settled))
    print()

print("=" * 74)
print("=== 5) 本地曲库命中仍在最前 ===")
code, _, body, el, _ = search("空心")
if code == "200":
    obj = json.loads(body.decode("utf-8", "replace"))
    lst = ((obj.get("data") or {}).get("list")) or []
    print("  顺序:", [(str(x.get("title"))[:12],
                     "本地" if not str(x.get("guid") or "").startswith("online:") else "在线")
                    for x in lst[:8]])
    head_local = all(not str(x.get("guid") or "").startswith("online:") for x in lst[:1]) if lst else False
    # 只要有本地命中，第一条必须是本地
    ck("首条是本地曲库结果（若存在本地命中）", head_local or len(lst) == 0, lst[:2])

print()
print("=" * 74)
print("=== 6) [searchrank] / [searchenrich] 探针 ===")
try:
    all_lines = open(PROBE, encoding="utf-8", errors="replace").readlines()
    lines = [l.rstrip() for l in all_lines if "[searchrank]" in l]
    elines = [l.rstrip() for l in all_lines if "[searchenrich]" in l]
    print("  [searchrank] 最近:")
    for l in lines[-4:]:
        print("   " + l.strip())
    print("  [searchenrich] 最近:")
    for l in elines[-4:]:
        print("   " + l.strip())
    ck("已产生 [searchrank] 探针（排序真的跑了）", len(lines) > 0, len(lines))
    ck("已产生 [searchenrich] 探针", len(elines) > 0, len(elines))
    ck("补全无 FAILED", not any("FAILED" in l for l in elines), [l for l in elines if "FAILED" in l][:2])
    if lines:
        m = re.search(r"poster\(top30\) (\d+)->(\d+) lossless\(top10\)=(\d+)", lines[-1])
        if m:
            ck("重排后 top30 海报数不减少", int(m.group(2)) >= int(m.group(1)), (m.group(1), m.group(2)))
    if elines:
        last = elines[-1]
        print("  [searchenrich] 语义检查 → " + last.strip())
        m2 = re.search(r"cap=(\d+) pending=(\d+) ne=(\d+) kw=(\d+) resolved=(\d+)", last)
        if m2:
            cap, pend, ne, kw, res = (int(x) for x in m2.groups())
            # resolved 里含「从 meta 缓存回填」的条目（这些不在 pending 内），
            # 因此只能用 resolved ≤ cap / pending ≤ cap 作**自洽**判据，
            # 不能断言 resolved ≤ pending，也不能要求「本轮必有空转产出」
            # （二次搜索全命中缓存是正常完成态）—— 旧版两条断言都踩过坑。
            ck("补全探针自洽：resolved ≤ cap 且 pending ≤ cap",
               res <= cap and pend <= cap, (cap, pend, res))
        # 不再断言「近期必有 resolved>0」：全部命中缓存时（pending=0）虽然探针
        # 会记录 resolved，但真正「无待补条目」的轮次也可以 resolved=0，属正常
        # 完成态；补全是否真的生效，由第 2 节的封面断言直接证明。
        best = 0
        for l in elines[-40:]:
            mm = re.search(r"resolved=(\d+)", l)
            if mm:
                best = max(best, int(mm.group(1)))
        print("  近期补全最大 resolved = %d" % best)

    # --- 权威校验：直接读 proxy 内部探针里的「真实档位」顺序 ---
    # 响应体的 audioSpec.bitrate 是展示用的，只有 proxy 探针里的
    # `_search_item_quality_rank` 才是排序真正用的键，才可能证明「排序对了」。
    rank_lines = [l.rstrip() for l in all_lines if "[poolrank]" in l or "[pagealloc]" in l]
    print("  [poolrank]/[pagealloc] 最近:")
    for l in rank_lines[-6:]:
        print("   " + l.strip()[:160])

    def _seq_of(line):
        out = []
        for tok in line.split():
            m = re.match(r"^(\d)(\d):", tok)
            if m:
                out.append((int(m.group(1)), int(m.group(2))))
        return out

    pool_seq, page_seq = {}, {}
    for l in rank_lines:
        if "[poolrank]" in l:
            m = re.search(r"\[poolrank\] kw=(.*?)\s+\d\d:", l)
            if m:
                pool_seq[m.group(1)] = _seq_of(l)
        else:
            m = re.search(r"\[pagealloc\] kw=(.*?)\s+p=(\d+)\s+n=(\d+)\s", l)
            if m and int(m.group(2)) == 1:
                page_seq[m.group(1)] = _seq_of(l)

    ck("已产生 [poolrank] 探针（池子真实档位）", bool(pool_seq), list(pool_seq))
    ck("已产生 [pagealloc] 探针（首屏真实档位）", bool(page_seq), list(page_seq))
    for kw0 in ("周杰伦", "稻香"):
        for label, seqs in (("池子", pool_seq), ("首屏", page_seq)):
            seq = seqs.get(kw0)
            if not seq:
                continue
            mono = all(seq[i] >= seq[i + 1] for i in range(len(seq) - 1))
            ck("%s %s 真实档位 (有海报,音质) 单调不增" % (kw0, label), mono, seq)
except Exception as e:
    ck("读取探针", False, e)

print()
print("=" * 74)
print("FAILS = %d" % len(fails))
for f in fails:
    print("  -", f)
print("=" * 74)
