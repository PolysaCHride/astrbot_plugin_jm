from __future__ import annotations
import asyncio
from astrbot.api import logger

class ClientService:
    def __init__(self, config): self.config = config; self.logged_in = False
    async def option(self):
        if self.config.option is None: await self.config.rebuild()
        return self.config.option
    async def call(self, func, *args, **kwargs): return await asyncio.to_thread(func, *args, **kwargs)
    async def new(self):
        option = await self.option(); client = option.new_jm_client()
        if self.config.cfg.get("enable_login") and not self.logged_in:
            user = str(self.config.cfg.get("username") or "").strip(); password = str(self.config.cfg.get("password") or "").strip()
            if user and password:
                try: await self.call(client.login, user, password); self.logged_in = True
                except Exception as exc: logger.error(f"[JM] login failed: {exc}")
        return client
