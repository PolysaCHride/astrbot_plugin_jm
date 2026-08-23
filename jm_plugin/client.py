"""jmcomic 客户端封装: option 缓存 / 登录 / 阻塞调用."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

try:
    from astrbot.api import logger as _astrbot_logger
except ImportError:
    _astrbot_logger = None


def _logger() -> Any:
    if _astrbot_logger is not None:
        return _astrbot_logger
    return logging.getLogger("astrbot_plugin_jm")


class ClientService:
    """jmcomic 客户端: option 单例缓存、登录状态与线程池调用."""

    def __init__(self, config_service, config: Optional[dict] = None, logger: Any = None) -> None:
        self.config_service = config_service
        self.config: dict = config or {}
        self.logger = logger if logger is not None else _logger()
        self._logged_in = False

    @property
    def logged_in(self) -> bool:
        """最近一次登录尝试是否成功 (仅状态展示用)."""
        return self._logged_in

    def reset_login(self) -> None:
        """配置重载后清空登录状态."""
        self._logged_in = False

    async def ensure_option(self) -> Any:
        """获取可用的 jmcomic option, 未构建时按需构建."""
        if self.config_service.option is None:
            await self.config_service.rebuild_option(initial=True)
        return self.config_service.option

    def new_client(self, option: Optional[Any] = None) -> Any:
        """基于当前 (或指定) option 创建 jmcomic 客户端."""
        opt = option if option is not None else self.config_service.option
        if opt is None:
            raise RuntimeError("jmcomic option 尚未构建")
        return opt.new_jm_client()

    async def maybe_login(self, client: Any) -> None:
        """配置启用登录时执行 client.login, 失败仅记录日志不阻断主流程."""
        if not self.config.get("enable_login", False):
            return
        username = str(self.config.get("username") or "").strip()
        password = str(self.config.get("password") or "").strip()
        if not username or not password:
            return
        try:
            await asyncio.to_thread(client.login, username, password)
            self._logged_in = True
            self.logger.info("[JM] 登录成功")
        except Exception as e:  # noqa: BLE001
            self._logged_in = False
            self.logger.error(f"[JM] 登录失败: {e}")

    async def run_blocking(self, func, *args, **kwargs):
        """把 jmcomic 的同步阻塞调用放到线程池执行."""
        return await asyncio.to_thread(func, *args, **kwargs)
