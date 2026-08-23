"""download 模块编排流程测试 (全部使用 fakes)."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from jm_plugin.client import ClientService
from jm_plugin.config import ConfigService
from jm_plugin.download import DownloadService
from jm_plugin.scan import PhotoScanner


class _FakeClient:
    def __init__(self, album):
        self.album = album

    def get_album_detail(self, aid):
        if aid == self.album.album_id:
            return self.album
        raise ValueError("album not found")

    def get_photo_detail(self, pid, fetch=False):
        for ph in self.album.photos:
            if ph.photo_id == pid:
                return ph
        return None


class _FakeDirRule:
    def __init__(self, base: Path):
        self.base_dir = str(base)

    def decide_image_save_dir(self, album, photo):
        if album is None:
            raise AttributeError("album required")
        return str(Path(self.base_dir) / str(photo.photo_id))


class _FakeOption:
    def __init__(self, base: Path):
        self.dir_rule = _FakeDirRule(base)
        self.base = base
        self.downloaded: list = []

    def new_jm_client(self):
        return _FakeClient(self.album)

    def download_photo(self, photo_id):
        self.downloaded.append(photo_id)
        d = self.base / str(photo_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "1.jpg").write_bytes(b"img1")
        (d / "2.jpg").write_bytes(b"img2")


class _FakeForwarder:
    def __init__(self):
        self.sent: list = []

    async def send(self, platform_id, session_id, is_group, self_id, files, aid, base_dir):
        self.sent.append(
            {
                "files": list(files),
                "aid": aid,
                "session_id": session_id,
            }
        )
        return {"images": len(files), "batches": 1, "transport": "file", "elapsed": 0.0}


class _FakeAlbum:
    """模拟 jmcomic 本子对象 (支持 list(album) 迭代)."""

    def __init__(self):
        self.album_id = "42"
        self.title = "测试本子"
        self.page_count = 2
        self.author: list = []
        self.tags: list = []
        self.views = 0
        self.likes = 0
        self.comment_count = 0
        self.pub_date = "2025-01-01"
        self.update_date = "2025-01-02"
        self.photos = [
            SimpleNamespace(photo_id="100", title="第一章", page_count=2, album_id="42"),
            SimpleNamespace(photo_id="200", title="第二章", page_count=2, album_id="42"),
        ]

    def __iter__(self):
        return iter(self.photos)


def _album():
    return _FakeAlbum()


def _run(album, tmp_path, config=None, selector="all", target="42"):
    cfg = dict(config or {})
    cfg.setdefault("custom_data_dir", str(tmp_path / "data"))
    cfg.setdefault("skip_if_cached", True)
    svc = ConfigService("test_jm", cfg)
    svc.option = _FakeOption(tmp_path / "downloads")
    svc.option.album = album
    clients = ClientService(svc, cfg)
    scanner = PhotoScanner(svc)
    forwarder = _FakeForwarder()
    downloader = DownloadService(svc, clients, scanner, forwarder)
    pushes: list = []

    async def push(text):
        pushes.append(text)

    asyncio.run(
        downloader.run(
            target, selector, "umo:test", "aiocqhttp", "123", True, "10000", push
        )
    )
    return downloader, svc, forwarder, pushes


def test_full_download_flow(tmp_path):
    album = _album()
    _, svc, forwarder, pushes = _run(album, tmp_path)

    # 两章都下载了
    assert sorted(svc.option.downloaded) == ["100", "200"]
    joined = chr(10).join(pushes)
    assert "下载完成" in joined
    assert "正在下载 2 个章节" in joined
    # 推送了 4 张图片 (每章 2 张)
    assert len(forwarder.sent) == 1
    assert len(forwarder.sent[0]["files"]) == 4
    assert forwarder.sent[0]["aid"] == "42"


def test_selector_partial_download(tmp_path):
    album = _album()
    _, svc, forwarder, _ = _run(album, tmp_path, selector="1")
    assert svc.option.downloaded == ["100"]
    assert len(forwarder.sent[0]["files"]) == 2


def test_single_photo_target(tmp_path):
    album = _album()
    _, svc, forwarder, _ = _run(album, tmp_path, target="100")
    assert svc.option.downloaded == ["100"]
    assert len(forwarder.sent[0]["files"]) == 2


def test_max_album_images_rejects(tmp_path):
    album = _album()
    _, svc, forwarder, pushes = _run(album, tmp_path, config={"max_album_images": 3})
    assert svc.option.downloaded == []
    assert forwarder.sent == []
    joined = chr(10).join(pushes)
    assert "已取消下载" in joined


def test_invalid_id_reports_error(tmp_path):
    album = _album()
    _, _, forwarder, pushes = _run(album, tmp_path, target="99999")
    assert forwarder.sent == []
    joined = chr(10).join(pushes)
    assert "不是有效的本子或章节 ID" in joined


def test_cached_photos_skip_download(tmp_path):
    album = _album()
    base = tmp_path / "downloads"
    (base / "100").mkdir(parents=True)
    (base / "100" / "1.jpg").write_bytes(b"img1")
    (base / "100" / "2.jpg").write_bytes(b"img2")
    _, svc, forwarder, pushes = _run(album, tmp_path, selector="1")
    assert svc.option.downloaded == []  # 未触发下载
    joined = chr(10).join(pushes)
    assert "已存在于下载目录" in joined
    assert len(forwarder.sent[0]["files"]) == 2
