from __future__ import annotations
import asyncio
from pathlib import Path
from urllib.parse import urlparse
from astrbot.core.message.components import Image
from astrbot.core import file_token_service

class ForwardImageTransportError(RuntimeError): pass

class ForwardService:
    def __init__(self, plugin): self.plugin = plugin
    async def register(self, path: Path) -> str:
        image = Image.fromFileSystem(str(path)); override = str(self.plugin.config.get("callback_api_base") or "").strip()
        if override:
            token = await file_token_service.register_file(await image.convert_to_file_path())
            url = override.rstrip("/") + "/api/file/" + str(token)
        else: url = await image.register_to_file_service()
        if urlparse(str(url)).scheme not in {"http", "https"}: raise ForwardImageTransportError("文件服务返回了非 HTTP(S) 地址")
        return str(url)
    async def send(self, platform_id, session_id, is_group, self_id, files, aid, base_dir):
        from download import unique_images
        images = unique_images(files)
        if not images: return
        platform = self.plugin.context.get_platform_inst(platform_id)
        client = platform.get_client() if platform else None
        if not client or not callable(getattr(client, "call_action", None)): raise ForwardImageTransportError("当前平台不支持 OneBot call_action")
        maximum = int(self.plugin.config.get("max_forward_images", 10) or 0); size = len(images) if maximum == 0 else max(1, maximum)
        batches = [images[i:i+size] for i in range(0, len(images), size)]
        for batch_no, batch in enumerate(batches, 1):
            urls = await asyncio.gather(*(self.register(p) for p in batch))
            messages = [{"type":"node","data":{"user_id":str(self_id),"nickname":"JM 漫画下载器","content":[{"type":"text","data":{"text":f"JM 图集 [{aid}] 第 {batch_no}/{len(batches)} 批\n来源目录: {base_dir}"}}]}}]
            messages += [{"type":"node","data":{"user_id":str(self_id),"nickname":f"{(batch_no-1)*size+i+1}/{len(images)}","content":[{"type":"image","data":{"file":url,"url":url}}]}} for i,url in enumerate(urls)]
            action = "send_group_forward_msg" if is_group else "send_private_forward_msg"
            payload = {"group_id":session_id} if is_group else {"user_id":session_id}; payload["messages"] = messages
            await client.call_action(action, **payload)
