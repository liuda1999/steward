#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# steward 正式启动入口（Linux / macOS）。
#
# 为什么需要这个脚本：
#
#   `python -m steward serve` 要求「配置已经存在且填好了」。可新用户手上
#   只有一份 clone 下来的代码，配置文件叫什么、放哪、两个槽位填什么，
#   全都不知道。这个脚本把整条链路收成一条命令：
#
#       检查 Python 版本 → 装依赖 → 生成配置 → 起服务 → 打印控制台地址
#
# 用法：
#   ./scripts/start.sh              # 前台启动（Ctrl+C 停）
#   ./scripts/start.sh --port 27419
#   ./scripts/start.sh --host 0.0.0.0    # 局域网给别的设备用
#   ./scripts/start.sh --check       # 只检查配置，不启动
#
# Windows 用户请用 scripts\start.bat（内容等价）。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
fi

if [ -z "$PY" ]; then
    echo "[环境] 找不到 python3。装一个 3.11+ 的 Python：https://www.python.org/downloads/" >&2
    exit 1
fi

# 版本号要够 3.11：项目用到了 3.11 引入的语法，低版本 import 阶段就炸。
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    VERSION="$("$PY" -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || echo "未知")"
    echo "[环境] Python $VERSION 太低，需要 3.11 或更高。" >&2
    exit 1
fi

# 有虚拟环境就用它的解释器，这样装的依赖和运行时是同一份。
if [ -x ".venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
fi

# --check 走 serve 的校验路径，不真的起服务
exec "$PY" -m steward start "$@"
