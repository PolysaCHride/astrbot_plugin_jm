"""后台下载任务编排。

流程: 解析目标 (本子/章节) -> 缓存核对 -> 图片数上限检查 -> 下载缺失章节
-> 扫描图片 -> 完成提示 -> 合并转发推送。全部在后台任务中执行, 通过
push 回调向来源会话发送进度消息。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .utils import fmt_size, parse_selector

try:
    from astrbot.api import logger as _astrbot_logger
except ImportError:
    _astrbot_logger = None


def _logger() -> Any:
    if _astrbot_logger is not None:
        return _astrbot_logger
    return logging.getLogger("astrbot_plugin_jm")


PushFunc = Callable[[str], Awaitable[Any]]


class DownloadService:
    """下载任务编排 (后台任务入口: run)."""

    def __init__(
        self,
        config_service,
        client_service,
        scanner,
        forwarder,
        logger: Any = None,
    ) -> None:
        self.config_service = config_service
        self.client_service = client_service
        self.scanner = scanner
        self.forwarder = forwarder
        self.logger = logger if logger is not None else _logger()

    # ---------------------------------------------------------------- #
    # 入口
    # ---------------------------------------------------------------- #
    async def run(
        self,
        aid: str,
        selector: str,
        umo: str,
        platform_id: str,
        session_id: str,
        is_group: bool,
        self_id: str,
        push: PushFunc,
    ) -> None:
        """执行完整下载任务, 任何异常都不向事件循环外抛出."""
        try:
            option = await self.client_service.ensure_option()
            client = self.client_service.new_client(option)
            await self.client_service.maybe_login(client)

            album, photos, is_album = await self._resolve_target(client, aid, selector, push)
            if not photos:
                return

            cached, missing, cached_images, scan_album = await self._check_cache(
                option, client, album, photos, is_album
            )

            if not await self._check_image_limit(client, aid, missing, push):
                return

            all_cached = bool(cached) and not missing
            await push(self._progress_text(aid, len(photos), len(cached), len(missing), len(cached_images), all_cached))

            # 下载 (仅缺失章节)
            t0 = time.time()
            if missing:
                await self.client_service.run_blocking(self._download_photos, option, missing)
            elapsed = time.time() - t0

            downloaded, matched_root = await self._collect_images(
                option, scan_album, photos, missing, cached_images, t0
            )

            total_size = sum(p.stat().st_size for p in downloaded)
            await push(
                self._done_text(aid, len(missing), len(downloaded), total_size, elapsed, all_cached)
            )

            if downloaded:
                base = matched_root or self.config_service.download_dir()
                try:
                    stats = await self.forwarder.send(
                        platform_id, session_id, is_group, self_id, downloaded, aid, base
                    )
                    self.logger.info(f"[JM] 图集已推送: {stats}")
                except Exception as e:  # noqa: BLE001
                    self.logger.error(f"[JM] 推送合并转发图集失败: {e}", exc_info=True)
                    await push(
                        f"❌ 推送合并转发图集失败: {e}"
                        + chr(10)
                        + f"图片已保留在: {base}",
                    )
            elif all_cached:
                await push(
                    f"⚠️ 本子 {aid} 判定为已缓存但未找到图片文件, "
                    "请检查下载目录权限或重新下载。",
                )
            else:
                await push(
                    f"⚠️ 未发现本次下载的图片文件, 尝试的目录: {self.config_service.download_dir()}"
                )
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"[JM] 下载任务异常: {e}", exc_info=True)
            try:
                await push(f"❌ 下载任务异常: {e}")
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------------- #
    # 目标解析
    # ---------------------------------------------------------------- #
    async def _resolve_target(
        self, client: Any, aid: str, selector: str, push: PushFunc
    ) -> tuple[Any, list, bool]:
        """先尝试按本子 ID 解析, 失败再按章节 ID 兜底。

        :return: (album, photos, is_album); photos 为空表示已推送错误信息
        """
        album_error: Optional[Exception] = None
        try:
            album = await self.client_service.run_blocking(client.get_album_detail, aid)
            if album is not None:
                photo_list = list(album)
                idx_list = parse_selector(selector, len(photo_list))
                if not idx_list:
                    await push(f"❌ 选择器 {selector} 没有匹配到任何章节")
                    return None, [], False
                return album, [photo_list[i - 1] for i in idx_list], True
        except Exception as e:  # noqa: BLE001
            album_error = e

        try:
            photo = await self.client_service.run_blocking(client.get_photo_detail, aid, False)
            if photo is not None:
                return None, [photo], False
        except Exception:  # noqa: BLE001
            pass

        msg = f"❌ ID {aid} 不是有效的本子或章节 ID"
        if album_error is not None:
            msg += f" ({album_error})"
        await push(msg)
        return None, [], False

    # ---------------------------------------------------------------- #
    # 缓存核对
    # ---------------------------------------------------------------- #
    async def _check_cache(
        self,
        option: Any,
        client: Any,
        album: Any,
        photos: list,
        is_album: bool,
    ) -> tuple[list, list, list[Path], Any]:
        """核对已缓存章节。

        :return: (cached_photos, missing_photos, cached_images, scan_album)
        - scan_album: 用于精确目录扫描的本子对象; 单章节场景下为补查的
          所属本子 (dir_rule 含 A* 规则时计算目录需要它)
        """
        scan_album = album
        cached_photos: list = []
        missing_photos: list = list(photos)
        cached_images: list[Path] = []

        if not self.config_service.get_bool("skip_if_cached", True):
            return cached_photos, missing_photos, cached_images, scan_album

        if is_album:
            try:
                cached_photos, missing_photos, cached_images = self.scanner.scan_cached_photos(
                    option, album, photos
                )
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"[JM] 缓存核对失败, 回退全量下载: {e}")
                missing_photos = list(photos)
            return cached_photos, missing_photos, cached_images, scan_album

        # 单章节: 取所属本子用于精确算目录; 失败则跳过缓存检测走全量下载
        assoc_album: Any = None
        try:
            assoc_album = await self.client_service.run_blocking(
                client.get_album_detail, photos[0].album_id
            )
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"[JM] 单章节缓存核对取所属本子失败, 跳过缓存检测: {e}")
        if assoc_album is not None:
            scan_album = assoc_album
            try:
                cached_photos, missing_photos, cached_images = self.scanner.scan_cached_photos(
                    option, assoc_album, photos
                )
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"[JM] 缓存核对失败, 回退全量下载: {e}")
                missing_photos = list(photos)
        return cached_photos, missing_photos, cached_images, scan_album

    # ---------------------------------------------------------------- #
    # 图片数上限预查
    # ---------------------------------------------------------------- #
    async def _check_image_limit(
        self, client: Any, aid: str, photos: list, push: PushFunc
    ) -> bool:
        """下载前预查总图片数, 超过 max_album_images 直接拒绝 (防误下长篇)."""
        max_images = self.config_service.get_int("max_album_images", 0)
        if not photos or max_images <= 0:
            return True
        try:
            est_total = 0
            for ph in photos:
                cnt = getattr(ph, "page_count", None)
                if not cnt:
                    try:
                        cnt = len(list(ph))
                    except Exception:  # noqa: BLE001
                        det = await self.client_service.run_blocking(
                            client.get_photo_detail, ph.photo_id, False
                        )
                        cnt = len(list(det)) if det else 0
                est_total += int(cnt or 0)
            if est_total > max_images:
                await push(
                    f"❌ 本子 {aid} 需补下 {len(photos)} 章, "
                    f"共约 {est_total} 张图片, 超过上限 {max_images} 张, "
                    f"已取消下载。{chr(10)}"
                    f"如需下载请在插件配置中调大 max_album_images, "
                    f"或用 /jm d <ID> <章节选择器> 分章节下载。",
                )
                return False
        except Exception as e:  # noqa: BLE001
            # 预查失败不阻断主流程
            self.logger.warning(f"[JM] 预查图片数失败, 跳过上限检查: {e}")
        return True

    # ---------------------------------------------------------------- #
    # 下载与扫描
    # ---------------------------------------------------------------- #
    @staticmethod
    def _download_photos(option: Any, photos: list) -> None:
        """逐个下载缺失章节 (jmcomic 内部自带图片级线程池)."""
        for ph in photos:
            option.download_photo(ph.photo_id)

    async def _collect_images(
        self,
        option: Any,
        scan_album: Any,
        photos: list,
        missing: list,
        cached_images: list[Path],
        t0: float,
    ) -> tuple[list[Path], Optional[Path]]:
        """扫描本次任务的图片: 先按精确章节目录, 失败再 mtime 兜底。

        :return: (downloaded, matched_root)
        """
        new_images: list[Path] = []
        if missing:
            try:
                _, _, new_images = self.scanner.scan_cached_photos(option, scan_album, missing)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"[JM] 精确扫描新下载章节失败: {e}")
        downloaded = self.scanner.merge_images(cached_images, new_images)
        matched_root: Optional[Path] = None

        if not downloaded and missing:
            # 精确扫描拿不到又确实下载过 -> mtime 兜底扫描
            primary_root: Optional[Path] = None
            try:
                base_dir_attr = getattr(option.dir_rule, "base_dir", None)
                if base_dir_attr:
                    primary_root = Path(str(base_dir_attr))
            except Exception:  # noqa: BLE001
                primary_root = None
            fallback_root = self.config_service.download_dir()

            hints: list[str] = []
            if scan_album is not None:
                title = str(getattr(scan_album, "title", "") or "").strip()
                if title:
                    hints.append(title)
            for ph in photos:
                title = str(getattr(ph, "title", "") or "").strip()
                if title:
                    hints.append(title)

            mtime_window = t0 - 300  # 5 分钟容差
            roots = self.config_service.scan_roots(primary_root, fallback_root)
            scan_mode = "none"
            for recent_only in (True, False):
                since = mtime_window if recent_only else None
                for root in roots:
                    found = self.scanner.fallback_scan(root, since, hints)
                    if found:
                        downloaded = found
                        matched_root = root
                        scan_mode = "recent" if recent_only else "cached"
                        break
                if downloaded:
                    break
            self.logger.info(f"[JM] 下载完成扫描(回退 mtime): roots={roots}, matched={matched_root}, mode={scan_mode}, 扫到 {len(downloaded)} 张图片")
        else:
            try:
                base_dir_attr = getattr(option.dir_rule, "base_dir", None)
                matched_root = (
                    Path(str(base_dir_attr))
                    if base_dir_attr
                    else self.config_service.download_dir()
                )
            except Exception:  # noqa: BLE001
                matched_root = self.config_service.download_dir()
        return downloaded, matched_root

    # ---------------------------------------------------------------- #
    # 提示文本
    # ---------------------------------------------------------------- #
    @staticmethod
    def _progress_text(
        aid: str,
        total_photos: int,
        cached_count: int,
        missing_count: int,
        cached_images: int,
        all_cached: bool,
    ) -> str:
        """下载前状态提示: 区分全部命中 / 部分命中 / 全部缺失."""
        if all_cached:
            return (
                f"✅ 本子 {aid} 已存在于下载目录, 跳过下载, 直接推送 "
                f"({total_photos} 章, 共 {cached_images} 张图片)"
            )
        if cached_count:
            return (
                f"⏳ 本子 {aid} 部分章节已缓存 "
                f"({cached_count}/{total_photos} 章), "
                f"补下缺失 {missing_count} 章 ..."
            )
        return f"⏳ 正在下载 {missing_count} 个章节 ..."

    @staticmethod
    def _done_text(
        aid: str,
        missing_count: int,
        image_count: int,
        total_size: int,
        elapsed: float,
        all_cached: bool,
    ) -> str:
        """下载完成提示."""
        if all_cached:
            return (
                f"✅ ID {aid} ({image_count} 张图片, "
                f"{fmt_size(total_size)}, 已缓存跳过下载)"
            )
        return (
            f"✅ 下载完成: ID {aid} ({missing_count} 章补下, "
            f"共 {image_count} 张图片, {fmt_size(total_size)}, "
            f"耗时 {elapsed:.1f}s)"
        )
