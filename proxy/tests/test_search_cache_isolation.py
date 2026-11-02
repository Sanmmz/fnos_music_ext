"""回归：管理台综合搜索缓存不得与主搜索缓存同名。

背景（v1.9.2 / git 5a75c6d）：`proxy/app.py` 里 `_SEARCH_CACHE` 被定义了两次
—— L267 的主搜索缓存（值是 dict）与 L8214 的 v85 综合搜索缓存（值是
`(exp, payload)` 元组）。模块级第二次赋值会把第一个 dict 整个替换掉，于是
管理台搜一次歌单后，播放链路的 `_clean_search_cache()` 对着元组调 `.get()`
→ AttributeError → 所有在线播放返回 599。

修法：把 v85 那套改名为 `_ADMIN_SEARCH_CACHE`，两套缓存彻底隔离。
"""
import importlib

p = importlib.import_module("proxy.app")


def test_admin_search_cache_is_a_separate_object():
    """两套缓存必须是不同的对象，不能同名互相覆盖。"""
    assert p._ADMIN_SEARCH_CACHE is not p._SEARCH_CACHE


def test_admin_search_cache_stores_tuples_without_breaking_playback_cleanup():
    """管理台写入 (exp, payload) 元组后，播放链路的缓存清理不得抛 AttributeError。"""
    p._SEARCH_CACHE.clear()
    p._ADMIN_SEARCH_CACHE.clear()
    try:
        p._search_cache_put(("playlist", "回归用关键词", 20), {"results": [{"id": "x"}]})
        # 关键断言：元组进了 admin 缓存，主缓存仍然是干净的
        assert len(p._ADMIN_SEARCH_CACHE) == 1
        assert len(p._SEARCH_CACHE) == 0

        # 播放链路每次取流前都会清主缓存；修复前这里会因元组 .get() 抛 AttributeError
        p._clean_search_cache()
    finally:
        p._SEARCH_CACHE.clear()
        p._ADMIN_SEARCH_CACHE.clear()


def test_main_search_cache_still_accepts_dict_entries():
    """主搜索缓存仍是 dict 条目（_clean_search_cache 依赖 entry.get(...)）。"""
    p._SEARCH_CACHE.clear()
    p._ADMIN_SEARCH_CACHE.clear()
    try:
        p._SEARCH_CACHE["kw"] = {"items": [], "ts": 0, "accessed": 0}
        p._clean_search_cache()
    finally:
        p._SEARCH_CACHE.clear()
        p._ADMIN_SEARCH_CACHE.clear()
