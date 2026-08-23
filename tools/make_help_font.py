"""重新生成插件内置的中文字体子集 (/jm help 卡片渲染用)。

用法:  python tools/make_help_font.py

流程:
1. 从 jm_plugin.help_card 的文案生成字符集 (tools/charset.txt);
2. 从 GitHub noto-cjk 仓库下载 Noto Sans CJK SC 源字体 (可通过
   HTTPS_PROXY 环境变量走代理, 已存在则跳过);
3. 用 fontTools 子集化后写入 jm_plugin/assets/。

依赖: fonttools (pip install fonttools)。修改帮助文案后运行本脚本即可。
"""

import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jm_plugin import help_card  # noqa: E402

from fontTools import subset  # noqa: E402

_SRC_DIR = Path(__file__).resolve().parent / "_src_fonts"
_SRC_URLS = {
    "regular": "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf",
    "bold": "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Bold.otf",
}


def build_charset() -> str:
    """从帮助文案生成子集字符集."""
    chars: list[str] = []

    def add(text: str) -> None:
        for ch in text:
            if ch not in chars:
                chars.append(ch)

    add(help_card.help_markdown())
    add(help_card.help_text())
    # 常见中文标点 / 全角字符兜底
    for cp in list(range(0x2000, 0x2070)) + list(range(0x3000, 0x3040)) + list(range(0xFF00, 0xFFF0)):
        add(chr(cp))
    # ASCII 可打印
    for cp in range(0x20, 0x7F):
        add(chr(cp))
    return "".join(chars)


def download_src(kind: str) -> Path:
    """下载源字体 (走系统代理配置), 返回本地路径."""
    _SRC_DIR.mkdir(parents=True, exist_ok=True)
    out = _SRC_DIR / f"NotoSansCJKsc-{kind.title()}.otf"
    if out.exists() and out.stat().st_size > 1024 * 1024:
        return out
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy}))
    else:
        opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    print(f"downloading {kind} font ...")
    with opener.open(_SRC_URLS[kind], timeout=60) as resp:
        data = resp.read()
    out.write_bytes(data)
    print(f"  {len(data)} bytes -> {out.name}")
    return out


def subset_font(src: Path, charset: str, bold: bool) -> Path:
    charset_file = Path(__file__).resolve().parent / "charset.txt"
    charset_file.write_text(charset, encoding="utf-8")
    dst = ROOT / "jm_plugin" / "assets" / (
        "help_font_bold.otf" if bold else "help_font_regular.otf"
    )
    print(f"subsetting {src.name} -> {dst.name} ...")
    subset.main([
        str(src),
        f"--text-file={charset_file}",
        f"--output-file={dst}",
        "--desubroutinize",
        "--no-hinting",
    ])
    print(f"  {dst.stat().st_size} bytes")
    return dst


def main() -> None:
    charset = build_charset()
    print(f"charset: {len(charset)} unique chars")
    for kind, bold in (( "regular", False), ("bold", True)):
        src = download_src(kind)
        subset_font(src, charset, bold)
    print("done. assets updated; 重新渲染 /jm help 即生效 (帮助卡片按内容哈希缓存, 会自动刷新)")


if __name__ == "__main__":
    main()
