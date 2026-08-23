"""纯标准库工具函数: 无 AstrBot / jmcomic 依赖, 便于单元测试."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

# 本子 / 章节 ID 都是纯数字字符串
ID_PATTERN = re.compile(r"\d{4,}")

IMAGE_SUFFIXES = {
    ".jpg", ".jpe", ".jpeg", ".jfif", ".png", ".webp", ".gif",
    ".bmp", ".tif", ".tiff", ".avif",
}


def extract_id(text: str) -> Optional[str]:
    """从消息文本中提取纯数字 ID (4 位及以上, 避免误中常用数字)."""
    if not text:
        return None
    m = ID_PATTERN.search(text)
    return m.group(0) if m else None


def safe_filename(name: str, max_len: int = 80) -> str:
    """移除文件名中不能出现的字符, 并限制长度."""
    if not name:
        return "untitled"
    name = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len]
    return name or "untitled"


def fmt_size(num: int) -> str:
    """把字节数格式化为人类可读的 B/KB/MB/GB 字符串."""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PB"


def path_sort_key(path: Path) -> list[Any]:
    """自然排序路径, 避免 10.jpg 排在 2.jpg 前面."""
    try:
        text = str(path.relative_to(path.anchor))
    except ValueError:
        text = str(path)
    parts = re.split(r"(\d+)", text)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def is_image(path: Path) -> bool:
    """判断路径是否为支持的图片文件."""
    return path.suffix.lower() in IMAGE_SUFFIXES


def unique_paths(paths: Iterable[Optional[Path]]) -> list[Path]:
    """去重 (按字符串形式) 并过滤 None, 保持顺序."""
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def parse_tags_args(args: str) -> Optional[tuple[str, str, int]]:
    """解析 /jm tags 参数。

    - 纯数字 (4 位及以上, 本子/章节 ID) -> ("id", "213848", 1): 查看该本子的标签
    - 其他 -> ("tag", "巨乳", page): 按标签名搜索本子
    - 空输入 / 无有效内容 -> None (调用方返回用法提示)
    """
    parts = (args or "").strip().split()
    if not parts:
        return None
    first = parts[0]
    page = 1
    if len(parts) > 1:
        try:
            page = max(1, int(parts[1]))
        except ValueError:
            page = 1
    if first.isdigit() and len(first) >= 4:
        return "id", first, page
    return "tag", first, page


def parse_selector(selector: str, total: int) -> list[int]:
    """解析章节选择器: \"all\" / \"1,3,5-10\" -> 1-based 序号列表."""
    s = (selector or "").strip().lower()
    if not s or s in ("all", "全部", "*"):
        return list(range(1, total + 1))
    selected: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                x, y = int(a), int(b)
            except ValueError:
                continue
            if x > y:
                x, y = y, x
            x = max(1, x)
            y = min(total, y)
            selected.update(range(x, y + 1))
        else:
            try:
                n = int(part)
            except ValueError:
                continue
            if 1 <= n <= total:
                selected.add(n)
    return sorted(selected)
