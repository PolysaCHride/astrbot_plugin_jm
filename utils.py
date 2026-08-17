"""Small stateless helpers used by the JM plugin."""
from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

ID_PATTERN = re.compile(r"\d{4,}")
IMAGE_SUFFIXES = {".jpg", ".jpe", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif"}

def extract_id(text: str) -> Optional[str]:
    match = ID_PATTERN.search(text or "")
    return match.group(0) if match else None

def safe_filename(name: str, max_len: int = 80) -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", name or "").strip(" .")[:max_len]
    return value or "untitled"

def fmt_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}PB"

def natural_path_key(path: Path) -> list:
    parts = re.split(r"(\d+)", str(path))
    return [int(p) if p.isdigit() else p.lower() for p in parts]
