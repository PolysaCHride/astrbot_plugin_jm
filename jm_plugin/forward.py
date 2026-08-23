"""QQ 合并转发图集推送 (直接调用 OneBot send_*_forward_msg)。

设计说明:
- AstrBot 的 Node.to_dict() 会把节点内图片强制序列化为 base64;
- v1 方案把图片注册到 AstrBot 文件服务后传 HTTP URL, 但文件 token 是
  一次性使用且 NapCat 下载失败时该图片节点会被静默丢弃, QQ 客户端即
  显示「该类消息类型暂不支持查看」, 同时每张图多一次网络下载导致打包慢;
- 本实现绕开文件服务直接构造 OneBot payload:
    transport=file   (默认): 传 file:/// 本地路径, NapCat 本机直读, 最快最稳;
    transport=base64 : 内嵌 base64:// 数据, 兼容 NapCat 在其他机器的场景。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .utils import path_sort_key, unique_paths

try:
    from astrbot.api import logger as _astrbot_logger
except ImportError:
    _astrbot_logger = None


def _logger() -> Any:
    if _astrbot_logger is not None:
        return _astrbot_logger
    return logging.getLogger("astrbot_plugin_jm")


class ForwardImageTransportError(RuntimeError):
    """无法通过 OneBot 发送 QQ 合并转发图片时抛出。"""


class ForwardService:
    """QQ 合并转发图集推送。"""

    _TRANSPORTS = ("file", "base64")

    def __init__(
        self,
        config_service,
        config: Optional[dict] = None,
        context: Any = None,
        logger: Any = None,
    ) -> None:
        self.config_service = config_service
        self.config: dict = config or {}
        self.context = context
        self.logger = logger if logger is not None else _logger()

    @property
    def transport(self) -> str:
        """图片传输方式: file (默认) | base64."""
        value = str(self.config.get("forward_image_transport") or "file").strip().lower()
        return value if value in self._TRANSPORTS else "file"

    # ---------------------------------------------------------------- #
    # 对外入口
    # ---------------------------------------------------------------- #
    async def send(
        self,
        platform_id: str,
        session_id: str,
        is_group: bool,
        self_id: str,
        files: Iterable[Path],
        aid: str,
        base_dir: Path,
        on_batch: Optional[Callable[[int, int], Any]] = None,
    ) -> dict:
        """把本地图片分批打包为 QQ 合并转发消息并发送。

        :return: 统计信息 {images, batches, transport, elapsed}
        """
        images = sorted(
            [p for p in unique_paths(files) if p.is_file() and p.stat().st_size > 0],
            key=path_sort_key,
        )
        if not images:
            return {"images": 0, "batches": 0, "transport": self.transport, "elapsed": 0.0}

        platform = self.context.get_platform_inst(platform_id) if self.context else None
        if platform is None:
            raise ForwardImageTransportError(
                f"找不到来源平台 {platform_id!r}，无法调用 QQ 合并转发 API"
            )
        client = platform.get_client()
        if not callable(getattr(client, "call_action", None)):
            raise ForwardImageTransportError(
                "当前平台不是支持 OneBot call_action 的 aiocqhttp 协议端"
            )

        # 每批大小: max_forward_images 是「每条合并转发」的图片上限,
        # 0 = 不限制, 全部塞进单批 (用户显式选择, 自担 QQ 多消息大小限制的风险)
        raw_max = self.config_service.get_int("max_forward_images", 10)
        batch_size = len(images) if raw_max == 0 else max(1, raw_max)
        total = len(images)
        batch_count = (total + batch_size - 1) // batch_size

        t0 = time.time()
        for idx, start in enumerate(range(0, total, batch_size), 1):
            batch = images[start : start + batch_size]
            nodes = await self._build_nodes(
                batch, aid, base_dir, self_id, idx, batch_count, start, total
            )
            action = "send_group_forward_msg" if is_group else "send_private_forward_msg"
            if is_group:
                payload = {"group_id": session_id, "messages": nodes}
            else:
                payload = {"user_id": session_id, "messages": nodes}
            await client.call_action(action, **payload)
            if on_batch is not None:
                await on_batch(idx, batch_count)
            await asyncio.sleep(0)

        elapsed = time.time() - t0
        self.logger.info(
            f"[JM] 合并转发已发送: {total} 张图片 / {batch_count} 批, "
            f"transport={self.transport}, 耗时 {elapsed:.1f}s"
        )
        return {"images": total, "batches": batch_count, "transport": self.transport, "elapsed": elapsed}

    # ---------------------------------------------------------------- #
    # payload 构造
    # ---------------------------------------------------------------- #
    async def _build_nodes(
        self,
        batch: list[Path],
        aid: str,
        base_dir: Path,
        self_id: str,
        idx: int,
        batch_count: int,
        start: int,
        total: int,
    ) -> list[dict]:
        """构造一批合并转发节点: 1 个说明节点 + N 个图片节点."""
        user_id = str(self_id)
        nodes = [
            self._text_node(
                user_id,
                "JM 漫画下载器",
                (
                    f"JM 图集 [{aid}]"
                    + chr(10)
                    + f"第 {idx}/{batch_count} 批, 共 {total} 张, 本批 {len(batch)} 张"
                    + chr(10)
                    + f"来源目录: {base_dir}"
                ),
            )
        ]
        if self.transport == "base64":
            encoded = await asyncio.gather(
                *(asyncio.to_thread(_read_base64, p) for p in batch)
            )
            for n, b64 in enumerate(encoded, 1):
                nodes.append(self._image_node(user_id, f"{start + n}/{total}", {"file": "base64://" + b64}))
        else:
            for n, p in enumerate(batch, 1):
                nodes.append(
                    self._image_node(
                        user_id,
                        f"{start + n}/{total}",
                        {"file": p.as_uri()},
                    )
                )
        return nodes

    @staticmethod
    def _text_node(user_id: str, nickname: str, text: str) -> dict:
        """构造纯文本说明节点."""
        return {
            "type": "node",
            "data": {
                "user_id": user_id,
                "nickname": nickname,
                "content": [{"type": "text", "data": {"text": text}}],
            },
        }

    @staticmethod
    def _image_node(user_id: str, nickname: str, file_data: dict) -> dict:
        """构造单张图片节点."""
        return {
            "type": "node",
            "data": {
                "user_id": user_id,
                "nickname": nickname,
                "content": [{"type": "image", "data": file_data}],
            },
        }


def _read_base64(path: Path) -> str:
    """读取图片文件并编码为 base64 文本 (无前缀)."""
    return base64.b64encode(path.read_bytes()).decode("ascii")
