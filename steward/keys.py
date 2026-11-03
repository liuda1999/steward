# SPDX-License-Identifier: Apache-2.0
"""项目自有 API Key 的签发与校验。

这套东西对外只暴露一把自己签的 key，上游的本地 key 和云端 key 都锁在里面：

    智能体 --[st-xxxx]--> steward --[本地 key]--> Ollama / LM Studio
                             |
                             +--[云端 key]--> Cloud API

三条约束：
1. 只存 hash，不存明文。签发时展示一次，之后无法取回。
2. 校验用常量时间比较；key id 只用于索引，不参与鉴权。
3. 文件原子写 + 收紧权限（POSIX 上 0600）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

PREFIX = "st-"
KEY_ID_LEN = 8
SECRET_LEN = 32
FLUSH_INTERVAL_S = 30.0

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mask(token: str) -> str:
    """用于日志展示，永远不吐出完整 key。"""
    if not token.startswith(PREFIX) or len(token) <= len(PREFIX) + KEY_ID_LEN + 4:
        return "****"
    return f"{token[: len(PREFIX) + KEY_ID_LEN]}-****"


@dataclass
class ApiKey:
    id: str
    label: str
    hash: str
    created_at: str
    enabled: bool = True
    last_used_at: str = ""
    calls: int = 0

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "created_at": self.created_at,
            "enabled": self.enabled,
            "last_used_at": self.last_used_at,
            "calls": self.calls,
        }


class KeyStoreError(RuntimeError):
    pass


_FIELDS = {f.name for f in fields(ApiKey)}


class KeyStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._keys: list[ApiKey] = []
        self._by_id: dict[str, ApiKey] = {}
        self._last_flush = 0.0
        self._load()

    # ---------- 对外 ----------

    def issue(self, label: str = "default") -> tuple[str, ApiKey]:
        """签发一把新 key。返回 (明文, 记录)。明文只在这一刻存在。"""
        key_id = secrets.token_hex(4)
        while key_id in self._by_id:
            key_id = secrets.token_hex(4)
        secret = secrets.token_urlsafe(SECRET_LEN)
        token = f"{PREFIX}{key_id}-{secret}"

        rec = ApiKey(id=key_id, label=(label or "default").strip(), hash=_hash(token), created_at=_now())
        self._keys.append(rec)
        self._by_id[rec.id] = rec
        self._save()
        return token, rec

    def verify(self, token: str) -> ApiKey | None:
        if not token or not token.startswith(PREFIX):
            return None
        rest = token[len(PREFIX) :]
        if len(rest) <= KEY_ID_LEN or rest[KEY_ID_LEN] != "-":
            return None
        rec = self._by_id.get(rest[:KEY_ID_LEN])
        if rec is None or not rec.enabled:
            return None
        if not hmac.compare_digest(_hash(token), rec.hash):
            return None
        return rec

    def touch(self, key_id: str) -> None:
        """记一次使用。落盘有节流，避免每个请求都写文件。"""
        rec = self._by_id.get(key_id)
        if rec is None:
            return
        rec.calls += 1
        rec.last_used_at = _now()
        now = time.time()
        if now - self._last_flush >= FLUSH_INTERVAL_S:
            self._save()
            self._last_flush = now

    def revoke(self, key_id: str) -> bool:
        rec = self._by_id.get(key_id)
        if rec is None or not rec.enabled:
            return False
        rec.enabled = False
        self._save()
        return True

    def get(self, key_id: str) -> ApiKey | None:
        return self._by_id.get(key_id)

    def all(self) -> list[ApiKey]:
        return list(self._keys)

    @property
    def empty(self) -> bool:
        return not any(k.enabled for k in self._keys)

    def summary(self) -> dict:
        active = [k for k in self._keys if k.enabled]
        return {
            "total": len(self._keys),
            "active": len(active),
            "calls_today_source": "usage",
            "items": [k.public() for k in self._keys],
        }

    # ---------- 内部 ----------

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in raw.get("keys") or []:
            if not isinstance(item, dict):
                continue
            data = {k: v for k, v in item.items() if k in _FIELDS}
            if not data.get("id") or not data.get("hash"):
                continue
            rec = ApiKey(**data)
            self._keys.append(rec)
            self._by_id[rec.id] = rec

    def _save(self) -> None:
        payload = {"version": 1, "keys": [asdict(k) for k in self._keys]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        self._last_flush = time.time()
