"""config 模块单元测试 (不触碰 jmcomic)."""

from pathlib import Path

from jm_plugin.config import ConfigService


def _svc(config, tmp_path, plugin_name="test_jm"):
    cfg = dict(config)
    cfg.setdefault("custom_data_dir", str(tmp_path / "data"))
    return ConfigService(plugin_name, cfg)


def test_typed_getters(tmp_path):
    svc = _svc(
        {"num": "5", "zero": 0, "bad": "abc", "flag": "true", "s": "  x  "},
        tmp_path,
    )
    assert svc.get_int("num", 1) == 5
    assert svc.get_int("zero", 1) == 0
    assert svc.get_int("bad", 1) == 1
    assert svc.get_int("missing", 9) == 9
    assert svc.get_bool("flag") is True
    assert svc.get_bool("missing") is False
    assert svc.get_str("s") == "x"
    assert svc.get_str("missing", "d") == "d"


def test_data_dir_custom_and_cached(tmp_path):
    svc = _svc({}, tmp_path)
    assert svc.data_dir == (tmp_path / "data").resolve()
    assert svc.data_dir is svc.data_dir


def test_download_dir_escape_guard(tmp_path):
    svc = _svc({"download_subdir": "../../outside"}, tmp_path)
    assert svc.download_dir() == (tmp_path / "data" / "downloads").resolve()


def test_download_dir_custom(tmp_path):
    svc = _svc({"download_subdir": "mydl"}, tmp_path)
    assert svc.download_dir() == (tmp_path / "data" / "mydl").resolve()


def test_docker_host_mapped_paths():
    svc = _svc({}, Path("C:/tmp"))
    mapped = svc.docker_host_mapped_paths(Path("/AstrBot/data/plugin_data/x"))
    assert mapped == [
        Path("/root/astrbot/data/plugin_data/x"),
        Path("/root/AstrBot/astrbot/data/plugin_data/x"),
    ]
    assert svc.docker_host_mapped_paths(Path("C:/other")) == []


def test_scan_roots_dedupe_and_data_dir(tmp_path):
    svc = _svc({}, tmp_path)
    root = tmp_path / "downloads"
    roots = svc.scan_roots(root, root, None)
    keys = [str(p) for p in roots]
    assert len(keys) == len(set(keys))
    assert str(svc.data_dir) in keys
    assert str(root) in keys
    assert None not in roots


def test_build_option_dict_defaults(tmp_path):
    svc = _svc({"image_suffix": ".jpg"}, tmp_path)
    opt = svc.build_option_dict()
    assert opt["client"]["impl"] == "api"
    assert opt["client"]["retry_times"] == 3
    assert opt["client"]["domain"] == {}
    assert opt["download"]["image"]["suffix"] == ".jpg"
    assert opt["download"]["image"]["decode"] is True
    assert opt["download"]["threading"]["image"] == 16
    assert opt["download"]["threading"]["photo"] == 4
    assert opt["dir_rule"]["rule"] == "Bd / Atitle / Ptitle"
    assert opt["log"] is False
    assert opt["client"]["postman"]["meta_data"]["proxies"] is None


def test_build_option_dict_custom(tmp_path):
    svc = _svc(
        {
            "client_impl": "html",
            "custom_domain": "a.com, b.com",
            "use_proxy": True,
            "proxy": "http://127.0.0.1:7890",
            "retry_times": 0,
            "image_thread_count": 0,
            "photo_thread_count": 9,
            "image_suffix": "",
            "dir_rule": "Bd / Atitle",
        },
        tmp_path,
    )
    opt = svc.build_option_dict()
    assert opt["client"]["impl"] == "html"
    assert opt["client"]["domain"] == {"html": ["a.com", "b.com"]}
    assert opt["client"]["postman"]["meta_data"]["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    assert opt["client"]["retry_times"] == 1
    assert opt["download"]["threading"]["image"] == 1
    assert opt["download"]["threading"]["photo"] == 9
    assert opt["download"]["image"]["suffix"] is None
    assert opt["dir_rule"]["rule"] == "Bd / Atitle"


def test_build_option_dict_bad_impl_falls_back(tmp_path):
    svc = _svc({"client_impl": "nope"}, tmp_path)
    assert svc.build_option_dict()["client"]["impl"] == "api"
