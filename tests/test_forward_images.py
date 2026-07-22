"""Regression tests for the QQ merge-forward image transport."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path


def _load_plugin_module():
    """Import main.py with lightweight AstrBot/JMComic test doubles."""
    module_name = "jm_plugin_under_test"
    sys.modules.pop(module_name, None)

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    api.logger = types.SimpleNamespace(debug=lambda *_: None, info=lambda *_: None)

    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = lambda text: ("plain", text)
    components.Image = types.SimpleNamespace(fromFileSystem=lambda path: ("image", path))

    class _CommandGroup:
        def command(self, *_args, **_kwargs):
            return lambda func: func

    class _Filter:
        @staticmethod
        def command_group(*_args, **_kwargs):
            def _decorate(func):
                func.command = _CommandGroup().command
                return func

            return _decorate

        @staticmethod
        def permission_type(*_args, **_kwargs):
            return lambda func: func

        class PermissionType:
            ADMIN = "admin"

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = _Filter()

    star = types.ModuleType("astrbot.api.star")

    class _Star:
        def __init__(self, *_args, **_kwargs):
            pass

    star.Context = object
    star.Star = _Star
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda *_: ".")

    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    core_message = types.ModuleType("astrbot.core.message")
    core_message.__path__ = []
    core_components = types.ModuleType("astrbot.core.message.components")

    class _UnusedComponent:
        def __init__(self, *_args, **_kwargs):
            pass

    core_components.Image = _UnusedComponent

    core_config = types.ModuleType("astrbot.core.config")
    core_config.AstrBotConfig = dict

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.message_components": components,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.core": core,
            "astrbot.core.message": core_message,
            "astrbot.core.message.components": core_components,
            "astrbot.core.config": core_config,
            "jmcomic": types.ModuleType("jmcomic"),
        }
    )

    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).parents[1] / "main.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeImage:
    def __init__(self, path: str, should_fail: bool = False):
        self.path = path
        self.should_fail = should_fail

    @classmethod
    def fromFileSystem(cls, path: str):
        return cls(path)

    async def register_to_file_service(self) -> str:
        if self.should_fail:
            raise RuntimeError("callback_api_base is not configured")
        return f"http://astrbot:6185/api/file/{Path(self.path).name}"


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action: str, **payload):
        self.calls.append((action, payload))
        return {"message_id": 1}


class _FakeContext:
    def __init__(self, client: _FakeClient):
        self.client = client
        self.platform_ids: list[str] = []

    def get_platform_inst(self, platform_id: str):
        self.platform_ids.append(platform_id)
        return types.SimpleNamespace(get_client=lambda: self.client)


def _plugin(module, client: _FakeClient):
    plugin = module.JMPlugin.__new__(module.JMPlugin)
    plugin.context = _FakeContext(client)
    plugin.config = {"max_forward_images": 1}
    return plugin


def test_forward_images_use_registered_http_urls_without_base64(monkeypatch, tmp_path):
    module = _load_plugin_module()
    monkeypatch.setattr(module, "Image", _FakeImage)
    client = _FakeClient()
    plugin = _plugin(module, client)
    second = tmp_path / "2.jpg"
    first = tmp_path / "1.jpg"
    second.touch()
    first.touch()

    asyncio.run(
        plugin._send_images_as_forward(
            platform_id="Darkness",
            session_id="123456",
            is_group=True,
            self_id="10000",
            files=[second, first],
            aid="424242",
            base_dir=Path("/downloads/424242"),
        )
    )

    assert plugin.context.platform_ids == ["Darkness"]
    assert [action for action, _ in client.calls] == [
        "send_group_forward_msg",
        "send_group_forward_msg",
    ]
    image_files = [
        payload["messages"][1]["data"]["content"][0]["data"]["file"]
        for _, payload in client.calls
    ]
    assert image_files == [
        "http://astrbot:6185/api/file/1.jpg",
        "http://astrbot:6185/api/file/2.jpg",
    ]
    assert all("base64://" not in image_file for image_file in image_files)


def test_file_service_failure_never_sends_the_broken_base64_forward(monkeypatch, tmp_path):
    module = _load_plugin_module()

    class _FailingImage(_FakeImage):
        @classmethod
        def fromFileSystem(cls, path: str):
            return cls(path, should_fail=True)

    monkeypatch.setattr(module, "Image", _FailingImage)
    client = _FakeClient()
    plugin = _plugin(module, client)
    image = tmp_path / "1.jpg"
    image.touch()

    try:
        asyncio.run(
            plugin._send_images_as_forward(
                platform_id="Darkness",
                session_id="123456",
                is_group=True,
                self_id="10000",
                files=[image],
                aid="424242",
                base_dir=Path("/downloads/424242"),
            )
        )
    except module.ForwardImageTransportError as exc:
        assert "callback_api_base" in str(exc)
    else:
        raise AssertionError("file-service failure must be reported")

    assert client.calls == []


def test_non_http_file_service_url_is_rejected_before_sending(monkeypatch, tmp_path):
    module = _load_plugin_module()

    class _LocalPathImage(_FakeImage):
        async def register_to_file_service(self) -> str:
            return "file:///AstrBot/data/plugin_data/1.jpg"

    monkeypatch.setattr(module, "Image", _LocalPathImage)
    client = _FakeClient()
    plugin = _plugin(module, client)
    image = tmp_path / "1.jpg"
    image.touch()

    try:
        asyncio.run(
            plugin._send_images_as_forward(
                platform_id="Darkness",
                session_id="123456",
                is_group=True,
                self_id="10000",
                files=[image],
                aid="424242",
                base_dir=Path("/downloads/424242"),
            )
        )
    except module.ForwardImageTransportError as exc:
        assert "HTTP(S)" in str(exc)
    else:
        raise AssertionError("non-HTTP file service URL must be rejected")

    assert client.calls == []
