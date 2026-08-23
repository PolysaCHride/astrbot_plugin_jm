"""scan 模块单元测试."""

import os
import time
from pathlib import Path
from types import SimpleNamespace

from jm_plugin.config import ConfigService
from jm_plugin.scan import PhotoScanner


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


def _write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"img")


def test_scan_cached_photos_split(tmp_path):
    base = tmp_path / "downloads"
    svc = ConfigService("test_jm", {"custom_data_dir": str(tmp_path / "data")})
    scanner = PhotoScanner(svc)
    option = _FakeOption(base)

    p1 = SimpleNamespace(photo_id="100", title="ch1")
    p2 = SimpleNamespace(photo_id="200", title="ch2")
    album = SimpleNamespace(title="album")

    _write_image(base / "100" / "10.jpg")
    _write_image(base / "100" / "2.jpg")
    _write_image(base / "100" / "1.jpg")

    cached, missing, images = scanner.scan_cached_photos(option, album, [p1, p2])
    assert cached == [p1]
    assert missing == [p2]
    assert [p.name for p in images] == ["1.jpg", "2.jpg", "10.jpg"]


def test_photo_save_dir_album_none_returns_none(tmp_path):
    svc = ConfigService("test_jm", {"custom_data_dir": str(tmp_path / "data")})
    scanner = PhotoScanner(svc)
    option = _FakeOption(tmp_path)
    photo = SimpleNamespace(photo_id="100")
    assert scanner.photo_save_dir(option, None, photo) is None


def test_fallback_scan_mtime_and_hints(tmp_path):
    svc = ConfigService("test_jm", {"custom_data_dir": str(tmp_path / "data")})
    scanner = PhotoScanner(svc)
    root = tmp_path / "dl"
    new_img = root / "本子A" / "1.jpg"
    old_img = root / "本子B" / "1.jpg"
    _write_image(new_img)
    _write_image(old_img)

    now = time.time()
    os.utime(new_img, (now, now))
    os.utime(old_img, (now - 3600, now - 3600))

    assert len(scanner.fallback_scan(root, None, None)) == 2
    recent = scanner.fallback_scan(root, now - 600, None)
    assert recent == [new_img]
    hinted = scanner.fallback_scan(root, None, ["本子A"])
    assert hinted == [new_img]
    assert len(scanner.fallback_scan(root, None, ["不存在"])) == 2


def test_merge_images_dedupe(tmp_path):
    svc = ConfigService("test_jm", {"custom_data_dir": str(tmp_path / "data")})
    scanner = PhotoScanner(svc)
    a, b = tmp_path / "a1.jpg", tmp_path / "a2.jpg"
    _write_image(a)
    _write_image(b)
    merged = scanner.merge_images([a, b], [a])
    assert merged == [a, b]
