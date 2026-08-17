from __future__ import annotations
from pathlib import Path
from typing import Iterable
from .utils import IMAGE_SUFFIXES, natural_path_key

def photo_save_dir(option, album, photo):
    try: value = option.dir_rule.decide_image_save_dir(album, photo)
    except Exception: return None
    return Path(str(value)) if value else None

def scan_photo_dir_images(path: Path | None) -> list[Path]:
    if not path or not path.exists(): return []
    return sorted((p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES), key=natural_path_key)

def scan_cached_photos(option, album, photos):
    cached, missing, images = [], [], []
    for photo in photos:
        found = scan_photo_dir_images(photo_save_dir(option, album, photo))
        if found: cached.append(photo); images.extend(found)
        else: missing.append(photo)
    return cached, missing, sorted(images, key=natural_path_key)

def parse_selector(selector: str, total: int) -> list[int]:
    value = (selector or "").strip().lower()
    if not value or value in {"all", "全部", "*"}: return list(range(1, total + 1))
    selected = set()
    for part in value.split(","):
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1)); a, b = sorted((a, b)); selected.update(range(max(1,a), min(total,b)+1))
            else:
                n = int(part)
                if 1 <= n <= total: selected.add(n)
        except ValueError: pass
    return sorted(selected)

def unique_images(files: Iterable[Path]) -> list[Path]:
    seen = set(); result = []
    for path in sorted((p for p in files if p.is_file()), key=natural_path_key):
        key = str(path.resolve())
        if key not in seen: seen.add(key); result.append(path)
    return result
