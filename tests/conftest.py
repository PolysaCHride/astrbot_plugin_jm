"""pytest 公共配置。

- 把插件根目录加入 sys.path;
- 自定义 tmp_path fixture: 在插件根目录下建临时目录 (避开 pytest 内置
  tmpdir 插件与受限运行环境 (沙箱) 的兼容问题), 用后自动清理。
"""

import shutil
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_path() -> Path:
    """工作区内的独立临时目录 (等价 pytest 内置 tmp_path 的用法)."""
    d = ROOT / ".test_tmp" / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)
