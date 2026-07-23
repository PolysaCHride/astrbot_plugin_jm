"""astrbot_plugin_jm - 基于 JMComic-Crawler-Python 的禁漫搜索 / 详情 / 下载插件"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Image
from astrbot.core.config import AstrBotConfig

import yaml

import jmcomic

# 本子 / 章节 ID 都是纯数字字符串
ID_PATTERN = re.compile(r"\d{4,}")
IMAGE_SUFFIXES = {
    ".jpg",
    ".jpe",
    ".jpeg",
    ".jfif",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
}


class ForwardImageTransportError(RuntimeError):
    """无法安全地发送 QQ 合并转发图片时抛出。"""


def extract_id(text: str) -> Optional[str]:
    """从消息文本中提取纯数字 ID (支持 6 位及以上, 避免误中常用数字)."""
    if not text:
        return None
    m = ID_PATTERN.search(text)
    return m.group(0) if m else None


def safe_filename(name: str, max_len: int = 80) -> str:
    """移除文件名中不能出现的字符."""
    if not name:
        return "untitled"
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len]
    return name or "untitled"


def fmt_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}PB"


class JMPlugin(Star):
    """JM 漫画下载器主类.

    使用 `command_group("jm")` 将 /jm 系列子命令组织在一起.
    支持子命令: help, status, reload, search, info, episodes, photo, cover,
    d, ranking, tags.
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # 数据目录解析优先级:
        #   1. 配置项 custom_data_dir (用户显式覆盖)
        #   2. StarTools.get_data_dir() (AstrBot 标准接口)
        #   3. get_astrbot_data_path() + self.name (老版本兜底)
        # 注: 当部署环境导致 2/3 返回的路径与实际不一致时
        #     (例如容器 /AstrBot/data/... 与宿主 /root/astrbot/data/...),
        #     通过配置项 1 直接指定真实路径即可.
        self.data_dir: Path = self._resolve_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"[JM] 数据目录: {self.data_dir}")

        # 启动时构建 jmcomic option
        self.option: Optional[jmcomic.JmOption] = None
        self._option_lock = asyncio.Lock()
        self._logged_in: bool = False
        self._background_tasks: set[asyncio.Task] = set()

    def _resolve_data_dir(self) -> Path:
        """解析插件数据目录. 优先读取 custom_data_dir 配置项."""
        cfg = self.config
        custom = (cfg.get("custom_data_dir") or "").strip() if cfg else ""
        if custom:
            return Path(custom).expanduser().resolve()

        try:
            try:
                return Path(StarTools.get_data_dir(self.name))
            except TypeError:
                return Path(StarTools.get_data_dir())
        except Exception:  # noqa: BLE001
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            return Path(get_astrbot_data_path()) / "plugin_data" / self.name

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def _ensure_runtime_attrs(self) -> None:
        """补齐运行期属性, 兼容热重载时旧实例未完整初始化的情况."""
        if not hasattr(self, "option"):
            self.option = None
        if not hasattr(self, "_option_lock"):
            self._option_lock = asyncio.Lock()
        if not hasattr(self, "_logged_in"):
            self._logged_in = False
        if not hasattr(self, "_background_tasks"):
            self._background_tasks = set()
        if not hasattr(self, "data_dir"):
            self.data_dir = self._resolve_data_dir()
            self.data_dir.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        """AstrBot 在加载本插件后会调用一次."""
        self._ensure_runtime_attrs()
        try:
            await self._rebuild_option(initial=True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 初始化 option 失败: {e}", exc_info=True)
        logger.info(f"[JM] 插件已加载, 数据目录: {self.data_dir}")

    async def terminate(self) -> None:
        """插件卸载时调用."""
        self._ensure_runtime_attrs()
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        logger.info("[JM] 插件已卸载")

    # ------------------------------------------------------------------ #
    # Option 管理
    # ------------------------------------------------------------------ #
    async def _rebuild_option(self, initial: bool = False) -> jmcomic.JmOption:
        """根据当前 plugin_config 重建 jmcomic option.

        兼容性说明:
        - 新版 jmcomic (>=2.5?) 提供了 ``create_option_by_dict`` 直接吃 dict
        - 老版本只有 ``create_option_by_file``, 因此这里统一把 dict 写
          成 YAML 临时文件再用 ``create_option_by_file`` 加载, 两种环境都能跑
        """
        self._ensure_runtime_attrs()
        async with self._option_lock:
            opt = self._build_option_dict()
            download_dir = self._resolve_download_dir()
            opt.setdefault("dir_rule", {})
            opt["dir_rule"]["base_dir"] = str(download_dir)

            # 优先尝试 create_option_by_dict (新版 jmcomic 才有, 性能更好)
            option = None
            if hasattr(jmcomic, "create_option_by_dict"):
                try:
                    option = jmcomic.create_option_by_dict(opt)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[JM] create_option_by_dict 失败, 回退到 YAML 文件方式: {e}")

            if option is None:
                # 兜底: 写 YAML 文件, 用 create_option_by_file 加载
                opt_file = self.data_dir / ".option.yml"
                try:
                    with open(opt_file, "w", encoding="utf-8") as f:
                        yaml.safe_dump(opt, f, allow_unicode=True, sort_keys=False)
                    option = jmcomic.create_option_by_file(str(opt_file))
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[JM] create_option_by_file 也失败, 退回到默认 option: {e}")
                    option = jmcomic.JmOption.default()

            self.option = option
            if not initial:
                logger.info("[JM] option 已重建")
            return option

    def _build_option_dict(self) -> dict:
        """根据 plugin_config 生成 jmcomic option 字典."""
        cfg = self.config

        client_impl = (cfg.get("client_impl", "api") or "api").strip()
        if client_impl not in ("html", "api"):
            client_impl = "api"

        domain: dict[str, list[str]] = {}
        custom_domain = (cfg.get("custom_domain") or "").strip()
        if custom_domain:
            domain[client_impl] = [d.strip() for d in custom_domain.split(",") if d.strip()]

        proxies: Any
        if cfg.get("use_proxy", False):
            proxy = (cfg.get("proxy") or "").strip()
            proxies = {"http": proxy, "https": proxy} if proxy else "system"
        else:
            proxies = None

        postman: dict[str, Any] = {
            "meta_data": {
                "proxies": proxies,
                "headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            },
        }

        suffix = (cfg.get("image_suffix") or "").strip() or None

        opt_dict: dict[str, Any] = {
            "client": {
                "impl": client_impl,
                "domain": domain,
                "retry_times": max(1, int(cfg.get("retry_times", 3) or 3)),
                "postman": postman,
            },
            "download": {
                "image": {"decode": True, "suffix": suffix},
                "threading": {
                    "image": max(1, int(cfg.get("image_thread_count", 16) or 16)),
                    "photo": max(1, int(cfg.get("photo_thread_count", 4) or 4)),
                },
            },
            "dir_rule": {
                "rule": (cfg.get("dir_rule") or "Bd / Atitle / Ptitle").strip(),
            },
            "log": False,  # 关闭 jmcomic 内置日志, 统一通过 AstrBot logger 输出
        }

        return opt_dict

    def _resolve_download_dir(self) -> Path:
        sub = (self.config.get("download_subdir") or "downloads").strip() or "downloads"
        base_dir = self.data_dir.resolve()
        d = (base_dir / sub).resolve()
        if d != base_dir and base_dir not in d.parents:
            logger.warning("[JM] download_subdir 越出插件数据目录, 已回退到 downloads")
            d = base_dir / "downloads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _docker_host_mapped_paths(self, path: Path) -> list[Path]:
        """把 AstrBot Docker 容器内常见数据路径映射到宿主机默认路径."""
        raw = str(path)
        prefix = "/AstrBot/data"
        mapped: list[Path] = []
        if raw == prefix or raw.startswith(prefix + "/"):
            suffix = raw[len(prefix):]
            mapped.append(Path("/root/astrbot/data" + suffix))
            mapped.append(Path("/root/AstrBot/astrbot/data" + suffix))
        return mapped

    def _unique_paths(self, paths: Iterable[Optional[Path]]) -> list[Path]:
        unique: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            if path is None:
                continue
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    def _scan_roots(self, *roots: Optional[Path]) -> list[Path]:
        """生成下载结果扫描候选目录, 兼容 Docker 宿主机/容器路径差异."""
        candidates: list[Optional[Path]] = []
        for root in roots:
            candidates.append(root)
            if root is not None:
                candidates.extend(self._docker_host_mapped_paths(root))

        candidates.append(self.data_dir)
        candidates.extend(self._docker_host_mapped_paths(self.data_dir))
        return self._unique_paths(candidates)

    def _photo_save_dir(self, option, album, photo) -> Optional[Path]:
        """根据当前 dir_rule 计算某个章节的落盘目录.

        直接复用 jmcomic ``DirRule.decide_image_save_dir``, 这样无论用户把
        ``dir_rule`` 配成什么 DSL (Atitle / Aid / Ptitle / Pindex ...), 得到的都
        是 jmcomic 自己算出的真实路径, 与实际落盘一致. 同时把 Docker 宿主机/
        容器映射路径一起作为候选, 容器内扫描找不到时能在宿主机路径上命中.

        album 可为 None (单章节下载 fallback 场景). 此时若 dir_rule 含 ``A*``
        规则 (如 Atitle), ``decide_image_save_dir`` 会在 ``getattr(None, ...)``
        上抛异常, 这里捕获后返回 None, 调用方据此回退到全量下载.
        """
        try:
            save_dir = option.dir_rule.decide_image_save_dir(album, photo)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[JM] 计算章节保存目录失败 (photo={getattr(photo, 'photo_id', '?')}): {e}")
            return None
        if not save_dir:
            return None
        primary = Path(str(save_dir))
        candidates = self._unique_paths(
            [primary, *self._docker_host_mapped_paths(primary)]
        )
        for cand in candidates:
            if cand.exists():
                return cand
        # 目录尚未创建 (章节未下载), 返回 primary 让调用方据此判定缺失
        return primary

    def _scan_photo_dir_images(self, dir_path: Optional[Path]) -> list[Path]:
        """扫描某个章节目录下的全部图片, 按自然序返回. 目录缺失/无图片返回 []."""
        if dir_path is None or not dir_path.exists():
            return []
        images = [
            p for p in dir_path.rglob("*")
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        ]
        return sorted(images, key=self._path_sort_key)

    def _scan_cached_photos(
        self,
        option,
        album,
        photos: list,
    ) -> tuple[list, list, list[Path]]:
        """逐章节核对下载目录, 区分已缓存与缺失章节.

        :return: (cached_photos, missing_photos, cached_images)
          - cached_photos: 目录存在且含图片的章节对象列表
          - missing_photos: 目录缺失或无图片的章节对象列表
          - cached_images: 已缓存章节的图片路径 (自然序合并), 直接可用于推送
        """
        cached_photos: list = []
        missing_photos: list = []
        cached_images: list[Path] = []
        for ph in photos:
            save_dir = self._photo_save_dir(option, album, ph)
            images = self._scan_photo_dir_images(save_dir)
            if images:
                cached_photos.append(ph)
                cached_images.extend(images)
            else:
                missing_photos.append(ph)
        cached_images = sorted(cached_images, key=self._path_sort_key)
        return cached_photos, missing_photos, cached_images

    async def _ensure_option(self) -> jmcomic.JmOption:
        self._ensure_runtime_attrs()
        if self.option is None:
            await self._rebuild_option()
        assert self.option is not None
        return self.option

    async def _maybe_login(self, client) -> None:
        self._ensure_runtime_attrs()
        if not self.config.get("enable_login", False):
            return
        username = (self.config.get("username") or "").strip()
        password = (self.config.get("password") or "").strip()
        if not username or not password:
            return
        try:
            await asyncio.to_thread(client.login, username, password)
            self._logged_in = True
            logger.info("[JM] 登录成功")
        except Exception as e:  # noqa: BLE001
            self._logged_in = False
            logger.error(f"[JM] 登录失败: {e}")

    # ------------------------------------------------------------------ #
    # 通用辅助
    # ------------------------------------------------------------------ #
    async def _run_blocking(self, func, *args, **kwargs):
        """把 jmcomic 的同步阻塞调用放到线程池中执行."""
        return await asyncio.to_thread(func, *args, **kwargs)

    def _send_text(self, event: AstrMessageEvent, text: str):
        return event.plain_result(text)

    def _send_image(self, event: AstrMessageEvent, path: str | os.PathLike, text: str = ""):
        """发送本地图片, 可选附带说明文字."""
        path = str(path)
        if not os.path.exists(path):
            return self._send_text(event, f"图片不存在: {path}")
        chain: list = []
        if text:
            chain.append(Comp.Plain(text))
        chain.append(Comp.Image.fromFileSystem(path))
        return event.chain_result(chain)

    def _help_text(self) -> str:
        return (
            "📚 JM 漫画下载器 帮助\n"
            "------------------------\n"
            "所有命令以 /jm 开头\n"
            "\n"
            " 命令              | 别名      | 说明\n"
            " ----------------- | --------- | ----\n"
            " /jm help          | h         | 显示本帮助\n"
            " /jm search <词>   | sc        | 搜索本子\n"
            " /jm info <ID>     | if        | 查看本子详情 (附封面)\n"
            " /jm episodes <ID> | ep        | 列出本子的全部章节\n"
            " /jm photo <ID>    | ph        | 查看章节信息\n"
            " /jm download <ID> [选择] | d         | 下载本子/章节 (异步)\n"
            "   长本子会分批发送多条合并聊天记录\n"
            "   选择器示例:\n"
            "     all / 全部                 - 全部章节\n"
            "     1,3,5                      - 指定章节序号\n"
            "     1-10                       - 范围\n"
            "     1,3,5-10,15                - 混合格式\n"
            " /jm cover <ID>    | cv        | 仅获取本子封面\n"
            " /jm ranking [t]   | rk        | 排行榜, 默认 week\n"
            " /jm tags <标签>   | tg        | 按标签查询\n"
            " /jm status        | st        | 查看当前配置\n"
            " /jm reload        | re        | 重新加载配置 (管理)\n"
            "------------------------\n"
            "提示: 本子ID 与章节ID 均为纯数字, 可在禁漫网址栏中查看."
        )

    # ------------------------------------------------------------------ #
    # 章节选择器解析: "all" / "1,3,5-10" -> 索引列表 (1-based)
    # ------------------------------------------------------------------ #
    def _parse_selector(self, selector: str, total: int) -> list[int]:
        s = (selector or "").strip().lower()
        if not s or s in ("all", "全部", "*"):
            return list(range(1, total + 1))
        selected: set[int] = set()
        for part in s.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                try:
                    x, y = int(a), int(b)
                except ValueError:
                    continue
                if x > y:
                    x, y = y, x
                x = max(1, x)
                y = min(total, y)
                selected.update(range(x, y + 1))
            else:
                try:
                    n = int(part)
                except ValueError:
                    continue
                if 1 <= n <= total:
                    selected.add(n)
        return sorted(selected)

    # ================================================================== #
    # 命令组: /jm
    # ================================================================== #
    @filter.command_group("jm")
    def jm_group(self):
        """JM 漫画下载器命令组."""
        pass

    # ------------------------------------------------------------------ #
    # /jm help
    # ------------------------------------------------------------------ #
    @jm_group.command("help", alias={"帮助", "h"})
    async def jm_help(self, event: AstrMessageEvent):
        yield self._send_text(event, self._help_text())
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm status
    # ------------------------------------------------------------------ #
    @jm_group.command("status", alias={"状态", "配置", "st"})
    async def jm_status(self, event: AstrMessageEvent):
        cfg = self.config
        custom_dir = (cfg.get("custom_data_dir") or "").strip()
        data_dir_label = (
            f"{self.data_dir} (自定义覆盖)" if custom_dir else f"{self.data_dir} (默认)"
        )
        msg = (
            "⚙️ JM 插件配置\n"
            f"  客户端实现: {cfg.get('client_impl')}\n"
            f"  自定义域名: {cfg.get('custom_domain') or '未设置 (使用内置)'}\n"
            f"  代理: {'启用 ' + (cfg.get('proxy') or '系统代理') if cfg.get('use_proxy') else '关闭'}\n"
            f"  数据目录: {data_dir_label}\n"
            f"  下载目录: {self._resolve_download_dir()}\n"
            f"  图片并发: {cfg.get('image_thread_count')}, 章节并发: {cfg.get('photo_thread_count')}\n"
            f"  每批合并转发上限: {cfg.get('max_forward_images')} 张, "
            f"整本图片上限: {cfg.get('max_album_images') or '不限'}\n"
            f"  嵌套合并转发: {'开(已停用, QQ 分批发送)' if cfg.get('nested_forward') else '关'}\n"
            f"  图片后缀: {cfg.get('image_suffix') or '原格式'}\n"
            f"  登录: {'是' if cfg.get('enable_login') and cfg.get('username') else '否'}\n"
            f"  已登录: {'是' if self._logged_in else '否'}\n"
        )
        yield self._send_text(event, msg)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm reload (管理员)
    # ------------------------------------------------------------------ #
    @filter.permission_type(filter.PermissionType.ADMIN)
    @jm_group.command("reload", alias={"重载", "re"})
    async def jm_reload(self, event: AstrMessageEvent):
        try:
            await self._rebuild_option()
            self._logged_in = False
            yield self._send_text(event, "✅ JM 配置已重新加载")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] reload 失败: {e}", exc_info=True)
            yield self._send_text(event, f"❌ 重新加载失败: {e}")
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm search <关键词>
    # ------------------------------------------------------------------ #
    @jm_group.command("search", alias={"搜索", "sc"})
    async def jm_search(self, event: AstrMessageEvent, args: str = ""):
        keyword = (args or "").strip()
        if not keyword:
            yield self._send_text(event, "用法: /jm search <关键词>")
            event.stop_event()
            return

        yield self._send_text(event, f"🔍 正在搜索: {keyword} ...")

        try:
            option = await self._ensure_option()
            client = option.new_jm_client()
            await self._maybe_login(client)
            page = await self._run_blocking(client.search_site, search_query=keyword, page=1)
            results = list(page) if page else []
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 搜索失败: {e}", exc_info=True)
            yield self._send_text(event, f"❌ 搜索失败: {e}")
            event.stop_event()
            return

        max_n = max(1, int(self.config.get("max_search_results", 10) or 10))
        if not results:
            yield self._send_text(event, "未找到相关结果")
            event.stop_event()
            return

        lines = [f"🔍 搜索结果: {keyword} (共 {len(results)} 条, 显示前 {min(max_n, len(results))} 条)\n"]
        for i, (aid, title) in enumerate(results[:max_n], 1):
            title = (title or "").replace("\n", " ").strip()
            lines.append(f"  {i:>2}. [{aid}] {title}")
        lines.append("\n使用 /jm info <本子ID> 查看详情, /jm d <本子ID> 下载。")
        yield self._send_text(event, "\n".join(lines))
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm info <本子ID>
    # ------------------------------------------------------------------ #
    @jm_group.command("info", alias={"详情", "if"})
    async def jm_info(self, event: AstrMessageEvent, args: str = ""):
        aid = extract_id(args)
        if not aid:
            yield self._send_text(event, "用法: /jm info <本子ID>")
            event.stop_event()
            return

        yield self._send_text(event, f"📖 正在获取本子 {aid} 详情 ...")

        try:
            option = await self._ensure_option()
            client = option.new_jm_client()
            await self._maybe_login(client)
            album = await self._run_blocking(client.get_album_detail, aid)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 获取本子详情失败 {aid}: {e}", exc_info=True)
            yield self._send_text(event, f"❌ 获取详情失败: {e}")
            event.stop_event()
            return

        if album is None:
            yield self._send_text(event, f"本子 {aid} 不存在")
            event.stop_event()
            return

        info = (
            f"📖 本子详情 [{aid}]\n"
            f"  标题: {album.title}\n"
            f"  作者: {', '.join(album.author or []) or '未知'}\n"
            f"  章节数: {album.page_count}\n"
            f"  标签: {', '.join(album.tags or []) or '无'}\n"
            f"  浏览 / 喜欢 / 评论: {album.views} / {album.likes} / {album.comment_count}\n"
            f"  发布: {album.pub_date}  更新: {album.update_date}\n"
        )

        # 是否自动发封面
        if self.config.get("auto_send_cover", True):
            tmp_dir = self.data_dir / "covers"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            cover_path = tmp_dir / f"{aid}.jpg"
            try:
                await self._run_blocking(
                    client.download_album_cover, aid, str(cover_path)
                )
                if cover_path.exists():
                    yield self._send_image(event, cover_path, info)
                    event.stop_event()
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[JM] 下载封面失败 {aid}: {e}")
        yield self._send_text(event, info)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm cover <本子ID>
    # ------------------------------------------------------------------ #
    @jm_group.command("cover", alias={"封面", "cv"})
    async def jm_cover(self, event: AstrMessageEvent, args: str = ""):
        aid = extract_id(args)
        if not aid:
            yield self._send_text(event, "用法: /jm cover <本子ID>")
            event.stop_event()
            return

        try:
            option = await self._ensure_option()
            client = option.new_jm_client()
            await self._maybe_login(client)
            tmp_dir = self.data_dir / "covers"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            cover_path = tmp_dir / f"{aid}.jpg"
            await self._run_blocking(client.download_album_cover, aid, str(cover_path))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 下载封面失败 {aid}: {e}", exc_info=True)
            yield self._send_text(event, f"❌ 下载封面失败: {e}")
            event.stop_event()
            return

        if not cover_path.exists():
            yield self._send_text(event, f"未能获取本子 {aid} 的封面")
            event.stop_event()
            return
        yield self._send_image(event, cover_path, f"本子 {aid} 封面")
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm episodes <本子ID>
    # ------------------------------------------------------------------ #
    @jm_group.command("episodes", alias={"章节", "ep"})
    async def jm_episodes(self, event: AstrMessageEvent, args: str = ""):
        aid = extract_id(args)
        if not aid:
            yield self._send_text(event, "用法: /jm episodes <本子ID>")
            event.stop_event()
            return

        yield self._send_text(event, f"📑 正在获取本子 {aid} 章节列表 ...")

        try:
            option = await self._ensure_option()
            client = option.new_jm_client()
            await self._maybe_login(client)
            album = await self._run_blocking(client.get_album_detail, aid)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 获取章节列表失败 {aid}: {e}", exc_info=True)
            yield self._send_text(event, f"❌ 获取章节列表失败: {e}")
            event.stop_event()
            return

        if album is None:
            yield self._send_text(event, f"本子 {aid} 不存在")
            event.stop_event()
            return

        photos = list(album) if album else []
        if not photos:
            yield self._send_text(event, "本子无章节")
            event.stop_event()
            return

        lines = [f"📑 本子 [{aid}] {album.title} 共 {len(photos)} 章\n"]
        for i, photo in enumerate(photos, 1):
            try:
                pid = getattr(photo, "photo_id", "?")
                ptitle = getattr(photo, "title", "") or ""
            except Exception:  # noqa: BLE001
                pid, ptitle = "?", ""
            lines.append(f"  {i:>3}. [{pid}] {safe_filename(ptitle, 60)}")
        lines.append("\n使用 /jm d <本子ID> [选择器] 下载。")
        yield self._send_text(event, "\n".join(lines))
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm photo <章节ID>
    # ------------------------------------------------------------------ #
    @jm_group.command("photo", alias={"章节详情", "ph"})
    async def jm_photo(self, event: AstrMessageEvent, args: str = ""):
        pid = extract_id(args)
        if not pid:
            yield self._send_text(event, "用法: /jm photo <章节ID>")
            event.stop_event()
            return

        yield self._send_text(event, f"🖼 正在获取章节 {pid} 信息 ...")

        try:
            option = await self._ensure_option()
            client = option.new_jm_client()
            await self._maybe_login(client)
            photo = await self._run_blocking(client.get_photo_detail, pid, False)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 获取章节详情失败 {pid}: {e}", exc_info=True)
            yield self._send_text(event, f"❌ 获取章节详情失败: {e}")
            event.stop_event()
            return

        if photo is None:
            yield self._send_text(event, f"章节 {pid} 不存在")
            event.stop_event()
            return

        try:
            img_count = len(list(photo))
        except Exception:  # noqa: BLE001
            img_count = "?"

        msg = (
            f"🖼 章节详情 [{pid}]\n"
            f"  标题: {getattr(photo, 'title', '')}\n"
            f"  所属本子: {getattr(photo, 'album_id', '?')}\n"
            f"  图片数: {img_count}\n"
        )
        yield self._send_text(event, msg)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm download <ID> [选择器] (异步任务, 完成后主动推送)
    # ------------------------------------------------------------------ #
    @jm_group.command("download", alias={"下载", "d"})
    async def jm_download(self, event: AstrMessageEvent, args: str = ""):
        self._ensure_runtime_attrs()
        joined = (args or "").strip()
        aid = extract_id(joined)
        if not aid:
            yield self._send_text(
                event,
                "用法: /jm d <本子ID|章节ID> [选择器]\n"
                "  选择器: all / 1,3,5 / 1-10 / 1,3-5",
            )
            event.stop_event()
            return

        # 提取选择器: 去掉 ID 之后剩下的非空段
        rest = joined
        m = ID_PATTERN.search(rest)
        selector = "all"
        if m:
            rest = (rest[: m.start()] + rest[m.end():]).strip()
            if rest:
                selector = rest

        yield self._send_text(
            event,
            f"⏬ 已开始下载 ID {aid} (选择器: {selector})\n"
            f"任务在后台执行, 完成后会自动推送。",
        )

        umo = event.unified_msg_origin
        self_id = event.get_self_id()
        platform_id = event.get_platform_id()
        is_group = bool(event.get_group_id())
        session_id = event.get_group_id() or event.get_sender_id()

        async def _task():
            try:
                option = await self._ensure_option()
                client = option.new_jm_client()
                await self._maybe_login(client)

                # 先尝试作为 album_id, 失败再当作 photo_id
                target_photos: list = []
                target_album = None
                is_album = False
                try:
                    album = await self._run_blocking(client.get_album_detail, aid)
                    if album is None:
                        raise ValueError("album not found")
                    photo_list = list(album)
                    total = len(photo_list)
                    idx_list = self._parse_selector(selector, total)
                    if not idx_list:
                        await self._send_to(umo, f"❌ 选择器 {selector} 没有匹配到任何章节")
                        return
                    target_photos = [photo_list[i - 1] for i in idx_list]
                    target_album = album
                    is_album = True
                except Exception:  # noqa: BLE001
                    photo = await self._run_blocking(client.get_photo_detail, aid, False)
                    if photo is None:
                        await self._send_to(umo, f"❌ ID {aid} 不是有效的本子或章节 ID")
                        return
                    target_photos = [photo]
                    is_album = False

                # ------------------------------------------------------------------
                # 下载前缓存核对: 按 dir_rule 算出每个章节的真实落盘目录,
                # 已存在且含图片的章节跳过下载, 缺失的章节才走下载.
                # album_for_scan 用于精确扫描章节目录 (Atitle/Ptitle 都依赖它).
                # ------------------------------------------------------------------
                skip_if_cached = bool(self.config.get("skip_if_cached", True))
                album_for_scan = target_album
                cached_photos: list = []
                missing_photos: list = target_photos
                cached_images: list[Path] = []

                if skip_if_cached:
                    if is_album:
                        try:
                            cached_photos, missing_photos, cached_images = (
                                self._scan_cached_photos(option, target_album, target_photos)
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"[JM] 缓存核对失败, 回退全量下载: {e}")
                            missing_photos = target_photos
                    else:
                        # 单章节 fallback: photo.from_album 为 None, 若 dir_rule 含
                        # A* (如 Atitle) decide_image_save_dir 会在 getattr(None,...)
                        # 上抛异常. 这里取所属本子用于精确算目录; 失败则跳过缓存
                        # 检测走全量下载.
                        assoc_album = None
                        try:
                            assoc_album = await self._run_blocking(
                                client.get_album_detail, target_photos[0].album_id
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"[JM] 单章节缓存核对取所属本子失败, 跳过缓存检测: {e}")
                        if assoc_album is not None:
                            album_for_scan = assoc_album
                            try:
                                cached_photos, missing_photos, cached_images = (
                                    self._scan_cached_photos(
                                        option, assoc_album, target_photos
                                    )
                                )
                            except Exception as e:  # noqa: BLE001
                                logger.warning(f"[JM] 缓存核对失败, 回退全量下载: {e}")
                                missing_photos = target_photos

                photos_to_download = missing_photos
                all_cached = bool(cached_photos) and not photos_to_download

                # 下载前预查总图片数, 超过 max_album_images 直接拒绝, 避免误下长篇
                # (预查只针对实际需要下载的缺失章节; 已缓存章节不产生下载开销)
                # (预查的网络请求不计入下方 t0 的下载耗时)
                max_album_images = int(self.config.get("max_album_images", 0) or 0)
                if photos_to_download and max_album_images > 0:
                    try:
                        est_total = 0
                        for ph in photos_to_download:
                            # 优先用章节对象自身的图片数属性; 不确定时回退到迭代计数;
                            # 都拿不到时再请求详情 (额外网络开销, 仅兜底)
                            cnt = getattr(ph, "page_count", None)
                            if not cnt:
                                try:
                                    cnt = len(list(ph))
                                except Exception:  # noqa: BLE001
                                    det = await self._run_blocking(
                                        client.get_photo_detail, ph.photo_id, False
                                    )
                                    cnt = len(list(det)) if det else 0
                            est_total += int(cnt or 0)
                        if est_total > max_album_images:
                            await self._send_to(
                                umo,
                                f"❌ 本子 {aid} 需补下 {len(photos_to_download)} 章, "
                                f"共约 {est_total} 张图片, 超过上限 {max_album_images} 张, "
                                f"已取消下载。\n"
                                f"如需下载请在插件配置中调大 max_album_images, "
                                f"或用 /jm d <ID> <章节选择器> 分章节下载。",
                            )
                            return
                    except Exception as e:  # noqa: BLE001
                        # 预查失败不阻断主流程
                        logger.warning(f"[JM] 预查图片数失败, 跳过上限检查: {e}")

                # 下载前提示: 区分全部命中 / 部分命中 / 全部缺失
                if all_cached:
                    await self._send_to(
                        umo,
                        f"✅ 本子 {aid} 已存在于下载目录, 跳过下载, 直接推送 "
                        f"({len(target_photos)} 章, 共 {len(cached_images)} 张图片)",
                    )
                elif cached_photos:
                    await self._send_to(
                        umo,
                        f"⏳ 本子 {aid} 部分章节已缓存 "
                        f"({len(cached_photos)}/{len(target_photos)} 章), "
                        f"补下缺失 {len(photos_to_download)} 章 ...",
                    )
                else:
                    await self._send_to(
                        umo, f"⏳ 正在下载 {len(photos_to_download)} 个章节 ..."
                    )

                # ------------------------------------------------------------------
                # 下载 (仅缺失章节)
                # ------------------------------------------------------------------
                t0 = time.time()
                if photos_to_download:
                    def _do_download():
                        for ph in photos_to_download:
                            option.download_photo(ph.photo_id)

                    await self._run_blocking(_do_download)
                elapsed = time.time() - t0

                # ------------------------------------------------------------------
                # 扫描: 优先按精确章节目录扫描 (缓存 + 新下载), 失败再回退到
                # mtime 兜底扫描 (兼容 dir_rule 含 A* 但取不到 album 的场景).
                # ------------------------------------------------------------------
                new_images: list[Path] = []
                if photos_to_download:
                    try:
                        _, _, new_images = self._scan_cached_photos(
                            option, album_for_scan, photos_to_download
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[JM] 精确扫描新下载章节失败: {e}")
                        new_images = []

                merged: list[Path] = []
                seen: set[str] = set()
                for p in (*cached_images, *new_images):
                    key = str(p)
                    if key not in seen:
                        seen.add(key)
                        merged.append(p)
                downloaded: list[Path] = sorted(merged, key=self._path_sort_key)
                matched_root: Optional[Path] = None

                # 精确扫描拿不到又确实下载过 → 回退到 mtime 兜底扫描
                if not downloaded and photos_to_download:
                    # 1. 优先用 jmcomic option 的 dir_rule.base_dir 作为扫描根
                    #    (这是 jmcomic 实际写入的目录, 与 plugin_config 计算的路径
                    #     可能因容器卷挂载 / 软链接而出现差异)
                    primary_root: Optional[Path] = None
                    try:
                        base_dir_attr = getattr(option.dir_rule, "base_dir", None)
                        if base_dir_attr:
                            primary_root = Path(str(base_dir_attr))
                    except Exception:  # noqa: BLE001
                        primary_root = None

                    # 2. 兜底: 用插件自身计算的下载目录
                    fallback_root = self._resolve_download_dir()

                    # 第一轮优先找本次新增/修改的图片; 若 jmcomic 命中缓存,
                    # 文件 mtime 可能早于本次任务, 第二轮会回退到目录内全部图片.
                    mtime_window = t0 - 300  # 5 分钟

                    target_hints = [
                        str(getattr(album_for_scan, "title", "") or ""),
                        *[
                            str(getattr(ph, "title", "") or "")
                            for ph in photos_to_download
                        ],
                    ]
                    target_hints = [hint for hint in target_hints if hint]

                    def _is_image(path: Path) -> bool:
                        return path.suffix.lower() in IMAGE_SUFFIXES

                    def _filter_by_target_hints(paths: list[Path]) -> list[Path]:
                        if not target_hints:
                            return paths
                        filtered = [
                            p for p in paths
                            if any(hint in str(p) for hint in target_hints)
                        ]
                        return filtered or paths

                    def _scan(root: Path, recent_only: bool) -> list[Path]:
                        found: list[Path] = []
                        if not root or not root.exists():
                            return found
                        for p in root.rglob("*"):
                            if not p.is_file() or not _is_image(p):
                                continue
                            if recent_only and p.stat().st_mtime < mtime_window:
                                continue
                            found.append(p)
                        return _filter_by_target_hints(found)

                    roots_to_try = self._scan_roots(primary_root, fallback_root)
                    scan_mode = "recent"
                    for recent_only in (True, False):
                        for root in roots_to_try:
                            downloaded = _scan(root, recent_only)
                            if downloaded:
                                matched_root = root
                                scan_mode = "recent" if recent_only else "cached"
                                break
                        if downloaded:
                            break

                    logger.info(
                        f"[JM] 下载完成扫描(回退 mtime): primary={primary_root}, "
                        f"fallback={fallback_root}, data_dir={self.data_dir}, "
                        f"roots={roots_to_try}, matched={matched_root}, mode={scan_mode}, "
                        f"扫到 {len(downloaded)} 张图片"
                    )
                else:
                    # 精确扫描命中: 记录诊断, 并取本子根目录作为推送 base
                    logger.info(
                        f"[JM] 下载完成扫描(精确目录): "
                        f"cached={len(cached_images)}, new={len(new_images)}, "
                        f"merged={len(downloaded)} 张图片, all_cached={all_cached}"
                    )
                    try:
                        primary_root = None
                        base_dir_attr = getattr(option.dir_rule, "base_dir", None)
                        if base_dir_attr:
                            primary_root = Path(str(base_dir_attr))
                        matched_root = primary_root or self._resolve_download_dir()
                    except Exception:  # noqa: BLE001
                        matched_root = self._resolve_download_dir()

                # ------------------------------------------------------------------
                # 完成提示 + 推送
                # ------------------------------------------------------------------
                total_size = sum(p.stat().st_size for p in downloaded)
                if all_cached:
                    done_msg = (
                        f"✅ ID {aid} ({len(target_photos)} 章, 共 {len(downloaded)} 张图片, "
                        f"{fmt_size(total_size)}, 已缓存跳过下载)"
                    )
                else:
                    done_msg = (
                        f"✅ 下载完成: ID {aid} ({len(photos_to_download)} 章补下, "
                        f"共 {len(downloaded)} 张图片, {fmt_size(total_size)}, "
                        f"耗时 {elapsed:.1f}s)"
                    )
                await self._send_to(umo, done_msg)

                if downloaded:
                    base_for_forward = matched_root or self._resolve_download_dir()
                    try:
                        await self._send_images_as_forward(
                            platform_id,
                            session_id,
                            is_group,
                            self_id,
                            downloaded,
                            aid,
                            base_for_forward,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"[JM] 推送合并转发图集失败: {e}", exc_info=True)
                        await self._send_to(
                            umo,
                            f"❌ 推送合并转发图集失败: {e}\n"
                            f"图片已保留在: {base_for_forward}",
                        )
                else:
                    if all_cached:
                        # 已缓存但精确扫描未取到图片 (理论上不该发生)
                        await self._send_to(
                            umo,
                            f"⚠️ 本子 {aid} 判定为已缓存但未找到图片文件, "
                            f"请检查下载目录权限或重新下载。",
                        )
                    else:
                        tried = str(self._resolve_download_dir())
                        await self._send_to(
                            umo, f"⚠️ 未发现本次下载的图片文件, 尝试的目录: {tried}"
                        )
            except Exception as e:  # noqa: BLE001
                logger.error(f"[JM] 下载任务异常: {e}", exc_info=True)
                try:
                    await self._send_to(umo, f"❌ 下载任务异常: {e}")
                except Exception:  # noqa: BLE001
                    pass

        task = asyncio.create_task(_task())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm ranking [day|week|month]
    # ------------------------------------------------------------------ #
    @jm_group.command("ranking", alias={"排行榜", "rk"})
    async def jm_ranking(self, event: AstrMessageEvent, args: str = ""):
        rtype = (args or "").strip().split()[0] if (args or "").strip() else "week"
        rtype = rtype.lower()
        if rtype not in ("day", "week", "month"):
            rtype = "week"

        yield self._send_text(event, f"🏆 正在获取 {rtype} 排行榜 ...")

        try:
            option = await self._ensure_option()
            client = option.new_jm_client()
            method = {
                "day": getattr(client, "day_ranking", None),
                "week": getattr(client, "week_ranking", None),
                "month": getattr(client, "month_ranking", None),
            }.get(rtype)
            if method is None:
                yield self._send_text(event, "用法: /jm ranking [day|week|month]")
                event.stop_event()
                return
            page = await self._run_blocking(method, 1)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 获取排行榜失败: {e}", exc_info=True)
            yield self._send_text(event, f"❌ 获取排行榜失败: {e}")
            event.stop_event()
            return

        items = list(page) if page else []
        max_n = max(1, int(self.config.get("max_search_results", 10) or 10))
        if not items:
            yield self._send_text(event, "排行榜为空")
            event.stop_event()
            return

        lines = [f"🏆 {rtype} 排行榜 (前 {min(max_n, len(items))} 名)\n"]
        for i, (aid, title) in enumerate(items[:max_n], 1):
            title = (title or "").replace("\n", " ").strip()
            lines.append(f"  {i:>2}. [{aid}] {title}")
        yield self._send_text(event, "\n".join(lines))
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm tags <标签> [页码]
    # ------------------------------------------------------------------ #
    @jm_group.command("tags", alias={"标签", "tg"})
    async def jm_tags(self, event: AstrMessageEvent, args: str = ""):
        parts = (args or "").strip().split()
        if not parts:
            yield self._send_text(event, "用法: /jm tags <标签> [页码]")
            event.stop_event()
            return
        tag = parts[0]
        page_num = 1
        if len(parts) > 1:
            try:
                page_num = max(1, int(parts[1]))
            except ValueError:
                page_num = 1

        yield self._send_text(event, f"🏷 正在查询标签: {tag} (第 {page_num} 页) ...")

        try:
            option = await self._ensure_option()
            client = option.new_jm_client()
            page = await self._run_blocking(client.search_tag, tag, page=page_num)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 标签查询失败: {e}", exc_info=True)
            yield self._send_text(event, f"❌ 标签查询失败: {e}")
            event.stop_event()
            return

        max_n = max(1, int(self.config.get("max_search_results", 10) or 10))
        items: list[tuple[str, str]] = []
        try:
            for aid, title, _tags in page.iter_id_title_tag():
                items.append((aid, title))
        except Exception:  # noqa: BLE001
            items = list(page) if page else []

        if not items:
            yield self._send_text(event, "无结果")
            event.stop_event()
            return

        lines = [f"🏷 标签 [{tag}] 第 {page_num} 页 (显示前 {min(max_n, len(items))} 条)\n"]
        for i, (aid, title) in enumerate(items[:max_n], 1):
            title = (title or "").replace("\n", " ").strip()
            lines.append(f"  {i:>2}. [{aid}] {title}")
        yield self._send_text(event, "\n".join(lines))
        event.stop_event()

    # ================================================================== #
    # 工具方法
    # ================================================================== #
    async def _send_to(self, umo: str, text: str) -> None:
        from astrbot.api.event import MessageChain

        chain = MessageChain().message(text)
        await self.context.send_message(umo, chain)

    def _path_sort_key(self, path: Path) -> list[Any]:
        """自然排序路径, 避免 10.jpg 排在 2.jpg 前面."""
        try:
            text = str(path.relative_to(path.anchor))
        except ValueError:
            text = str(path)
        parts = re.split(r"(\d+)", text)
        return [int(part) if part.isdigit() else part.lower() for part in parts]

    async def _send_images_as_forward(
        self,
        platform_id: str,
        session_id: str,
        is_group: bool,
        self_id: str,
        files: Iterable[Path],
        aid: str,
        base_dir: Path,
    ) -> None:
        """通过 AstrBot 文件服务分批发送 QQ 合并转发图集。

        AstrBot v4.22.2 会把 ``Node`` 内的 ``Image``（包括 URL 图片）强制
        序列化为 ``base64://``。大量图片 Base64 会使 NapCat/QQ 生成无法展开的
        合并转发。因此这里将本地文件注册到 AstrBot 文件服务，直接调用 OneBot
        ``send_*_forward_msg`` 传递 HTTP URL，绕开 ``Node.to_dict()``。

        ``callback_api_base`` 必须指向 NapCat 容器可访问的 AstrBot 地址。
        相关实现：
        https://github.com/AstrBotDevs/AstrBot/blob/v4.22.2/astrbot/core/message/components.py
        https://github.com/NapNeko/NapCatQQ/blob/10f961c5529bcc4ac7bf53c4df0c749d766fc571/packages/napcat-onebot/action/msg/SendMsg.ts
        """
        images = sorted(
            [p for p in files if p.is_file()],
            key=self._path_sort_key,
        )
        if not images:
            return

        platform = self.context.get_platform_inst(platform_id)
        if platform is None:
            raise ForwardImageTransportError(
                f"找不到来源平台 {platform_id!r}，无法调用 QQ 合并转发 API"
            )
        client = platform.get_client()
        if not callable(getattr(client, "call_action", None)):
            raise ForwardImageTransportError(
                "当前平台不是支持 OneBot call_action 的 aiocqhttp 协议端"
            )

        # 每批大小: max_forward_images 是「每条合并转发」的图片上限
        # 0 = 不限制, 全部塞进单批 (用户显式选择, 自担 QQ 多消息大小限制的风险)
        raw_max = int(self.config.get("max_forward_images", 10) or 0)
        batch_size = len(images) if raw_max == 0 else max(1, raw_max)
        total = len(images)
        batch_count = (total + batch_size - 1) // batch_size

        if self.config.get("nested_forward", False) and batch_count > 1:
            logger.warning(
                f"[JM] nested_forward 已停用: 本子 {aid} 将按 QQ 分批合并转发 "
                f"({batch_count} 批), 避免构造嵌套合并转发导致发送慢或显示异常."
            )

        for idx, start in enumerate(range(0, total, batch_size), 1):
            batch = images[start : start + batch_size]
            try:
                image_urls = [
                    await self._register_forward_image_url(image_path)
                    for image_path in batch
                ]
            except Exception as exc:  # noqa: BLE001
                raise ForwardImageTransportError(
                    "无法把图片注册到 AstrBot 文件服务；请在插件或 AstrBot 全局配置设置 "
                    "callback_api_base 为 NapCat 容器可访问的 HTTP(S) 地址"
                ) from exc

            if not all(url.startswith(("http://", "https://")) for url in image_urls):
                raise ForwardImageTransportError(
                    "AstrBot 文件服务返回了非 HTTP(S) 地址；请检查 callback_api_base"
                )

            messages = [
                {
                    "type": "node",
                    "data": {
                        "user_id": str(self_id),
                        "nickname": "JM 漫画下载器",
                        "content": [
                            {
                                "type": "text",
                                "data": {
                                    "text": (
                                        f"JM 图集 [{aid}]\n"
                                        f"第 {idx}/{batch_count} 批, 共 {total} 张, "
                                        f"本批 {len(batch)} 张\n来源目录: {base_dir}"
                                    )
                                },
                            }
                        ],
                    },
                }
            ]
            for n, image_url in enumerate(image_urls, 1):
                page_number = start + n
                messages.append(
                    {
                        "type": "node",
                        "data": {
                            "user_id": str(self_id),
                            "nickname": f"{page_number}/{total}",
                            "content": [
                                {
                                    "type": "image",
                                    "data": {
                                        "file": image_url,
                                        "url": image_url,
                                    },
                                }
                            ],
                        },
                    }
                )

            if is_group:
                await client.call_action(
                    "send_group_forward_msg",
                    group_id=session_id,
                    messages=messages,
                )
            else:
                await client.call_action(
                    "send_private_forward_msg",
                    user_id=session_id,
                    messages=messages,
                )
            await asyncio.sleep(0)

    async def _register_forward_image_url(self, image_path: Path) -> str:
        """注册图片并允许插件配置覆盖 AstrBot 全局回调地址."""
        image = Image.fromFileSystem(str(image_path))
        callback_api_base = str(self.config.get("callback_api_base") or "").strip()
        if not callback_api_base:
            return await image.register_to_file_service()

        callback_api_base = callback_api_base.rstrip("/")
        try:
            from astrbot.core import file_token_service

            file_path = await image.convert_to_file_path()
            token = await file_token_service.register_file(file_path)
        except Exception as exc:  # noqa: BLE001
            raise ForwardImageTransportError(
                "无法使用插件配置的 callback_api_base 注册图片文件"
            ) from exc
        return f"{callback_api_base}/api/file/{token}"
