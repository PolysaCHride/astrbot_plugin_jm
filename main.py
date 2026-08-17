"""JM v2 AstrBot plugin entry point."""
from __future__ import annotations
import asyncio
import os
from pathlib import Path
import jmcomic
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
import astrbot.api.message_components as Comp
from config import ConfigService
from client import ClientService
from download import parse_selector, scan_cached_photos, unique_images
from forward import ForwardService
from help_card import help_text, render_help
from utils import extract_id, safe_filename, fmt_size, ID_PATTERN

class JMPlugin(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.context, self.config = context, config or {}
        self.runtime = ConfigService(self)
        self.clients = ClientService(self.runtime)
        self.forward = ForwardService(self)
        self._tasks = set()

    async def initialize(self):
        try: await self.runtime.rebuild()
        except Exception as exc: logger.error(f"[JM] 初始化失败: {exc}", exc_info=True)

    async def terminate(self):
        for task in tuple(self._tasks): task.cancel()
        if self._tasks: await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _text(self, event, text): return event.plain_result(text)
    def _image(self, event, path, caption=""):
        if not path or not os.path.exists(path): return self._text(event, caption or "图片不存在")
        chain = [Comp.Plain(caption)] if caption else []
        chain.append(Comp.Image.fromFileSystem(str(path)))
        return event.chain_result(chain)
    async def _push(self, origin, text):
        from astrbot.api.event import MessageChain
        await self.context.send_message(origin, MessageChain().message(text))
    async def _client(self): return await self.clients.new()

    def _help_text(self): return help_text()

    @filter.command_group("jm")
    def jm_group(self): pass

    @jm_group.command("help", alias={"h", "帮助"})
    async def jm_help(self, event: AstrMessageEvent):
        path = await render_help(self)
        yield self._image(event, path, "JM 漫画下载器帮助") if path else self._text(event, self._help_text())
        event.stop_event()

    @jm_group.command("status", alias={"st", "状态", "配置"})
    async def jm_status(self, event):
        cfg = self.config
        yield self._text(event, f"⚙️ JM 配置\n客户端: {cfg.get('client_impl','api')}\n下载目录: {self.runtime.download_dir()}\n每批图片: {cfg.get('max_forward_images',10)}\n缓存跳过: {cfg.get('skip_if_cached',True)}")
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @jm_group.command("reload", alias={"re", "重载"})
    async def jm_reload(self, event):
        try: await self.runtime.rebuild(); self.clients.logged_in = False; result = "✅ JM 配置已重新加载"
        except Exception as exc: result = f"❌ 重载失败: {exc}"
        yield self._text(event, result); event.stop_event()

    @jm_group.command("search", alias={"sc", "搜索"})
    async def jm_search(self, event, args=""):
        keyword = args.strip()
        if not keyword: yield self._text(event, "用法: /jm search <关键词>"); event.stop_event(); return
        try:
            page = await self.clients.call((await self._client()).search_site, search_query=keyword, page=1)
            items = list(page or [])[:max(1, int(self.config.get("max_search_results",10) or 10))]
            result = "🔍 搜索结果\n" + "\n".join(f"{i}. [{aid}] {title}" for i,(aid,title) in enumerate(items,1)) if items else "未找到结果"
        except Exception as exc: result = f"❌ 搜索失败: {exc}"
        yield self._text(event, result); event.stop_event()

    async def _album(self, aid): return await self.clients.call((await self._client()).get_album_detail, aid)

    @jm_group.command("info", alias={"if", "详情"})
    async def jm_info(self, event, args=""):
        aid = extract_id(args)
        if not aid: yield self._text(event,"用法: /jm info <本子ID>"); event.stop_event(); return
        try:
            album = await self._album(aid)
            if not album: raise ValueError("本子不存在")
            info = f"📖 [{aid}] {album.title}\n作者: {', '.join(album.author or [])}\n章节: {album.page_count}\n标签: {', '.join(album.tags or [])}"
            if self.config.get("auto_send_cover",True):
                path = self.runtime.data_dir / "covers" / f"{aid}.jpg"; path.parent.mkdir(parents=True,exist_ok=True)
                await self.clients.call((await self._client()).download_album_cover, aid, str(path))
                if path.exists(): yield self._image(event,path,info); event.stop_event(); return
            result = info
        except Exception as exc: result = f"❌ 获取详情失败: {exc}"
        yield self._text(event,result); event.stop_event()

    @jm_group.command("cover", alias={"cv", "封面"})
    async def jm_cover(self,event,args=""):
        aid=extract_id(args)
        if not aid: yield self._text(event,"用法: /jm cover <本子ID>"); event.stop_event(); return
        try:
            path=self.runtime.data_dir/"covers"/f"{aid}.jpg"; path.parent.mkdir(parents=True,exist_ok=True)
            await self.clients.call((await self._client()).download_album_cover,aid,str(path)); result=self._image(event,path,f"本子 {aid} 封面")
        except Exception as exc: result=self._text(event,f"❌ 下载封面失败: {exc}")
        yield result; event.stop_event()

    @jm_group.command("episodes", alias={"ep", "章节"})
    async def jm_episodes(self,event,args=""):
        aid=extract_id(args)
        if not aid: yield self._text(event,"用法: /jm episodes <本子ID>"); event.stop_event(); return
        try:
            album=await self._album(aid); photos=list(album or [])
            result=f"📑 {album.title} 共 {len(photos)} 章\n"+"\n".join(f"{i}. [{p.photo_id}] {safe_filename(p.title,60)}" for i,p in enumerate(photos,1))
        except Exception as exc: result=f"❌ 获取章节失败: {exc}"
        yield self._text(event,result); event.stop_event()

    @jm_group.command("photo", alias={"ph", "章节详情"})
    async def jm_photo(self,event,args=""):
        pid=extract_id(args)
        if not pid: yield self._text(event,"用法: /jm photo <章节ID>"); event.stop_event(); return
        try:
            photo=await self.clients.call((await self._client()).get_photo_detail,pid,False); result=f"🖼 [{pid}] {photo.title}\n图片数: {len(list(photo))}"
        except Exception as exc: result=f"❌ 获取章节失败: {exc}"
        yield self._text(event,result); event.stop_event()

    @jm_group.command("download", alias={"d", "下载"})
    async def jm_download(self,event,args=""):
        joined=(args or "").strip(); aid=extract_id(joined)
        if not aid: yield self._text(event,"用法: /jm d <本子ID|章节ID> [选择器]"); event.stop_event(); return
        rest=joined.replace(aid,"",1).strip() or "all"; origin=event.unified_msg_origin
        yield self._text(event,f"⏬ 已开始下载 {aid}，完成后自动推送")
        async def task():
            try:
                option=await self.clients.option(); client=await self._client(); album=await self.clients.call(client.get_album_detail,aid)
                is_album=album is not None
                if is_album:
                    photos=list(album); selected=parse_selector(rest,len(photos)); targets=[photos[i-1] for i in selected]
                else: targets=[await self.clients.call(client.get_photo_detail,aid,False)]; album=None
                if not targets: await self._push(origin,"❌ 没有匹配的章节"); return
                cached, missing, images=scan_cached_photos(option,album,targets) if self.config.get("skip_if_cached",True) and is_album else ([],targets,[])
                start=asyncio.get_running_loop().time()
                for photo in missing: await self.clients.call(option.download_photo,photo.photo_id)
                if missing:
                    _,_,new=scan_cached_photos(option,album,missing) if is_album else ([],[],[]); images=unique_images([*images,*new])
                await self._push(origin,f"✅ 下载完成，共 {len(images)} 张，耗时 {asyncio.get_running_loop().time()-start:.1f}s")
                if images: await self.forward.send(event.get_platform_id(),event.get_group_id() or event.get_sender_id(),bool(event.get_group_id()),event.get_self_id(),images,aid,self.runtime.download_dir())
            except Exception as exc: logger.error(f"[JM] 下载任务失败: {exc}",exc_info=True); await self._push(origin,f"❌ 下载失败: {exc}")
        job=asyncio.create_task(task()); self._tasks.add(job); job.add_done_callback(self._tasks.discard); event.stop_event()

    @jm_group.command("ranking", alias={"rk", "排行榜"})
    async def jm_ranking(self,event,args=""):
        kind=(args or "week").strip().split()[0]; kind=kind if kind in {"day","week","month"} else "week"
        try: items=list(await self.clients.call(getattr(await self._client(),f"{kind}_ranking"),1) or []); result="🏆 排行榜\n"+"\n".join(f"{i}. [{a}] {t}" for i,(a,t) in enumerate(items[:10],1))
        except Exception as exc: result=f"❌ 获取排行失败: {exc}"
        yield self._text(event,result); event.stop_event()

    @jm_group.command("tags", alias={"tg", "标签"})
    async def jm_tags(self,event,args=""):
        parts=(args or "").split(); tag=parts[0] if parts else ""
        if not tag: yield self._text(event,"用法: /jm tags <标签> [页码]"); event.stop_event(); return
        page=max(1,int(parts[1])) if len(parts)>1 and parts[1].isdigit() else 1
        try:
            data=await self.clients.call((await self._client()).search_tag,tag,page=page); items=list(data or [])[:10]; result="🏷 标签结果\n"+"\n".join(f"{i}. [{a}] {t}" for i,(a,t,*_) in enumerate(items,1))
        except Exception as exc: result=f"❌ 标签查询失败: {exc}"
        yield self._text(event,result); event.stop_event()
