"""下载目录扫描与缓存核对。

- photo_save_dir: 复用 jmcomic dir_rule 计算章节真实落盘目录 (含 Docker 宿主机映射候选)
- scan_cached_photos: 逐章节核对下载目录, 区分已缓存与缺失章节
- fallback_scan: mtime 兜底扫描 (精确目录扫描失败时用)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Optional

from .utils import is_image, path_sort_key, unique_paths

try:
    from astrbot.api import logger as _astrbot_logger
except ImportError:
    _astrbot_logger = None


def _logger() -> Any:
    if _astrbot_logger is not None:
        return _astrbot_logger
    return logging.getLogger("astrbot_plugin_jm")


class PhotoScanner:
    """下载目录扫描与缓存核对。"""

    def __init__(self, config_service, logger: Any = None) -> None:
        self.config_service = config_service
        self.logger = logger if logger is not None else _logger()

    # ---------------------------------------------------------------- #
    # 章节落盘目录计算
    # ---------------------------------------------------------------- #
    def photo_save_dir(self, option: Any, album: Any, photo: Any) -> Optional[Path]:
        """按当前 dir_rule 计算某章节的落盘目录。

        直接复用 jmcomic 的 DirRule.decide_image_save_dir, 保证与实际落盘
        一致 (无论 dir_rule 配成 Atitle / Aid / Ptitle / ... )。

        album 可为 None (单章节场景): 此时若 dir_rule 含 A* 规则会抛异常,
        这里捕获后返回 None, 由调用方回退到兜底扫描。
        另外把 Docker 宿主机/容器映射路径作为候选, 容器内扫描不到时
        可以在宿主机路径上命中。
        """
        try:
            save_dir = option.dir_rule.decide_image_save_dir(album, photo)
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"[JM] 计算章节保存目录失败 (photo={getattr(photo, 'photo_id', '?')}): {e}")
            return None
        if not save_dir:
            return None
        primary = Path(str(save_dir))
        for cand in unique_paths([primary, *self.config_service.docker_host_mapped_paths(primary)]):
            if cand.exists():
                return cand
        # 目录尚未创建 (章节未下载), 返回 primary 让调用方据此判定缺失
        return primary

    def scan_photo_dir_images(self, dir_path: Optional[Path]) -> list[Path]:
        """扫描章节目录下的全部图片, 按自然序返回; 缺失/无图片返回 []."""
        if dir_path is None or not dir_path.exists():
            return []
        images = [
            p for p in dir_path.rglob("*")
            if p.is_file() and is_image(p)
        ]
        return sorted(images, key=path_sort_key)

    # ---------------------------------------------------------------- #
    # 缓存核对
    # ---------------------------------------------------------------- #
    def scan_cached_photos(
        self,
        option: Any,
        album: Any,
        photos: list,
    ) -> tuple[list, list, list[Path]]:
        """逐章节核对下载目录。

        :return: (cached_photos, missing_photos, cached_images)
          - cached_photos: 目录存在且含图片的章节对象列表
          - missing_photos: 目录缺失或无图片的章节对象列表
          - cached_images:  已缓存章节的图片路径 (自然序合并), 可直接用于推送
        """
        cached_photos: list = []
        missing_photos: list = []
        cached_images: list[Path] = []
        for ph in photos:
            save_dir = self.photo_save_dir(option, album, ph)
            images = self.scan_photo_dir_images(save_dir)
            if images:
                cached_photos.append(ph)
                cached_images.extend(images)
            else:
                missing_photos.append(ph)
        cached_images = sorted(cached_images, key=path_sort_key)
        return cached_photos, missing_photos, cached_images

    def merge_images(self, *groups: Iterable[Path]) -> list[Path]:
        """合并多个图片分组: 去重 + 自然序排序."""
        merged = [p for group in groups for p in group]
        return sorted(unique_paths(merged), key=path_sort_key)

    # ---------------------------------------------------------------- #
    # mtime 兜底扫描
    # ---------------------------------------------------------------- #
    def fallback_scan(
        self,
        root: Optional[Path],
        since_ts: Optional[float],
        hints: Optional[list[str]] = None,
    ) -> list[Path]:
        """在根目录下全量扫描图片, 可选按 mtime 与标题关键词过滤。

        :param root: 扫描根目录 (不存在返回 [])
        :param since_ts: 非 None 时只收集 mtime >= since_ts 的文件
        :param hints: 目标标题关键词列表, 命中任一关键词的路径优先返回;
                      无命中时回退为全部
        """
        if root is None or not root.exists():
            return []
        found: list[Path] = []
        for p in root.rglob("*"):
            if not p.is_file() or not is_image(p):
                continue
            if since_ts is not None and p.stat().st_mtime < since_ts:
                continue
            found.append(p)
        if hints:
            filtered = [p for p in found if any(hint in str(p) for hint in hints)]
            if filtered:
                found = filtered
        return sorted(found, key=path_sort_key)
