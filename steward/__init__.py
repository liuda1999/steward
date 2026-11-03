# SPDX-License-Identifier: Apache-2.0
"""steward：本地模型网关 + 云端外援。

版本号必须定义在下面这些 import 之前：server.py 会 `from . import __version__`，
如果包初始化时还没赋值，就会撞上「部分初始化的模块没有这个属性」。
"""

__version__ = "1.3.0"

from .config import AppCfg, load_config  # noqa: E402
from .server import create_app  # noqa: E402

__all__ = ["AppCfg", "load_config", "create_app", "__version__"]
