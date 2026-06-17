"""astrbot_plugin_jm - 基于 JMComic-Crawler-Python 的禁漫搜索 / 详情 / 下载插件"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools

import jmcomic

# 本子 / 章节 ID 都是纯数字字符串
ID_PATTERN = re.compile(r"\d{4,}")


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
    download, ranking, tags.
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config

        # 数据目录 (绝对路径, 由 StarTools 自动创建)
        # 注: v4.22.2 中 Context.get_data_dir() 不存在, 应使用 StarTools.get_data_dir()
        #     (PR #1194 引入的标准化接口)
        try:
            self.data_dir: Path = Path(StarTools.get_data_dir())
        except Exception:  # noqa: BLE001
            # 兜底: 拼接标准路径 (兼容无 StarTools.get_data_dir 的旧版本)
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 启动时构建 jmcomic option
        self.option: Optional[jmcomic.JmOption] = None
        self._option_lock = asyncio.Lock()
        self._logged_in: bool = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        """AstrBot 在加载本插件后会调用一次."""
        try:
            await self._rebuild_option(initial=True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 初始化 option 失败: {e}", exc_info=True)
        logger.info(f"[JM] 插件已加载, 数据目录: {self.data_dir}")

    async def terminate(self) -> None:
        """插件卸载时调用."""
        logger.info("[JM] 插件已卸载")

    # ------------------------------------------------------------------ #
    # Option 管理
    # ------------------------------------------------------------------ #
    async def _rebuild_option(self, initial: bool = False) -> jmcomic.JmOption:
        """根据当前 plugin_config 重建 jmcomic option."""
        async with self._option_lock:
            opt = self._build_option_dict()
            download_dir = self._resolve_download_dir()
            opt.setdefault("dir_rule", {})
            opt["dir_rule"]["base_dir"] = str(download_dir)
            option = jmcomic.create_option_by_dict(opt)
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
        d = self.data_dir / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _ensure_option(self) -> jmcomic.JmOption:
        if self.option is None:
            await self._rebuild_option()
        assert self.option is not None
        return self.option

    async def _maybe_login(self, client) -> None:
        if not self.config.get("enable_login", False) or self._logged_in:
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
            "/jm help                       - 显示本帮助\n"
            "/jm search <关键词>            - 搜索本子\n"
            "/jm info <本子ID>              - 查看本子详情 (附封面)\n"
            "/jm episodes <本子ID>          - 列出本子的全部章节\n"
            "/jm photo <章节ID>             - 查看章节信息\n"
            "/jm download <ID> [选择器]     - 下载本子/章节 (异步)\n"
            "   选择器示例:\n"
            "     all / 全部                 - 全部章节\n"
            "     1,3,5                      - 指定章节序号\n"
            "     1-10                       - 范围\n"
            "     1,3,5-10,15                - 混合格式\n"
            "/jm cover <本子ID>             - 仅获取本子封面\n"
            "/jm ranking [day|week|month]   - 排行榜, 默认 week\n"
            "/jm tags <标签> [页码]         - 按标签查询\n"
            "/jm status                     - 查看当前配置\n"
            "/jm reload                     - 重新加载配置 (管理)\n"
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
    @jm_group.command("status", alias={"状态", "配置"})
    async def jm_status(self, event: AstrMessageEvent):
        cfg = self.config
        msg = (
            "⚙️ JM 插件配置\n"
            f"  客户端实现: {cfg.get('client_impl')}\n"
            f"  自定义域名: {cfg.get('custom_domain') or '未设置 (使用内置)'}\n"
            f"  代理: {'启用 ' + (cfg.get('proxy') or '系统代理') if cfg.get('use_proxy') else '关闭'}\n"
            f"  下载目录: {self._resolve_download_dir()}\n"
            f"  图片并发: {cfg.get('image_thread_count')}, 章节并发: {cfg.get('photo_thread_count')}\n"
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
    @jm_group.command("reload", alias={"重载"})
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
    @jm_group.command("search", alias={"搜索", "s"})
    async def jm_search(self, event: AstrMessageEvent, *args: str):
        keyword = " ".join(a for a in args if a).strip()
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
        lines.append("\n使用 /jm info <本子ID> 查看详情, /jm download <本子ID> 下载。")
        yield self._send_text(event, "\n".join(lines))
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm info <本子ID>
    # ------------------------------------------------------------------ #
    @jm_group.command("info", alias={"详情", "i"})
    async def jm_info(self, event: AstrMessageEvent, *args: str):
        aid = extract_id(" ".join(args))
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
    @jm_group.command("cover", alias={"封面", "c"})
    async def jm_cover(self, event: AstrMessageEvent, *args: str):
        aid = extract_id(" ".join(args))
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
    @jm_group.command("episodes", alias={"章节", "e"})
    async def jm_episodes(self, event: AstrMessageEvent, *args: str):
        aid = extract_id(" ".join(args))
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
        lines.append("\n使用 /jm download <本子ID> [选择器] 下载。")
        yield self._send_text(event, "\n".join(lines))
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm photo <章节ID>
    # ------------------------------------------------------------------ #
    @jm_group.command("photo", alias={"章节详情", "p"})
    async def jm_photo(self, event: AstrMessageEvent, *args: str):
        pid = extract_id(" ".join(args))
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
    async def jm_download(self, event: AstrMessageEvent, *args: str):
        joined = " ".join(args).strip()
        aid = extract_id(joined)
        if not aid:
            yield self._send_text(
                event,
                "用法: /jm download <本子ID|章节ID> [选择器]\n"
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

                t0 = time.time()
                await self._send_to(umo, f"⏳ 正在下载 {len(target_photos)} 个章节 ...")

                def _do_download():
                    for ph in target_photos:
                        option.download_photo(ph.photo_id)

                await self._run_blocking(_do_download)
                elapsed = time.time() - t0

                # 统计本次时间窗内新增的图片, 不依赖 dir_rule
                root = self._resolve_download_dir()
                img_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
                downloaded: list[Path] = []
                if root.exists():
                    for p in root.rglob("*"):
                        if (
                            p.is_file()
                            and p.suffix.lower() in img_suffixes
                            and p.stat().st_mtime >= t0 - 1
                        ):
                            downloaded.append(p)

                total_size = sum(p.stat().st_size for p in downloaded)
                await self._send_to(
                    umo,
                    f"✅ 下载完成: ID {aid} ({len(target_photos)} 章, 共 {len(downloaded)} 张图片, "
                    f"{fmt_size(total_size)}, 耗时 {elapsed:.1f}s)",
                )

                if downloaded:
                    zip_path = await self._zip_to_send(downloaded, root, aid)
                    if zip_path:
                        chain = [
                            Comp.Plain(f"📦 已打包: {os.path.basename(zip_path)}"),
                            Comp.File(file=str(zip_path), name=os.path.basename(zip_path)),
                        ]
                        try:
                            await self.context.send_message(umo, chain)
                        except Exception as e:  # noqa: BLE001
                            logger.error(f"[JM] 推送 zip 失败: {e}")
                            await self._send_to(umo, f"❌ 推送文件失败: {e}")
                        finally:
                            try:
                                os.remove(zip_path)
                            except OSError:
                                pass
                    else:
                        await self._send_to(umo, f"📁 下载目录: {root}")
                else:
                    await self._send_to(umo, f"⚠️ 未发现本次下载的图片文件, 请检查目录: {root}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"[JM] 下载任务异常: {e}", exc_info=True)
                try:
                    await self._send_to(umo, f"❌ 下载任务异常: {e}")
                except Exception:  # noqa: BLE001
                    pass

        asyncio.create_task(_task())
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm ranking [day|week|month]
    # ------------------------------------------------------------------ #
    @jm_group.command("ranking", alias={"排行榜", "r"})
    async def jm_ranking(self, event: AstrMessageEvent, *args: str):
        rtype = (args[0] if args else "week").strip().lower()
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
    @jm_group.command("tags", alias={"标签", "t"})
    async def jm_tags(self, event: AstrMessageEvent, *args: str):
        if not args:
            yield self._send_text(event, "用法: /jm tags <标签> [页码]")
            event.stop_event()
            return
        tag = args[0]
        page_num = 1
        if len(args) > 1:
            try:
                page_num = max(1, int(args[1]))
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

    async def _zip_to_send(self, files: Iterable[Path], base_dir: Path, aid: str) -> Optional[str]:
        """把下载的图片打包成 zip 返回临时路径, 失败返回 None."""
        try:
            tmp = Path(tempfile.gettempdir())
            zip_path = tmp / f"jm_{aid}_{int(time.time())}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in files:
                    if not p.exists():
                        continue
                    try:
                        arcname = str(p.relative_to(base_dir))
                    except ValueError:
                        arcname = p.name
                    zf.write(p, arcname)
            if not zip_path.exists() or zip_path.stat().st_size == 0:
                return None
            return str(zip_path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 打包 zip 失败: {e}", exc_info=True)
            return None
