"""帮助卡片: 命令速查表 + markdown 生成 + PIL 渲染 (失败时回退纯文本)。

- help_markdown(): 单一内容来源, 生成 markdown 文本;
- help_text():     纯文本回退版本 (内容与 markdown 一致);
- render_help_card(): 用内置迷你 markdown 渲染器 (PIL) 把 markdown 渲染成
  PNG 卡片并缓存, 任何失败返回 None, 由调用方回退纯文本。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # PIL 不可用时降级为纯文本
    Image = ImageDraw = ImageFont = None

try:
    from astrbot.api import logger as _astrbot_logger
except ImportError:
    _astrbot_logger = None


def _logger() -> Any:
    if _astrbot_logger is not None:
        return _astrbot_logger
    return logging.getLogger("astrbot_plugin_jm")


PLUGIN_VERSION = "2.0.0"
DATA_SOURCE = "禁漫天堂 (基于 JMComic-Crawler-Python)"

# -------------------------------------------------------------------- #
# 命令数据 (单一内容来源)
# -------------------------------------------------------------------- #
COMMANDS: list[dict] = [
    {"name": "help", "aliases": "h, 帮助", "usage": "/jm help", "desc": "显示本帮助 (markdown 卡片)"},
    {"name": "status", "aliases": "st, 状态, 配置", "usage": "/jm status", "desc": "查看当前配置与登录状态"},
    {"name": "reload", "aliases": "re, 重载", "usage": "/jm reload", "desc": "重载配置 (管理员)"},
    {"name": "search", "aliases": "sc, 搜索", "usage": "/jm search <关键词>", "desc": "搜索本子"},
    {"name": "info", "aliases": "if, 详情", "usage": "/jm info <本子ID>", "desc": "查看本子详情 (自动发封面)"},
    {"name": "cover", "aliases": "cv, 封面", "usage": "/jm cover <本子ID>", "desc": "下载并发送封面"},
    {"name": "episodes", "aliases": "ep, 章节", "usage": "/jm episodes <本子ID>", "desc": "列出本子的全部章节"},
    {"name": "photo", "aliases": "ph, 章节详情", "usage": "/jm photo <章节ID>", "desc": "查看章节信息"},
    {"name": "download", "aliases": "d, 下载", "usage": "/jm d <本子ID|章节ID> [选择器]", "desc": "后台下载并推送合并转发"},
    {"name": "ranking", "aliases": "rk, 排行榜", "usage": "/jm ranking [day|week|month]", "desc": "排行榜, 默认 week"},
    {"name": "tags", "aliases": "tg, 标签", "usage": "/jm tags <标签> [页码]", "desc": "按标签查询本子"},
]

SELECTOR_EXAMPLES: list[tuple[str, str]] = [
    ("all / 全部 / *", "全部章节"),
    ("1,3,5", "指定章节序号"),
    ("1-10", "章节范围"),
    ("1,3-5", "混合格式"),
]

_ID_TIP = "提示: 本子ID 与章节ID 均为纯数字, 可在禁漫网址栏中查看."


# -------------------------------------------------------------------- #
# 文本内容生成
# -------------------------------------------------------------------- #
def help_markdown() -> str:
    """生成帮助 markdown (标题 + 命令表格 + 选择器语法 + 页脚)."""
    lines = [
        "# JM 漫画下载器 · 使用帮助",
        "",
        f"所有命令以 /jm 开头 · 数据源: {DATA_SOURCE} · 版本 {PLUGIN_VERSION}",
        "",
        "| 命令 | 用法 | 说明 |",
        "| --- | --- | --- |",
    ]
    for cmd in COMMANDS:
        # 单元格内的竖线转义, 避免破坏表格结构
        usage = cmd["usage"].replace("|", "\\|")
        desc = cmd["desc"].replace("|", "\\|")
        lines.append(
            f"| **{cmd["name"]}** ({cmd["aliases"]}) | "
            f"`{usage}` | {desc} |"
        )
    lines.append("")
    lines.append("**选择器语法** (download 命令):")
    lines.append("")
    for example, meaning in SELECTOR_EXAMPLES:
        lines.append(f"- `{example}` — {meaning}")
    lines.append("")
    lines.append("---")
    lines.append(_ID_TIP)
    return "".join(line + chr(10) for line in lines)


def help_text() -> str:
    """纯文本回退: 与 markdown 版内容一致, 无表格竖线与反引号."""
    lines = [
        "JM 漫画下载器 帮助",
        "------------------------",
        "所有命令以 /jm 开头",
        "",
    ]
    for cmd in COMMANDS:
        usage = cmd["usage"].replace("|", "/")
        lines.append(f"  {cmd["name"]} ({cmd["aliases"]}): {usage} - {cmd["desc"]}")
    lines.append("")
    lines.append("选择器语法 (download 命令):")
    for example, meaning in SELECTOR_EXAMPLES:
        lines.append(f"  {example}  - {meaning}")
    lines.append("------------------------")
    lines.append(_ID_TIP)
    return "".join(line + chr(10) for line in lines)


# -------------------------------------------------------------------- #
# 迷你 markdown 渲染器 (PIL)
# -------------------------------------------------------------------- #
_CARD_WIDTH = 960
_MARGIN = 40
_BG_COLOR = (247, 249, 252)
_CARD_COLOR = (255, 255, 255)
_BORDER_COLOR = (216, 226, 242)
_HEAD_FILL = (233, 240, 254)
_TITLE_COLOR = (23, 68, 150)
_TEXT_COLOR = (38, 46, 60)
_SUB_COLOR = (110, 120, 140)
_CODE_FILL = (232, 240, 254)
_CODE_TEXT = (27, 91, 178)
_ACCENT = (31, 111, 235)

# 插件内置的子集中文字体 (Noto Sans CJK SC, SIL OFL 1.1), 保证任何环境都能渲染中文
_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

# 系统字体搜索目录: Windows / Linux / macOS 常见位置
_SYSTEM_FONT_DIRS = [
    str(Path((os.environ.get("WINDIR") or "C:/Windows")) / "Fonts"),
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/noto-cjk",
    "/usr/share/fonts/opentype/noto-cjk",
    "/usr/share/fonts/truetype/wqy",
    "/usr/share/fonts/truetype/arphic",
    "/usr/share/fonts/truetype/droid",
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    str(Path.home() / ".fonts"),
    str(Path.home() / ".local/share/fonts"),
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
]

# 各平台常见的中文字体文件名
_CJK_FONT_FILES = {
    "regular": (
        "msyh.ttc", "simhei.ttf", "simsun.ttc", "Deng.ttf", "msyhl.ttc",
        "NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf", "NotoSansSC-Regular.otf",
        "SourceHanSansCN-Regular.otf", "source-han-sans-cn-regular.otf",
        "wqy-zenhei.ttc", "wqy-microhei.ttc", "WenQuanYi Zen Hei.ttc",
        "PingFang.ttc", "Hiragino Sans GB.ttc", "STHeiti Light.ttc",
        "Arial Unicode.ttf", "Songti.ttc", "uming.ttc", "ukai.ttc",
    ),
    "bold": (
        "msyhbd.ttc", "simhei.ttf",
        "NotoSansCJK-Bold.ttc", "NotoSansCJKsc-Bold.otf", "NotoSansSC-Bold.otf",
        "SourceHanSansCN-Bold.otf", "WenQuanYi Zen Hei.ttc", "PingFang.ttc",
        "NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf",
    ),
}

# 字体族名含这些标记才认为支持中文 (防止回退到 Aileron 等纯拉丁字体)
_CJK_FONT_MARKERS = (
    "yahei", "simhei", "simsun", "dengxian", "jhenghei", "kaiti", "fangsong",
    "cjk", "source han", "wenquanyi", "wqy", "pingfang", "hiragino", "songti",
    "heiti", "arphic", "uming", "ukai", "sarasa", "lxgw", "ms gothic",
    "meiryo", "yu gothic", "malgun", "microsoft yahei", "noto sans sc",
)


def _system_font_candidates(bold: bool = False) -> list[str]:
    """生成系统 CJK 字体候选路径: 目录×文件名 + 裸名 + AstrBot data/font.ttf."""
    files = _CJK_FONT_FILES["bold" if bold else "regular"]
    paths = [str(Path(d) / f) for d in _SYSTEM_FONT_DIRS for f in files]
    paths.extend(files)  # 裸名 (部分 Pillow 版本可解析)
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        paths.insert(0, str(Path(get_astrbot_data_path()) / "font.ttf"))
    except Exception:  # noqa: BLE001
        pass
    seen: set[str] = set()
    unique: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _load_font(size: int, bold: bool = False):
    """加载支持中文的字体。

    优先级:
    1. 插件内置子集字体 (Noto Sans CJK SC, 任何环境都能渲染卡中文);
    2. 系统 CJK 字体 (Windows / Linux / macOS 常见位置 + AstrBot data/font.ttf);
    3. PIL 默认字体 (纯拉丁, 仅兜底并记录 warning)。
    """
    bundled = _ASSETS_DIR / ("help_font_bold.otf" if bold else "help_font_regular.otf")
    try:
        if bundled.exists():
            return ImageFont.truetype(str(bundled), size)
    except OSError:
        pass

    for path in _system_font_candidates(bold):
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            continue
        family = (font.getname()[0] or "").lower()
        if any(marker in family for marker in _CJK_FONT_MARKERS):
            return font

    _logger().warning(
        "[JM] 未找到支持中文的字体, 帮助卡片中文可能显示为方框; "
        "插件已内置子集中文字体, 若仍异常请检查 jm_plugin/assets 目录是否完整。"
    )
    return ImageFont.load_default()


_INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")


def _split_inline(text: str) -> list[tuple[str, str]]:
    """把行内 **粗体** 与 `代码` 拆成 (style, text) 片段."""
    parts: list[tuple[str, str]] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            parts.append(("text", text[pos : m.start()]))
        token = m.group(0)
        if token.startswith("**"):
            parts.append(("bold", token[2:-2]))
        else:
            parts.append(("code", token[1:-1]))
        pos = m.end()
    if pos < len(text):
        parts.append(("text", text[pos:]))
    return parts


def _plain(text: str) -> str:
    """去掉行内标记, 仅用于宽度计算与折行."""
    text = text.replace("**", "")
    text = text.replace("`", "")
    return text


def _inline_width(draw, text: str, font, bold_font, code_font) -> int:
    """计算带行内标记的文本总宽度."""
    total = 0.0
    for style, seg in _split_inline(text):
        if style == "code":
            total += draw.textlength(seg, font=code_font) + 8
        elif style == "bold":
            total += draw.textlength(seg, font=bold_font)
        else:
            total += draw.textlength(seg, font=font)
    return int(total)


def _wrap(draw, text: str, max_width: int, font) -> list[str]:
    """按像素宽度折行 (贪心逐字)."""
    lines: list[str] = []
    current = ""
    for ch in text:
        if draw.textlength(current + ch, font=font) <= max_width or not current:
            current += ch
        else:
            lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines or [""]


def _split_row(s: str) -> list[str]:
    """按未转义的竖线拆分表格行, 并还原 \\| 转义."""
    body = s.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for ch in body:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    cells.append("".join(current).strip())
    return [c.replace("\\|", "|") for c in cells]


def _parse_blocks(markdown: str) -> list[tuple[str, Any]]:
    """把 markdown 拆成渲染块: title / text / table / list / hr."""
    blocks: list[tuple[str, Any]] = []
    table_rows: list[list[str]] = []

    def flush_table() -> None:
        if table_rows:
            blocks.append(("table", list(table_rows)))
            table_rows.clear()

    for line in markdown.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = _split_row(s)
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue  # 表格分隔行
            table_rows.append(cells)
            continue
        flush_table()
        if s.startswith("# "):
            blocks.append(("title", s[2:].strip()))
        elif s == "---":
            blocks.append(("hr", None))
        elif s.startswith("- "):
            blocks.append(("list", s[2:].strip()))
        elif s:
            blocks.append(("text", s))
    flush_table()
    return blocks


class _CardRenderer:
    """把 markdown 块渲染为 PIL 卡片图片."""

    def __init__(self, markdown: str) -> None:
        self.blocks = _parse_blocks(markdown)
        self.title_font = _load_font(38, bold=True)
        self.body_font = _load_font(24)
        self.bold_font = _load_font(24, bold=True)
        self.code_font = _load_font(23)
        self.small_font = _load_font(22)
        self.head_font = _load_font(25, bold=True)
        self.line_h = self.body_font.size + 10

    def _table_geometry(self, draw) -> Optional[tuple[int, int, int]]:
        """计算表格三列宽度, 无表格返回 None."""
        table = next((b for b in self.blocks if b[0] == "table"), None)
        if table is None:
            return None
        rows: list[list[str]] = table[1]
        inner_w = _CARD_WIDTH - _MARGIN * 2
        col0 = max(_inline_width(draw, r[0], self.body_font, self.bold_font, self.code_font) for r in rows)
        col1 = max(_inline_width(draw, r[1], self.body_font, self.bold_font, self.code_font) for r in rows)
        col0 = max(col0, 140) + 32
        col1 = max(col1, 190) + 32
        col2 = inner_w - col0 - col1
        return col0, col1, max(col2, 200)

    def _block_height(self, block: tuple[str, Any], draw, cols) -> int:
        """计算单个块的渲染高度."""
        kind, payload = block
        if kind == "title":
            return self.title_font.size + 26
        if kind == "hr":
            return 22
        if kind == "list":
            return self.line_h + 6
        if kind == "text":
            return 30
        if kind == "table":
            col0, col1, col2 = cols
            h = self.head_font.size + 22  # 表头
            for row in payload[1:]:  # 跳过标题行 (已由表头绘制)
                desc_lines = _wrap(draw, _plain(row[2]), col2 - 32, self.body_font)
                h += max(1, len(desc_lines)) * self.line_h + 20
            return h + 6
        return 0

    def render(self) -> Any:
        """渲染整张卡片, 返回 PIL Image."""
        img = Image.new("RGB", (_CARD_WIDTH, 120), _BG_COLOR)
        draw = ImageDraw.Draw(img)
        cols = self._table_geometry(draw)

        heights = [self._block_height(b, draw, cols) for b in self.blocks]
        total_h = 16 + sum(heights) + 26
        img = Image.new("RGB", (_CARD_WIDTH, max(120, total_h)), _BG_COLOR)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (8, 8, _CARD_WIDTH - 8, total_h - 8), radius=18, fill=_CARD_COLOR,
            outline=_BORDER_COLOR, width=2,
        )

        y = _MARGIN
        for block, h in zip(self.blocks, heights):
            y = self._draw_block(draw, block, cols, y)
        return img

    def _draw_block(self, draw, block: tuple[str, Any], cols, y: int) -> int:
        """绘制单个块, 返回新的 y 坐标."""
        kind, payload = block
        if kind == "title":
            draw.text((_MARGIN, y), payload, font=self.title_font, fill=_TITLE_COLOR)
            y += self.title_font.size + 10
            draw.rounded_rectangle(
                (_MARGIN, y, _MARGIN + 180, y + 6), radius=3, fill=_ACCENT
            )
            return y + 18
        if kind == "text":
            self._draw_inline(draw, (_MARGIN, y), payload, self.small_font, _SUB_COLOR)
            return y + 30
        if kind == "list":
            draw.text((_MARGIN + 8, y), "•", font=self.body_font, fill=_TEXT_COLOR)
            self._draw_inline(draw, (_MARGIN + 34, y), payload, self.body_font, _TEXT_COLOR)
            return y + self.line_h + 6
        if kind == "hr":
            y += 8
            draw.line((_MARGIN, y, _CARD_WIDTH - _MARGIN, y), fill=_BORDER_COLOR, width=2)
            return y + 14
        if kind == "table":
            return self._draw_table(draw, payload, cols, y)
        return y

    def _draw_table(self, draw, rows: list[list[str]], cols, y: int) -> int:
        """绘制命令表格 (表头 + 行, 说明列自动折行)."""
        col0, col1, col2 = cols
        x0, x1, x2 = _MARGIN, _MARGIN + col0, _MARGIN + col0 + col1
        table_top = y
        table_w = col0 + col1 + col2
        # 表头
        head_h = self.head_font.size + 22
        draw.rectangle((x0, y, x0 + table_w, y + head_h), fill=_HEAD_FILL)
        draw.text((x0 + 16, y + 11), "命令", font=self.head_font, fill=_TEXT_COLOR)
        draw.text((x1 + 16, y + 11), "用法", font=self.head_font, fill=_TEXT_COLOR)
        draw.text((x2 + 16, y + 11), "说明", font=self.head_font, fill=_TEXT_COLOR)
        y += head_h
        # 数据行 (跳过标题行, 已由上方表头绘制)
        for i, row in enumerate(rows[1:]):
            desc_lines = _wrap(draw, _plain(row[2]), col2 - 32, self.body_font)
            row_h = max(1, len(desc_lines)) * self.line_h + 20
            if i % 2 == 1:
                draw.rectangle((x0, y, x0 + table_w, y + row_h), fill=(250, 252, 255))
            cy = y + 10
            self._draw_inline(draw, (x0 + 16, cy), row[0], self.body_font, _TEXT_COLOR)
            self._draw_inline(draw, (x1 + 16, cy), row[1], self.body_font, _TEXT_COLOR)
            for line in desc_lines:
                draw.text((x2 + 16, cy), line, font=self.body_font, fill=_TEXT_COLOR)
                cy += self.line_h
            draw.line((x0, y + row_h, x0 + table_w, y + row_h), fill=_BORDER_COLOR, width=1)
            y += row_h
        # 外边框与列分隔线
        draw.rectangle((x0, table_top, x0 + table_w, y), outline=_BORDER_COLOR, width=2)
        draw.line((x1, table_top, x1, y), fill=_BORDER_COLOR, width=1)
        draw.line((x2, table_top, x2, y), fill=_BORDER_COLOR, width=1)
        return y + 4

    def _draw_inline(self, draw, xy: tuple[int, int], text: str, font, color) -> None:
        """绘制带 **粗体** / `代码` 行内样式的文本."""
        x, y = xy
        for style, seg in _split_inline(text):
            if style == "code":
                f = self.code_font
                w = draw.textlength(seg, font=f)
                draw.rounded_rectangle(
                    (x - 2, y + 2, x + w + 6, y + self.body_font.size + 8),
                    radius=5, fill=_CODE_FILL,
                )
                draw.text((x + 2, y), seg, font=f, fill=_CODE_TEXT)
                x += int(w) + 12
            else:
                f = self.bold_font if style == "bold" else font
                w = draw.textlength(seg, font=f)
                draw.text((x, y), seg, font=f, fill=color)
                x += int(w)


def render_help_card(
    cache_dir: Path,
    markdown: Optional[str] = None,
    width: int = _CARD_WIDTH,
) -> Optional[str]:
    """把帮助 markdown 渲染为 PNG 卡片并缓存, 返回图片路径。

    渲染或 PIL 不可用时返回 None (调用方回退纯文本)。
    """
    if Image is None:
        return None
    markdown = markdown or help_markdown()
    cache_dir = Path(cache_dir)
    # 内置字体纳入缓存键: 字体变更 (重新子集化) 后旧卡片自动失效
    bundled_font = _ASSETS_DIR / "help_font_regular.otf"
    font_sig = bundled_font.stat().st_size if bundled_font.exists() else 0
    key = hashlib.sha1(
        f"{markdown}{chr(10)}{width}{chr(10)}{PLUGIN_VERSION}{chr(10)}{font_sig}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    out_path = cache_dir / f"help_{key}.png"
    if out_path.exists():
        return str(out_path)
    try:
        image = _CardRenderer(markdown).render()
        cache_dir.mkdir(parents=True, exist_ok=True)
        image.save(out_path, "PNG")
    except Exception as e:  # noqa: BLE001
        _logger().warning(f"[JM] 帮助卡片渲染失败, 回退纯文本: {e}")
        return None
    return str(out_path)
