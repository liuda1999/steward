# SPDX-License-Identifier: Apache-2.0
"""`.env` 的读写。

单独一个模块，是因为写这个文件的地方有两个（`init` 和 Web 控制台），
两份实现必然会分叉：一份保留注释、一份不保留，一份 0600、一份 0644。
这种分叉不会报错，只会让"密钥到底在哪"变成猜谜。
"""

from __future__ import annotations

import os
from pathlib import Path

HEADER = "# steward 上游凭据，不要提交到仓库"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def write_env(path: Path, updates: dict[str, str], drop: list[str] | None = None) -> None:
    """合并写入：只动给定的键，其余行（包括用户自己加的注释）原样保留。"""
    drop_set = set(drop or [])
    lines: list[str] = [HEADER, ""]
    seen: set[str] = set()

    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                if stripped != HEADER:
                    lines.append(raw)
                continue
            if "=" not in raw:
                continue
            name, value = raw.split("=", 1)
            name = name.strip()
            if not name or name in seen or name in drop_set:
                continue
            seen.add(name)
            lines.append(f"{name}={updates[name]}" if name in updates else f"{name}={value.strip()}")

    for name, value in updates.items():
        if name not in seen and name not in drop_set:
            lines.append(f"{name}={value}")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
