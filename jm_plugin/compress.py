"""合并转发图片预处理: 并发压缩 + 指纹持久缓存。

设计参照 astrbot_plugin_parser 的打包思路:
- 任务早启动: send() 入口一次性 gather 全部图片的准备任务 (asyncio.to_thread +
  Semaphore 控并发), 而不是每批发送前才逐张处理;
- 存在即复用 (对标 parser streamd): 压缩产物以「源文件名 + 大小 + mtime 指纹」
  写入 forward_cache/<aid>/, 二次推送同一本子直接命中缓存;
- 单张失败不拖垮整体: 坏图 / 动图 / 重压缩无收益的图一律回退原图直传
  (fail-open), 不影响其余图片正常发送。

发送侧耗时主要是 NapCat 把图片上传到 QQ 服务器, 与图片字节量近似成正比,
因此把长边几千像素的大页图缩放重压缩是合并转发提速的主要手段。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .utils import fmt_size

try:
    from astrbot.api import logger as _astrbot_logger
except ImportError:
    _astrbot_logger = None

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - Pillow 是声明依赖, 正常环境不缺
    Image = None
    ImageOps = None


def _logger() -> Any:
    if _astrbot_logger is not None:
        return _astrbot_logger
    return logging.getLogger("astrbot_plugin_jm")


# 小图快速通道阈值: 该大小以下的 jpg 直接原图直传, 省一次 PIL 解码
SMALL_FASTPATH_BYTES = 256 * 1024
# 预处理线程并发度
DEFAULT_CONCURRENCY = 6


@dataclass
class PrepareStats:
    """预处理结果: ready 路径列表 (顺序与输入一致) + 计数器。"""

    paths: list[Path] = field(default_factory=list)
    compressed: int = 0
    cache_hits: int = 0
    fallbacks: int = 0
    bytes_before: int = 0
    bytes_after: int = 0

    @property
    def passthrough(self) -> int:
        """未经处理的原图张数 (小图快速通道 / 动图 / 无收益回退)。"""
        return len(self.paths) - self.compressed - self.cache_hits - self.fallbacks

    @property
    def saved_pct(self) -> float:
        if self.bytes_before <= 0:
            return 0.0
        return max(0.0, 100.0 * (1 - self.bytes_after / self.bytes_before))


def safe_aid(aid: str) -> str:
    """aid 只保留安全字符, 作为缓存子目录名。"""
    keep = "".join(c for c in str(aid or "").strip() if c.isalnum() or c in "-_")
    return keep or "unknown"


def _fingerprint(src: Path) -> str:
    """源文件名 + 大小 + mtime 指纹, 保证缓存产物始终对应源文件的当前内容。"""
    st = src.stat()
    raw = f"{src.name}-{st.st_size}-{st.st_mtime_ns}"
    return hashlib.sha1(raw.encode("utf-8", "surrogateescape")).hexdigest()[:8]


def prepare_image(
    src: Path,
    cache_dir: Path,
    aid: str,
    *,
    enabled: bool = True,
    use_cache: bool = True,
    max_edge: int = 2048,
    quality: int = 85,
) -> tuple[Path, str]:
    """把单张图片准备为待发送形态 (同步, 由线程池执行)。

    :return: (最终路径, 类型); 类型 ∈ original|compressed|cache_hit|fallback。
             original/fallback 即 src 本身, compressed/cache_hit 为缓存产物。
    """
    try:
        src_size = src.stat().st_size
    except OSError:
        return src, "fallback"

    if not enabled or Image is None:
        return src, "original"

    # 快速通道: 已是小体积 JPEG, 直接原图直传
    if (
        src.suffix.lower() in (".jpg", ".jpeg", ".jfif")
        and src_size <= SMALL_FASTPATH_BYTES
    ):
        return src, "original"

    aid_dir = cache_dir / safe_aid(aid)
    dst = aid_dir / f"{src.stem}_{_fingerprint(src)}.jpg"
    if use_cache and dst.is_file() and dst.stat().st_size > 0:
        return dst, "cache_hit"

    try:
        with Image.open(src) as img:
            if getattr(img, "is_animated", False):
                # GIF / 动画 WEBP 不能重编码成单帧 JPEG, 保动画回退原图
                return src, "original"

            width, height = img.size
            try:
                transposed = ImageOps.exif_transpose(img)
                if transposed is not None:
                    img = transposed
            except Exception:  # noqa: BLE001 - EXIF 处理失败不影响压缩
                pass

            mode = img.mode
            if mode == "P" and "transparency" not in img.info:
                img = img.convert("RGB")
            elif mode != "RGB":
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                img = background

            if max_edge > 0 and max(width, height) > max_edge:
                img.thumbnail((max_edge, max_edge), Image.LANCZOS)

            aid_dir.mkdir(parents=True, exist_ok=True)
            img.save(
                dst,
                format="JPEG",
                quality=max(1, min(quality, 95)),
                optimize=True,
                progressive=True,
            )

        new_size = dst.stat().st_size
        if new_size <= 0 or new_size >= src_size:
            # 重压缩无收益, 回退原图
            dst.unlink(missing_ok=True)
            return src, "original"
        return dst, "compressed"
    except Exception as e:  # noqa: BLE001 - 单张坏图回退, 不拖垮整体
        _logger().warning(f"[JM] 图片预处理失败, 回退原图 {src}: {e}")
        try:
            dst.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return src, "fallback"


def cleanup_cache(cache_dir: Path, ttl_days: int) -> None:
    """删除超过 TTL 的缓存文件与空 aid 目录 (只动 forward_cache, 不碰下载图)。

    ttl_days <= 0 表示永不过期, 直接跳过。
    """
    if ttl_days <= 0 or not cache_dir.is_dir():
        return
    deadline = time.time() - ttl_days * 86400
    removed = 0
    for aid_dir in list(cache_dir.iterdir()):
        if not aid_dir.is_dir():
            continue
        for item in list(aid_dir.iterdir()):
            try:
                if item.is_file() and item.stat().st_mtime < deadline:
                    item.unlink()
                    removed += 1
            except OSError:
                continue
        if not any(aid_dir.iterdir()):
            try:
                aid_dir.rmdir()
            except OSError:
                pass
    if removed:
        _logger().info(
            f"[JM] 已清理 {removed} 个过期合并转发压缩缓存 (TTL {ttl_days} 天)"
        )


async def prepare_images(
    images: Iterable[Path],
    *,
    cache_dir: Path,
    aid: str,
    enabled: bool = True,
    use_cache: bool = True,
    max_edge: int = 2048,
    quality: int = 85,
    concurrency: int = DEFAULT_CONCURRENCY,
    clean_ttl_days: Optional[int] = 7,
) -> PrepareStats:
    """全量并发预处理所有待发送图片 (auto_task 思路: 入口即铺开全部任务)。

    单张失败只计入 fallbacks, 不影响其他图片。
    """
    originals = list(images)
    stats = PrepareStats(paths=originals)
    if not originals:
        return stats

    if clean_ttl_days is not None:
        try:
            await asyncio.to_thread(cleanup_cache, cache_dir, clean_ttl_days or 0)
        except Exception as e:  # noqa: BLE001
            _logger().warning(f"[JM] 压缩缓存清理失败 (忽略): {e}")

    if not enabled or Image is None:
        return stats

    sem = asyncio.Semaphore(max(1, concurrency))

    async def run(idx: int, path: Path) -> tuple[int, Path, str]:
        async with sem:
            final_path, kind = await asyncio.to_thread(
                prepare_image,
                path,
                cache_dir,
                aid,
                enabled=enabled,
                use_cache=use_cache,
                max_edge=max_edge,
                quality=quality,
            )
        return idx, final_path, kind

    results = await asyncio.gather(
        *(run(i, p) for i, p in enumerate(originals)), return_exceptions=True
    )

    for res in results:
        if isinstance(res, BaseException):
            stats.fallbacks += 1
            continue
        idx, final_path, kind = res
        try:
            before = originals[idx].stat().st_size
            after = final_path.stat().st_size
        except OSError:
            continue
        stats.bytes_before += before
        stats.bytes_after += after
        if kind == "compressed":
            stats.compressed += 1
        elif kind == "cache_hit":
            stats.cache_hits += 1
        elif kind == "fallback":
            stats.fallbacks += 1
        stats.paths[idx] = final_path
    return stats


def log_stats(stats: PrepareStats, prep_elapsed: float, logger: Any = None) -> None:
    lg = logger or _logger()
    extra = ""
    if stats.bytes_before > 0:
        extra = (
            f", 字节 {fmt_size(stats.bytes_before)}→{fmt_size(stats.bytes_after)} "
            f"(-{stats.saved_pct:.0f}%)"
        )
    lg.info(
        f"[JM] 合并转发图片预处理: 新压缩 {stats.compressed}, 缓存命中 {stats.cache_hits}, "
        f"原图直传 {stats.passthrough}, 回退原图 {stats.fallbacks}{extra}, "
        f"耗时 {prep_elapsed:.1f}s"
    )
