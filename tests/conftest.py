"""测试夹具覆盖。

pytest 默认的 `tmp_path` 由 `tmp_path_factory` 管理：会话开始时清空旧目录、
结束时再做一次垃圾回收。这两次递归删除在受限沙箱里会被拦下，
导致"测试全过但进程退出码非零"。

这里把它换成一个普通的 `mkdtemp`：每个测试一个独立目录，不由 pytest 回收，
交给操作系统清理。行为对测试完全等价，只是少了那两个删除动作。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_path() -> Path:
    return Path(tempfile.mkdtemp(prefix="st-test-"))
