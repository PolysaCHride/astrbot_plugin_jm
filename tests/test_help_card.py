"""help_card 模块单元测试."""

import pytest

from jm_plugin import help_card


def test_markdown_contains_all_commands():
    md = help_card.help_markdown()
    assert md.startswith("# JM 漫画下载器")
    assert "| 命令 | 用法 | 说明 |" in md
    for cmd in help_card.COMMANDS:
        assert cmd["name"] in md
        # 单元格内竖线会被转义
        assert cmd["usage"].replace("|", "\\|") in md
    assert "选择器语法" in md


def test_text_fallback_contains_commands_without_markup():
    text = help_card.help_text()
    for cmd in help_card.COMMANDS:
        assert cmd["name"] in text
    assert "|" not in text
    assert "`" not in text


def test_parse_blocks_table_and_sections():
    blocks = help_card._parse_blocks(help_card.help_markdown())
    kinds = [b[0] for b in blocks]
    assert "title" in kinds
    assert "table" in kinds
    assert "list" in kinds
    assert "hr" in kinds
    table = next(b for b in blocks if b[0] == "table")
    # 表格块包含表头行 + 11 行命令数据
    assert table[1][0] == ["命令", "用法", "说明"]
    assert len(table[1]) == len(help_card.COMMANDS) + 1


@pytest.mark.skipif(help_card.Image is None, reason="Pillow 不可用")
def test_render_help_card_produces_png(tmp_path):
    from PIL import Image

    path = help_card.render_help_card(tmp_path, help_card.help_markdown())
    assert path is not None
    with Image.open(path) as im:
        assert im.format == "PNG"
        assert im.width == 960
        assert im.height > 400
    # 缓存命中: 二次渲染返回同一路径
    assert help_card.render_help_card(tmp_path, help_card.help_markdown()) == path


@pytest.mark.skipif(help_card.Image is None, reason="Pillow 不可用")
def test_render_help_card_empty_markdown_does_not_raise(tmp_path):
    path = help_card.render_help_card(tmp_path, "")
    assert path is not None
