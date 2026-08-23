"""astrbot_plugin_jm v2 - 基于 JMComic-Crawler-Python 的禁漫搜索 / 详情 / 下载插件。

v2 重构说明:
- 单文件 main.py 拆分为 jm_plugin 包 (config / client / scan / forward /
  help_card / download / utils), 命令入口保持薄封装;
- 合并转发图片改用 file:/// 本地路径直传 NapCat (base64 可配置), 修复
  「该类消息类型暂不支持查看」并大幅加快打包速度;
- /jm help 渲染 PIL markdown 卡片, 失败自动回退纯文本。
"""

from __future__ import annotations

import asyncio
import os

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .jm_plugin.client import ClientService
from .jm_plugin.config import ConfigService
from .jm_plugin.download import DownloadService
from .jm_plugin.forward import ForwardService
from .jm_plugin.help_card import help_markdown, help_text, render_help_card
from .jm_plugin.scan import PhotoScanner
from .jm_plugin.utils import ID_PATTERN, extract_id, safe_filename


class JMPlugin(Star):
    """JM 漫画下载器插件。

    使用 command_group("jm") 组织 /jm 系列子命令:
    help, status, reload, search, info, cover, episodes, photo, download,
    ranking, tags。
    """

    def __init__(self, context: Context, config) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}

        self.config_service = ConfigService(self.name, self.config)
        self.client_service = ClientService(self.config_service, self.config)
        self.scanner = PhotoScanner(self.config_service)
        self.forwarder = ForwardService(self.config_service, self.config, context)
        self.downloader = DownloadService(
            self.config_service, self.client_service, self.scanner, self.forwarder
        )
        self._background_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        """AstrBot 加载插件后调用: 提前构建 option, 尽早暴露配置错误."""
        try:
            await self.config_service.rebuild_option(initial=True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 初始化 option 失败: {e}", exc_info=True)
        logger.info(f"[JM] 插件已加载, 数据目录: {self.config_service.data_dir}")

    async def terminate(self) -> None:
        """插件卸载时取消全部后台任务."""
        for task in tuple(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
            self._background_tasks.clear()
        logger.info("[JM] 插件已卸载")

    def _spawn_task(self, coro) -> None:
        """登记后台任务, 任务结束后自动清理."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ------------------------------------------------------------------ #
    # 消息发送辅助
    # ------------------------------------------------------------------ #
    def _send_text(self, event: AstrMessageEvent, text: str):
        return event.plain_result(text)

    def _send_image(self, event: AstrMessageEvent, path, text: str = ""):
        """发送本地图片, 可选附带说明文字."""
        path = str(path)
        if not os.path.exists(path):
            return self._send_text(event, f"图片不存在: {path}")
        chain: list = []
        if text:
            chain.append(Comp.Plain(text))
        chain.append(Comp.Image.fromFileSystem(path))
        return event.chain_result(chain)

    async def _push_text(self, umo: str, text: str) -> None:
        """向来源会话主动推送文本 (后台任务完成通知用)."""
        from astrbot.api.event import MessageChain

        await self.context.send_message(umo, MessageChain().message(text))

    # ------------------------------------------------------------------ #
    # jmcomic 调用辅助
    # ------------------------------------------------------------------ #
    async def _call(self, method: str, *args, **kwargs):
        """在后台线程执行 jmcomic client 方法 (自动建 option 与登录)."""
        option = await self.client_service.ensure_option()
        client = self.client_service.new_client(option)
        await self.client_service.maybe_login(client)
        return await self.client_service.run_blocking(
            getattr(client, method), *args, **kwargs
        )

    def _result_lines(self, title: str, items, max_n: int) -> str:
        """搜索结果通用格式化 (序号. [ID] 标题)."""
        if not items:
            return "未找到相关结果"
        lines = [f"{title} (显示前 {min(max_n, len(items))} 条)"]
        for i, (aid, title_text) in enumerate(items[:max_n], 1):
            title_text = (title_text or "").replace("\n", " ").strip()
            lines.append(f"  {i:>2}. [{aid}] {title_text}")
        lines.append("使用 /jm info <本子ID> 查看详情, /jm d <本子ID> 下载。")
        return "".join(line + chr(10) for line in lines)

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
        md = help_markdown()
        card = await asyncio.to_thread(
            render_help_card, self.config_service.data_dir / "cards", md
        )
        if card:
            yield self._send_image(event, card)
        else:
            yield self._send_text(event, help_text())
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm status
    # ------------------------------------------------------------------ #
    @jm_group.command("status", alias={"状态", "配置", "st"})
    async def jm_status(self, event: AstrMessageEvent):
        cfg = self.config_service
        custom_dir = cfg.get_str("custom_data_dir")
        data_dir_label = (
            f"{cfg.data_dir} (自定义覆盖)" if custom_dir else f"{cfg.data_dir} (默认)"
        )
        msg = "\n".join(
            [
                "JM 插件配置",
                f"  客户端实现: {cfg.get_str('client_impl', 'api')}",
                f"  自定义域名: {cfg.get_str('custom_domain') or '未设置 (使用内置)'}",
                (
                    "  代理: 启用 " + (cfg.get_str("proxy") or "系统代理")
                    if cfg.get_bool("use_proxy")
                    else "  代理: 关闭"
                ),
                f"  数据目录: {data_dir_label}",
                f"  下载目录: {cfg.download_dir()}",
                f"  图片并发: {cfg.get_int('image_thread_count', 16)}, 章节并发: {cfg.get_int('photo_thread_count', 4)}",
                f"  每批合并转发上限: {cfg.get_int('max_forward_images', 10)} 张, 传输方式: {self.forwarder.transport}",
                f"  整本图片上限: {cfg.get_int('max_album_images', 0) or '不限'}",
                f"  缓存跳过: {'是' if cfg.get_bool('skip_if_cached', True) else '否'}",
                f"  图片后缀: {cfg.get_str('image_suffix') or '原格式'}",
                f"  登录: {'是' if cfg.get_bool('enable_login') and cfg.get_str('username') else '否'}",
                f"  已登录: {'是' if self.client_service.logged_in else '否'}",
            ]
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
            await self.config_service.rebuild_option()
            self.client_service.reset_login()
            yield self._send_text(event, "JM 配置已重新加载")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] reload 失败: {e}", exc_info=True)
            yield self._send_text(event, f"重新加载失败: {e}")
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

        yield self._send_text(event, f"正在搜索: {keyword} ...")
        try:
            page = await self._call("search_site", search_query=keyword, page=1)
            results = list(page) if page else []
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 搜索失败: {e}", exc_info=True)
            yield self._send_text(event, f"搜索失败: {e}")
            event.stop_event()
            return

        max_n = max(1, self.config_service.get_int("max_search_results", 10))
        yield self._send_text(
            event,
            self._result_lines(f"搜索结果: {keyword} (共 {len(results)} 条)", results, max_n),
        )
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

        yield self._send_text(event, f"正在获取本子 {aid} 详情 ...")
        try:
            album = await self._call("get_album_detail", aid)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 获取本子详情失败 {aid}: {e}", exc_info=True)
            yield self._send_text(event, f"获取详情失败: {e}")
            event.stop_event()
            return
        if album is None:
            yield self._send_text(event, f"本子 {aid} 不存在")
            event.stop_event()
            return

        info = "\n".join(
            [
                f"本子详情 [{aid}]",
                f"  标题: {album.title}",
                f"  作者: {', '.join(album.author or []) or '未知'}",
                f"  章节数: {album.page_count}",
                f"  标签: {', '.join(album.tags or []) or '无'}",
                f"  浏览 / 喜欢 / 评论: {album.views} / {album.likes} / {album.comment_count}",
                f"  发布: {album.pub_date}  更新: {album.update_date}",
            ]
        )

        if self.config_service.get_bool("auto_send_cover", True):
            tmp_dir = self.config_service.data_dir / "covers"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            cover_path = tmp_dir / f"{aid}.jpg"
            try:
                await self._call("download_album_cover", aid, str(cover_path))
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
            tmp_dir = self.config_service.data_dir / "covers"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            cover_path = tmp_dir / f"{aid}.jpg"
            await self._call("download_album_cover", aid, str(cover_path))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 下载封面失败 {aid}: {e}", exc_info=True)
            yield self._send_text(event, f"下载封面失败: {e}")
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

        yield self._send_text(event, f"正在获取本子 {aid} 章节列表 ...")
        try:
            album = await self._call("get_album_detail", aid)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 获取章节列表失败 {aid}: {e}", exc_info=True)
            yield self._send_text(event, f"获取章节列表失败: {e}")
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

        lines = [f"本子 [{aid}] {album.title} 共 {len(photos)} 章"]
        for i, photo in enumerate(photos, 1):
            try:
                pid = getattr(photo, "photo_id", "?")
                ptitle = getattr(photo, "title", "") or ""
            except Exception:  # noqa: BLE001
                pid, ptitle = "?", ""
            lines.append(f"  {i:>3}. [{pid}] {safe_filename(ptitle, 60)}")
        lines.append("使用 /jm d <本子ID> [选择器] 下载。")
        yield self._send_text(event, "".join(line + chr(10) for line in lines))
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

        yield self._send_text(event, f"正在获取章节 {pid} 信息 ...")
        try:
            photo = await self._call("get_photo_detail", pid, False)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 获取章节详情失败 {pid}: {e}", exc_info=True)
            yield self._send_text(event, f"获取章节详情失败: {e}")
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

        msg = "\n".join(
            [
                f"章节详情 [{pid}]",
                f"  标题: {getattr(photo, 'title', '')}",
                f"  所属本子: {getattr(photo, 'album_id', '?')}",
                f"  图片数: {img_count}",
            ]
        )
        yield self._send_text(event, msg)
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm download <ID> [选择器] (异步任务, 完成后主动推送)
    # ------------------------------------------------------------------ #
    @jm_group.command("download", alias={"下载", "d"})
    async def jm_download(self, event: AstrMessageEvent, args: str = ""):
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
        selector = "all"
        m = ID_PATTERN.search(joined)
        if m:
            rest = (joined[: m.start()] + joined[m.end():]).strip()
            if rest:
                selector = rest

        yield self._send_text(
            event,
            f"已开始下载 ID {aid} (选择器: {selector})\n"
            "任务在后台执行, 完成后会自动推送。",
        )

        umo = event.unified_msg_origin
        self_id = event.get_self_id()
        platform_id = event.get_platform_id()
        is_group = bool(event.get_group_id())
        session_id = event.get_group_id() or event.get_sender_id()

        self._spawn_task(
            self.downloader.run(
                aid, selector, umo, platform_id, session_id, is_group, self_id,
                push=self._push_text,
            )
        )
        event.stop_event()

    # ------------------------------------------------------------------ #
    # /jm ranking [day|week|month]
    # ------------------------------------------------------------------ #
    @jm_group.command("ranking", alias={"排行榜", "rk"})
    async def jm_ranking(self, event: AstrMessageEvent, args: str = ""):
        rtype = (args or "").strip().split()[0].lower() if (args or "").strip() else "week"
        if rtype not in ("day", "week", "month"):
            rtype = "week"

        yield self._send_text(event, f"正在获取 {rtype} 排行榜 ...")
        try:
            page = await self._call(f"{rtype}_ranking", 1)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 获取排行榜失败: {e}", exc_info=True)
            yield self._send_text(event, f"获取排行榜失败: {e}")
            event.stop_event()
            return

        items = list(page) if page else []
        max_n = max(1, self.config_service.get_int("max_search_results", 10))
        yield self._send_text(
            event,
            self._result_lines(f"{rtype} 排行榜 (前 {min(max_n, len(items))} 名)", items, max_n),
        )
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

        yield self._send_text(event, f"正在查询标签: {tag} (第 {page_num} 页) ...")
        try:
            page = await self._call("search_tag", tag, page=page_num)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[JM] 标签查询失败: {e}", exc_info=True)
            yield self._send_text(event, f"标签查询失败: {e}")
            event.stop_event()
            return

        items: list = []
        try:
            for aid, title, _tags in page.iter_id_title_tag():
                items.append((aid, title))
        except Exception:  # noqa: BLE001
            items = list(page) if page else []

        max_n = max(1, self.config_service.get_int("max_search_results", 10))
        yield self._send_text(
            event,
            self._result_lines(f"标签 [{tag}] 第 {page_num} 页", items, max_n),
        )
        event.stop_event()
