# SPDX-License-Identifier: Apache-2.0
"""云端调用护栏。

这是整套东西的承重墙。四道闸，任何一道不通过都不会产生云端费用：

1. 单请求上限  max_calls_per_turn   防止一次对话里反复外援
2. 每日总量    daily_limit          防止一天烧穿
3. 最小间隔    min_interval_s        防止抖动式连续调用
4. 重复检测    dedupe               同一个问题换个说法再问一次，拒绝

计数在「放行时」就 +1（不是等返回），避免并发请求绕过配额。
真正没发出去的连接失败可以 refund。
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import GuardCfg
from .prompts import render_denial

_WS_RE = re.compile(r"[\s\u3000]+")
_PUNCT_RE = re.compile(r"[，。！？；：、,.!?;:\"'`（）()\[\]{}【】<>《》\-—_/\\|*#]+")
_COMPARE_CHARS = 1500


def normalize(text: str) -> str:
    text = _PUNCT_RE.sub("", text or "")
    text = _WS_RE.sub("", text)
    return text.lower()


def digest(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:16]


def _similar(a: str, b: str, threshold: float) -> bool:
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a[:_COMPARE_CHARS], b[:_COMPARE_CHARS]).ratio() >= threshold


@dataclass
class Decision:
    allowed: bool
    code: str = "ok"
    detail: str = ""

    @property
    def notice(self) -> str:
        if self.detail:
            return self.detail
        return "" if self.allowed else render_denial(self.code)


@dataclass
class TurnState:
    turn_id: str = ""
    key_id: str = ""
    calls: int = 0
    rounds: int = 0
    denied: int = 0
    precheck_rejects: int = 0
    hashes: set[str] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Guard:
    def __init__(self, cfg: GuardCfg, state_path: Path | None = None):
        self.cfg = cfg
        self.path = Path(state_path or cfg.state_file)
        self._lock = asyncio.Lock()
        self._day = date.today()
        self._used_today = 0
        self._tokens_today = 0
        self._last_call_ts = 0.0
        self._history: list[dict] = []
        self._seq = 0
        self._load()

    # ---------- 对外 ----------

    def new_turn(self, key_id: str = "") -> TurnState:
        self._seq += 1
        return TurnState(turn_id=f"t{int(time.time())}-{self._seq}", key_id=key_id)

    async def reserve(self, turn: TurnState, kind: str, ask: str, brief: str) -> Decision:
        """申请一次云端调用。通过则已计数，不通过则返回拦截原因。"""
        async with self._lock:
            self._roll_day()
            if self.cfg.max_calls_per_turn <= 0:
                return Decision(False, "disabled")

            if turn.calls >= self.cfg.max_calls_per_turn:
                turn.denied += 1
                return Decision(
                    False,
                    "turn_limit",
                    render_denial("turn_limit", used=turn.calls, limit=self.cfg.max_calls_per_turn),
                )

            if self.cfg.daily_limit >= 0 and self._used_today >= self.cfg.daily_limit:
                turn.denied += 1
                return Decision(
                    False,
                    "daily_limit",
                    render_denial("daily_limit", used=self._used_today, limit=self.cfg.daily_limit),
                )

            now = time.time()
            if self._last_call_ts and (now - self._last_call_ts) < self.cfg.min_interval_s:
                turn.denied += 1
                gap = int(self.cfg.min_interval_s - (now - self._last_call_ts)) + 1
                return Decision(False, "cooldown", render_denial("cooldown", gap=gap))

            text = f"{kind}\n{ask}\n{brief}"
            norm = normalize(text)
            if self.cfg.dedupe:
                h = digest(text)
                if h in turn.hashes:
                    turn.denied += 1
                    return Decision(False, "duplicate", render_denial("duplicate"))
                if any(_similar(norm, prev, self.cfg.similarity) for prev in turn.texts[-3:]):
                    turn.denied += 1
                    return Decision(False, "duplicate", render_denial("duplicate"))
                turn.hashes.add(h)

            turn.texts.append(norm)
            turn.calls += 1
            self._used_today += 1
            self._last_call_ts = now
            self._save()
            return Decision(True)

    async def commit(self, turn: TurnState, kind: str, ask: str, usage: dict, ok: bool, elapsed_ms: int, error: str = "") -> None:
        async with self._lock:
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            turn.prompt_tokens += pt
            turn.completion_tokens += ct
            self._tokens_today += pt + ct
            self._history.append(
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "turn": turn.turn_id,
                    "key": turn.key_id,
                    "kind": kind,
                    "ask": (ask or "")[:120],
                    "ok": ok,
                    "error": error,
                    "prompt_tokens": pt,
                    "completion_tokens": ct,
                    "elapsed_ms": elapsed_ms,
                }
            )
            self._history = self._history[-200:]
            self._save()

    async def refund(self, turn: TurnState) -> None:
        """请求根本没发出去（连接失败），退回配额。"""
        async with self._lock:
            turn.calls = max(0, turn.calls - 1)
            self._used_today = max(0, self._used_today - 1)
            self._save()

    async def snapshot(self) -> dict:
        async with self._lock:
            self._roll_day()
            remaining = None if self.cfg.daily_limit < 0 else max(0, self.cfg.daily_limit - self._used_today)
            return {
                "date": self._day.isoformat(),
                "used_today": self._used_today,
                "daily_limit": self.cfg.daily_limit,
                "remaining_today": remaining,
                "tokens_today": self._tokens_today,
                "max_calls_per_turn": self.cfg.max_calls_per_turn,
                "max_tool_rounds": self.cfg.max_tool_rounds,
                "min_interval_s": self.cfg.min_interval_s,
                "dedupe": self.cfg.dedupe,
                "last_call_ts": self._last_call_ts,
                "recent": self._history[-20:],
            }

    async def reset_day(self) -> None:
        async with self._lock:
            self._day = date.today()
            self._used_today = 0
            self._tokens_today = 0
            self._recent_reset()
            self._save()

    # ---------- 内部 ----------

    def _recent_reset(self) -> None:
        self._history = [h for h in self._history if h.get("ts", "").startswith(self._day.isoformat())]

    def _roll_day(self) -> None:
        today = date.today()
        if today != self._day:
            self._day = today
            self._used_today = 0
            self._tokens_today = 0
            self._recent_reset()
            self._save()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        saved_day = raw.get("date")
        if saved_day == self._day.isoformat():
            self._used_today = int(raw.get("used") or 0)
            self._tokens_today = int(raw.get("tokens") or 0)
        self._history = list(raw.get("history") or [])[-200:]
        self._last_call_ts = float(raw.get("last_call_ts") or 0.0)

    def _save(self) -> None:
        payload = {
            "date": self._day.isoformat(),
            "used": self._used_today,
            "tokens": self._tokens_today,
            "last_call_ts": self._last_call_ts,
            "history": self._history[-200:],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass
