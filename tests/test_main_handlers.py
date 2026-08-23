"""main.py 命令处理器回归测试 (模拟 AstrBot 命名空间包加载).

直接驱动 async generator 形式的命令处理器, 防止再出现
"await 异步生成器" 之类的错误 (此文件是 /jm tags 修复的回归测试)。
"""

import asyncio
import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_main_module():
    """以命名空间包方式加载 main.py (等价 AstrBot 的 data.plugins.<名>.main)."""
    pkg_name = "jm_main_under_test"
    pkg = type(sys)(pkg_name)
    pkg.__path__ = [str(ROOT)]
    pkg.__file__ = str(ROOT / "main.py")
    sys.modules[pkg_name] = pkg
    spec = importlib.util.spec_from_file_location(
        f"{pkg_name}.main", ROOT / "main.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg_name}.main"] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = None


def _module():
    global _mod
    if _mod is None:
        _mod = _load_main_module()
    return _mod


class _FakeEvent:
    def __init__(self):
        self.stopped = False

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    def stop_event(self):
        self.stopped = True


def _make_plugin(calls):
    """构造最小 JMPlugin 实例: 拦截 _call 与 _send_text."""
    mod = _module()
    plugin = mod.JMPlugin.__new__(mod.JMPlugin)

    async def fake_call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        if method == "get_album_detail":
            return album_of(args[0])
        if method == "get_photo_detail":
            return photo_of(args[0])
        if method == "search_tag":
            return page_of(args[0])
        raise AssertionError(f"unexpected method: {method}")

    plugin._call = fake_call
    plugin._send_text = lambda event, text: text
    plugin.config_service = SimpleNamespace(get_int=lambda k, d: 10)
    return plugin


def _album(id_, title, tags):
    return SimpleNamespace(album_id=id_, title=title, tags=tags)


def _photo(id_, album_id):
    return SimpleNamespace(photo_id=id_, album_id=album_id)


class _FakePage:
    def __init__(self, items):
        self._items = items

    def iter_id_title_tag(self):
        for aid, title, tags in self._items:
            yield aid, title, tags

    def __iter__(self):
        for aid, title, _tags in self._items:
            yield aid, title


_ALBUMS = {
    "213848": _album("213848", "测试本子A", ["中文", "单行本", "暮林あさ美"]),
    "350234": _album("350234", "测试本子B", ["无修正"]),
    "900001": _album("900001", "所属本子", ["杂志", "连载"]),
}
_PHOTOS = {
    "900002": _photo("900002", "900001"),
}
_PAGES = {
    "巨乳": _FakePage([("100001", "本子一", ["巨乳"]), ("100002", "本子二", ["巨乳"])]),
}


def album_of(aid):
    return _ALBUMS.get(aid)


def photo_of(pid):
    return _PHOTOS.get(pid)


def page_of(tag):
    return _PAGES.get(tag)


async def _collect(agen):
    out = []
    async for item in agen:
        out.append(item)
    return out


def test_tags_id_mode_shows_album_tags():
    calls: list = []
    plugin = _make_plugin(calls)
    event = _FakeEvent()
    results = asyncio.run(_collect(plugin.jm_tags(event, "213848")))
    text = chr(10).join(str(r) for r in results)
    assert "的标签 (共 3 个)" in text
    assert "暮林あさ美" in text
    assert event.stopped
    assert any(m == "get_album_detail" for m, _, _ in calls)


def test_tags_id_mode_photo_fallback():
    calls: list = []
    plugin = _make_plugin(calls)
    event = _FakeEvent()
    results = asyncio.run(_collect(plugin.jm_tags(event, "900002")))
    text = chr(10).join(str(r) for r in results)
    assert "所属本子" in text
    assert "的标签 (共 2 个)" in text
    assert any(m == "get_photo_detail" for m, _, _ in calls)


def test_tags_id_mode_not_found():
    calls: list = []
    plugin = _make_plugin(calls)
    event = _FakeEvent()
    results = asyncio.run(_collect(plugin.jm_tags(event, "999999")))
    text = chr(10).join(str(r) for r in results)
    assert "不是有效的本子或章节 ID" in text


def test_tags_name_mode_searches():
    calls: list = []
    plugin = _make_plugin(calls)
    event = _FakeEvent()
    results = asyncio.run(_collect(plugin.jm_tags(event, "巨乳")))
    text = chr(10).join(str(r) for r in results)
    assert "标签 [巨乳] 第 1 页" in text
    assert "[100001] 本子一" in text
    assert any(m == "search_tag" for m, _, _ in calls)


def test_tags_empty_args_shows_usage():
    plugin = _make_plugin([])
    event = _FakeEvent()
    results = asyncio.run(_collect(plugin.jm_tags(event, "")))
    text = chr(10).join(str(r) for r in results)
    assert "用法: /jm tags" in text


@pytest.mark.skipif(sys.version_info < (3, 8), reason="需要命名空间包支持")
def test_namespace_import_module_name():
    assert _module().__name__ == "jm_main_under_test.main"
