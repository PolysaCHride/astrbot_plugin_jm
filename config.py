from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Any
import yaml
import jmcomic
from astrbot.api import logger
from astrbot.api.star import StarTools

class ConfigService:
    def __init__(self, plugin):
        self.plugin = plugin
        self.data_dir = self.resolve_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.option = None
        self.lock = asyncio.Lock()

    @property
    def cfg(self): return self.plugin.config or {}

    def resolve_data_dir(self) -> Path:
        custom = str(self.cfg.get("custom_data_dir") or "").strip()
        if custom: return Path(custom).expanduser().resolve()
        try:
            try: return Path(StarTools.get_data_dir(self.plugin.name))
            except TypeError: return Path(StarTools.get_data_dir())
        except Exception:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            return Path(get_astrbot_data_path()) / "plugin_data" / self.plugin.name

    def download_dir(self) -> Path:
        sub = str(self.cfg.get("download_subdir") or "downloads").strip() or "downloads"
        base = self.data_dir.resolve(); result = (base / sub).resolve()
        if result != base and base not in result.parents:
            logger.warning("[JM] download_subdir escapes data directory; using downloads")
            result = base / "downloads"
        result.mkdir(parents=True, exist_ok=True)
        return result

    def option_dict(self) -> dict[str, Any]:
        cfg = self.cfg
        impl = str(cfg.get("client_impl") or "api").strip()
        if impl not in {"api", "html"}: impl = "api"
        domains = {}
        custom = str(cfg.get("custom_domain") or "").strip()
        if custom: domains[impl] = [x.strip() for x in custom.split(",") if x.strip()]
        proxy = str(cfg.get("proxy") or "").strip() if cfg.get("use_proxy") else None
        return {"client": {"impl": impl, "domain": domains,
            "retry_times": max(1, int(cfg.get("retry_times", 3) or 3)),
            "postman": {"meta_data": {"proxies": ({"http": proxy, "https": proxy} if proxy else "system"),
                "headers": {"User-Agent": "Mozilla/5.0 JM-AstrBot"}}}},
            "download": {"image": {"decode": True, "suffix": str(cfg.get("image_suffix") or "") or None},
                "threading": {"image": max(1, int(cfg.get("image_thread_count", 16) or 16)), "photo": max(1, int(cfg.get("photo_thread_count", 4) or 4))}},
            "dir_rule": {"rule": str(cfg.get("dir_rule") or "Bd / Atitle / Ptitle").strip()}, "log": False}

    async def rebuild(self):
        async with self.lock:
            data = self.option_dict(); data.setdefault("dir_rule", {})["base_dir"] = str(self.download_dir())
            option = None
            if hasattr(jmcomic, "create_option_by_dict"):
                try: option = jmcomic.create_option_by_dict(data)
                except Exception as exc: logger.warning(f"[JM] option dict failed: {exc}")
            if option is None:
                path = self.data_dir / ".option.yml"
                path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
                option = jmcomic.create_option_by_file(str(path))
            self.option = option
            return option
