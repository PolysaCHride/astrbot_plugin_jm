"""utils 模块单元测试."""

from pathlib import Path

from jm_plugin.utils import (
    extract_id,
    fmt_size,
    is_image,
    parse_selector,
    path_sort_key,
    safe_filename,
    unique_paths,
)


def test_extract_id():
    assert extract_id("下载 424242 谢谢") == "424242"
    assert extract_id("") is None
    assert extract_id("没有数字") is None
    assert extract_id("123") is None
    assert extract_id("12345") == "12345"


def test_safe_filename():
    assert safe_filename("a/b:c*d?e") == "a_b_c_d_e"
    assert safe_filename("") == "untitled"
    assert safe_filename("   ") == "untitled"
    assert safe_filename("x" * 200) == "x" * 80


def test_fmt_size():
    assert fmt_size(0) == "0.0B"
    assert fmt_size(1023) == "1023.0B"
    assert fmt_size(1024) == "1.0KB"
    assert fmt_size(1536) == "1.5KB"
    assert fmt_size(2 * 1024 * 1024) == "2.0MB"


def test_parse_selector():
    assert parse_selector("", 10) == list(range(1, 11))
    assert parse_selector("all", 3) == [1, 2, 3]
    assert parse_selector("*", 3) == [1, 2, 3]
    assert parse_selector("全部", 3) == [1, 2, 3]
    assert parse_selector("1,3,5", 6) == [1, 3, 5]
    assert parse_selector("1-10", 20) == list(range(1, 11))
    assert parse_selector("10-1", 20) == list(range(1, 11))
    assert parse_selector("1,3-5,8", 10) == [1, 3, 4, 5, 8]
    assert parse_selector("1,999", 5) == [1]
    assert parse_selector("abc", 5) == []


def test_path_sort_key_natural_order():
    paths = [Path("a10.jpg"), Path("a2.jpg"), Path("a1.jpg")]
    assert sorted(paths, key=path_sort_key) == [
        Path("a1.jpg"),
        Path("a2.jpg"),
        Path("a10.jpg"),
    ]


def test_is_image():
    assert is_image(Path("a.JPG"))
    assert is_image(Path("a.webp"))
    assert not is_image(Path("a.txt"))


def test_unique_paths():
    a, b = Path("a"), Path("b")
    assert unique_paths([a, b, a, None]) == [a, b]
