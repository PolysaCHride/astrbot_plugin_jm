"""插件配置与 jmcomic option 生命周期管理。

职责:
- 数据目录解析 (custom_data_dir > StarTools > 兜底)
- 下载目录 (含越界保护)
- Docker 宿主机/容器路径映射 (兼容容器部署场景)
- jmcomic option 字典构建与三级重建 (dict -> YAML 文件 -> 默认)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

try:  # AstrBot 运行环境
    from astrbot.api import logger as _astrbot_logger
except ImportError:  # 独立运行 / 单元测试
    _astrbot_logger = None

try:
    from astrbot.api.star import StarTools as _StarTools
except ImportError:
    _StarTools = None


def _logger() -> Any:
    if _astrbot_logger is not None:
        return _astrbot_logger
    return logging.getLogger("astrbot_plugin_jm")


_PLUGIN_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_DOCKER_DATA_PREFIX = "/AstrBot/data"
_DOCKER_HOST_SUFFIXES = ("/root/astrbot/data", "/root/AstrBot/astrbot/data")


class ConfigService:
    """插件配置 + 数据目录 + jmcomic option 生命周期。"""

    def __init__(
        self,
        plugin_name: str,
        config: Optional[dict] = None,
        logger: Any = None,
    ) -> None:
        self.plugin_name = plugin_name
        self.config: dict = config or {}
        self.logger = logger if logger is not None else _logger()
        self.option: Any = None
        self._option_lock = asyncio.Lock()
        self._data_dir: Optional[Path] = None

    # ---------------------------------------------------------------- #
    # 类型化配置读取
    # ---------------------------------------------------------------- #
    def get_str(self, key: str, default: str = "") -> str:
        """读取字符串配置, None 时回退默认值."""
        value = self.config.get(key)
        if value is None:
            return default
        return str(value).strip()

    def get_int(self, key: str, default: int = 0) -> int:
        """读取整数配置, 非法值回退默认值 (显式 0 保留)."""
        value = self.config.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        """读取布尔配置, 兼容字符串形式的 true/false."""
        value = self.config.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    # ---------------------------------------------------------------- #
    # 数据目录
    # ---------------------------------------------------------------- #
    @property
    def data_dir(self) -> Path:
        """解析后的插件数据目录, 首次访问时创建并缓存."""
        if self._data_dir is None:
            self._data_dir = self._resolve_data_dir()
            self._data_dir.mkdir(parents=True, exist_ok=True)
        return self._data_dir

    def _resolve_data_dir(self) -> Path:
        """优先级: custom_data_dir > StarTools.get_data_dir > 兜底."""
        custom = self.get_str("custom_data_dir")
        if custom:
            return Path(custom).expanduser().resolve()
        if _StarTools is not None:
            try:
                try:
                    return (
                        Path(_StarTools.get_data_dir(self.plugin_name))
                        .expanduser()
                        .resolve()
                    )
                except TypeError:
                    return Path(_StarTools.get_data_dir()).expanduser().resolve()
            except Exception:  # noqa: BLE001
                pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return (
                (Path(get_astrbot_data_path()) / "plugin_data" / self.plugin_name)
                .expanduser()
                .resolve()
            )
        except Exception:  # noqa: BLE001
            return (Path.cwd() / "data" / "plugin_data" / self.plugin_name).resolve()

    def download_dir(self) -> Path:
        """下载子目录 (带越界保护), 不存在时自动创建."""
        sub = self.get_str("download_subdir", "downloads") or "downloads"
        base = self.data_dir.resolve()
        d = (base / sub).resolve()
        if d != base and base not in d.parents:
            self.logger.warning("[JM] download_subdir 越出插件数据目录, 已回退到 downloads")
            d = base / "downloads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---------------------------------------------------------------- #
    # Docker 宿主机/容器路径兼容
    # ---------------------------------------------------------------- #
    def docker_host_mapped_paths(self, path: Path) -> list[Path]:
        """把容器内 /AstrBot/data 前缀路径映射为常见宿主机路径."""
        # Windows 下 str(Path) 是反斜杠, 统一归一化为正斜杠再匹配前缀
        raw = str(path).replace("\\", "/")
        mapped: list[Path] = []
        if raw == _DOCKER_DATA_PREFIX or raw.startswith(_DOCKER_DATA_PREFIX + "/"):
            suffix = raw[len(_DOCKER_DATA_PREFIX):]
            for host in _DOCKER_HOST_SUFFIXES:
                mapped.append(Path(host + suffix))
        return mapped

    def scan_roots(self, *roots: Optional[Path]) -> list[Path]:
        """生成扫描候选根目录: 输入 + Docker 映射 + 插件数据目录, 去重."""
        candidates: list[Optional[Path]] = []
        for root in roots:
            candidates.append(root)
            if root is not None:
                candidates.extend(self.docker_host_mapped_paths(root))
        candidates.append(self.data_dir)
        candidates.extend(self.docker_host_mapped_paths(self.data_dir))
        return [c for c in _dedupe(candidates) if c is not None]

    # ---------------------------------------------------------------- #
    # jmcomic option
    # ---------------------------------------------------------------- #
    def build_option_dict(self) -> dict:
        """把插件配置映射为 jmcomic option 字典."""
        client_impl = self.get_str("client_impl", "api")
        if client_impl not in ("html", "api"):
            client_impl = "api"

        domain: dict[str, list[str]] = {}
        custom_domain = self.get_str("custom_domain")
        if custom_domain:
            domain[client_impl] = [d.strip() for d in custom_domain.split(",") if d.strip()]

        proxies: Any
        if self.get_bool("use_proxy"):
            proxy = self.get_str("proxy")
            proxies = {"http": proxy, "https": proxy} if proxy else "system"
        else:
            proxies = None

        postman: dict[str, Any] = {
            "meta_data": {
                "proxies": proxies,
                "headers": {"User-Agent": _PLUGIN_USER_AGENT},
            },
        }

        suffix = self.get_str("image_suffix") or None

        return {
            "client": {
                "impl": client_impl,
                "domain": domain,
                "retry_times": max(1, self.get_int("retry_times", 3)),
                "postman": postman,
            },
            "download": {
                "image": {"decode": True, "suffix": suffix},
                "threading": {
                    "image": max(1, self.get_int("image_thread_count", 16)),
                    "photo": max(1, self.get_int("photo_thread_count", 4)),
                },
            },
            "dir_rule": {
                "rule": self.get_str("dir_rule", "Bd / Atitle / Ptitle")
                or "Bd / Atitle / Ptitle",
            },
            "log": False,  # 关闭 jmcomic 内置日志, 统一走 AstrBot logger
        }

    async def rebuild_option(self, initial: bool = False) -> Any:
        """按当前配置重建 jmcomic option, 三级回退, 结果缓存到 self.option.

        兼容性说明:
        - 新版 jmcomic (2.7 实测) 只提供 create_option_by_file, 因此默认走 YAML 文件方式;
        - 若未来版本提供 create_option_by_dict 则优先使用 (性能更好);
        - 两级都失败时回退 JmOption.default().
        """
        async with self._option_lock:
            opt = self.build_option_dict()
            opt.setdefault("dir_rule", {})
            opt["dir_rule"]["base_dir"] = str(self.download_dir())

            try:
                import jmcomic
            except ImportError:
                jmcomic = None
            if jmcomic is None:
                raise RuntimeError("jmcomic 不可用, 无法构建 option")

            option: Any = None
            if hasattr(jmcomic, "create_option_by_dict"):
                try:
                    option = jmcomic.create_option_by_dict(opt)
                except Exception as e:  # noqa: BLE001
                    self.logger.warning(f"[JM] create_option_by_dict 失败, 回退到 YAML 文件方式: {e}")

            if option is None:
                opt_file = self.data_dir / ".option.yml"
                try:
                    with open(opt_file, "w", encoding="utf-8") as f:
                        yaml.safe_dump(opt, f, allow_unicode=True, sort_keys=False)
                    option = jmcomic.create_option_by_file(str(opt_file))
                except Exception as e:  # noqa: BLE001
                    self.logger.error(f"[JM] create_option_by_file 也失败, 退回到默认 option: {e}")
                    option = jmcomic.JmOption.default()

            self.option = option
            if not initial:
                self.logger.info("[JM] option 已重建")
            return option


def _dedupe(paths: Iterable) -> list:
    """按字符串形式去重, 保持顺序, 保留 None 项由调用方过滤."""
    seen: set[str] = set()
    out: list = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out
