"""forward 模块单元测试: payload 构造 / 分批 / 传输方式."""

import asyncio
import base64
import pytest

from jm_plugin.config import ConfigService
from jm_plugin.forward import ForwardImageTransportError, ForwardService


class _FakeClient:
    def __init__(self):
        self.calls: list = []

    async def call_action(self, action, **payload):
        self.calls.append((action, payload))
        return {"message_id": 1}


class _FakePlatform:
    def __init__(self, client):
        self._client = client

    def get_client(self):
        return self._client


class _FakeContext:
    def __init__(self, platform):
        self.platform = platform

    def get_platform_inst(self, platform_id):
        return self.platform


def _svc(tmp_path, config=None):
    cfg = dict(config or {})
    cfg.setdefault("custom_data_dir", str(tmp_path / "data"))
    return ConfigService("test_jm", cfg)


def _make_images(tmp_path, count=3):
    paths = []
    for i in range(1, count + 1):
        p = tmp_path / f"{i}.jpg"
        p.write_bytes(f"image-{i}".encode())
        paths.append(p)
    return paths


def _send(config, images, tmp_path, is_group=True):
    svc = _svc(tmp_path, config)
    client = _FakeClient()
    ctx = _FakeContext(_FakePlatform(client))
    fwd = ForwardService(svc, config or {}, ctx)
    result = asyncio.run(
        fwd.send(
            "aiocqhttp", "123456", is_group, "10000", images, "424242",
            tmp_path,
        )
    )
    return fwd, client, result


def test_file_transport_payload(tmp_path):
    images = _make_images(tmp_path, 2)
    fwd, client, result = _send({"max_forward_images": 1}, images, tmp_path)
    assert fwd.transport == "file"
    assert result["images"] == 2 and result["batches"] == 2
    assert [a for a, _ in client.calls] == [
        "send_group_forward_msg",
        "send_group_forward_msg",
    ]
    for _, payload in client.calls:
        assert payload["group_id"] == "123456"
        nodes = payload["messages"]
        assert nodes[0]["type"] == "node"
        assert nodes[0]["data"]["nickname"] == "JM 漫画下载器"
        assert nodes[0]["data"]["user_id"] == "10000"
        assert nodes[0]["data"]["content"][0]["type"] == "text"
        img_node = nodes[1]
        assert img_node["data"]["content"][0]["type"] == "image"
        file_val = img_node["data"]["content"][0]["data"]["file"]
        assert file_val.startswith("file:///")
        assert "base64://" not in str(nodes)
    assert client.calls[0][1]["messages"][1]["data"]["nickname"] == "1/2"
    assert client.calls[1][1]["messages"][1]["data"]["nickname"] == "2/2"


def test_base64_transport_payload(tmp_path):
    images = _make_images(tmp_path, 2)
    fwd, client, result = _send(
        {"max_forward_images": 0, "forward_image_transport": "base64"},
        images,
        tmp_path,
    )
    assert fwd.transport == "base64"
    assert result["batches"] == 1
    payload = client.calls[0][1]
    nodes = payload["messages"]
    assert len(nodes) == 3
    for i, img_node in enumerate(nodes[1:], 1):
        file_val = img_node["data"]["content"][0]["data"]["file"]
        assert file_val.startswith("base64://")
        decoded = base64.b64decode(file_val[len("base64://"):])
        assert decoded == f"image-{i}".encode()


def test_invalid_transport_falls_back_to_file(tmp_path):
    images = _make_images(tmp_path, 1)
    fwd, client, _ = _send({"forward_image_transport": "http"}, images, tmp_path)
    assert fwd.transport == "file"
    file_val = client.calls[0][1]["messages"][1]["data"]["content"][0]["data"]["file"]
    assert file_val.startswith("file:///")


def test_private_forward_uses_user_id(tmp_path):
    images = _make_images(tmp_path, 1)
    _, client, _ = _send({}, images, tmp_path, is_group=False)
    assert client.calls[0][0] == "send_private_forward_msg"
    assert client.calls[0][1]["user_id"] == "123456"
    assert "group_id" not in client.calls[0][1]


def test_missing_files_skipped(tmp_path):
    images = _make_images(tmp_path, 1)
    ghost = tmp_path / "ghost.jpg"
    _, client, result = _send({}, [images[0], ghost], tmp_path)
    assert result["images"] == 1
    assert len(client.calls[0][1]["messages"]) == 2


def test_platform_missing_raises(tmp_path):
    images = _make_images(tmp_path, 1)
    svc = _svc(tmp_path)

    class _NoPlatformContext:
        def get_platform_inst(self, platform_id):
            return None

    fwd = ForwardService(svc, {}, _NoPlatformContext())
    with pytest.raises(ForwardImageTransportError):
        asyncio.run(
            fwd.send("aiocqhttp", "1", True, "1", images, "1", tmp_path)
        )


def test_client_without_call_action_raises(tmp_path):
    images = _make_images(tmp_path, 1)
    svc = _svc(tmp_path)

    class _BareClient:
        pass

    class _BarePlatform:
        def get_client(self):
            return _BareClient()

    class _Ctx:
        def get_platform_inst(self, platform_id):
            return _BarePlatform()

    fwd = ForwardService(svc, {}, _Ctx())
    with pytest.raises(ForwardImageTransportError):
        asyncio.run(
            fwd.send("aiocqhttp", "1", True, "1", images, "1", tmp_path)
        )


def test_empty_files_no_calls(tmp_path):
    _, client, result = _send({}, [], tmp_path)
    assert result["images"] == 0
    assert client.calls == []


def test_batch_callback_invoked(tmp_path):
    images = _make_images(tmp_path, 3)
    svc = _svc(tmp_path, {"max_forward_images": 2})
    client = _FakeClient()
    fwd = ForwardService(svc, {"max_forward_images": 2}, _FakeContext(_FakePlatform(client)))
    seen: list = []

    async def on_batch(idx, total):
        seen.append((idx, total))

    asyncio.run(
        fwd.send("aiocqhttp", "1", True, "1", images, "1", tmp_path, on_batch=on_batch)
    )
    assert seen == [(1, 2), (2, 2)]
