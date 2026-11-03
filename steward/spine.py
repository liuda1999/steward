# SPDX-License-Identifier: Apache-2.0
"""任务骨架：把被裁剪掉的区域压成六行，替换掉消失的中间历史。

解决什么问题：裁剪到 10% 之后，`_mark` 只能在 system 里写一句「前面有 224 条
被压缩」。模型知道有缺口，但不知道缺口里是什么，于是两种表现都不好——
要么反复重试已经试过并失败的路径，要么用「根据我们之前的讨论」凭空编。

骨架留下的是最容易丢、又最贵的两行：
  rejected —— 已经否决的方案及原因。丢了就会重试一遍。
  facts    —— 已经量出来的数字和结论。丢了就会重新量一遍或改用猜测值。

为什么用模型生成而不是纯机械：`rejected` 和 `facts` 需要判断哪句话是结论、
哪句话是过程。正则做不到。代价是裁剪时多一次本地调用（约 10 秒），
所以只在丢得足够多时才触发，且按内容 hash 缓存。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .config import ContextCfg
from .prompts import SPINE_PROMPT

logger = logging.getLogger("steward")

FIELDS = ("goal", "constraints", "facts", "rejected", "current", "next")
_LABELS = {
    "goal": "目标",
    "constraints": "约束",
    "facts": "已确认",
    "rejected": "已否决",
    "current": "当前",
    "next": "下一步",
}
_LINE_RE = re.compile(r"^\s*[-*]?\s*(%s)\s*[:：]\s*(.*)$" % "|".join(FIELDS), re.I)
_MAX_FIELD = 400  # 单行上限，防止某一行吃掉整个骨架预算
_BEGIN = "<<<SPINE"
_END = "SPINE>>>"


@dataclass
class Spine:
    goal: str = ""
    constraints: str = ""
    facts: str = ""
    rejected: str = ""
    current: str = ""
    next: str = ""
    source: str = "mechanical"  # model | mechanical | cache
    dropped: int = 0  # 累计压缩条数，写进说明让模型知道缺口有多大
    digest: str = ""

    def empty(self) -> bool:
        return not any(getattr(self, f).strip() for f in FIELDS)

    def as_text(self) -> str:
        lines = [f"[网关] 任务骨架（中间 {self.dropped} 条历史已压缩，以下是被保留下来的线索）"]
        for name in FIELDS:
            value = (getattr(self, name) or "").strip()
            lines.append(f"{name}: {value or 'none'}")
        lines.append(
            "以上由网关从被压缩的消息里提炼，可能不完整。"
            "缺细节时用工具重新确认（读文件、重跑命令），不要凭记忆猜；"
            "但「已否决」里的方案不要再试。"
        )
        return "\n".join(lines)

    def merge(self, newer: "Spine") -> "Spine":
        """新骨架覆盖旧骨架，但空字段沿用旧的。

        裁剪是分次发生的：上一轮留下的 facts 不该因为这一轮没提到就被抹掉。
        """
        out = Spine(source=newer.source, dropped=self.dropped + newer.dropped)
        for name in FIELDS:
            new_value = (getattr(newer, name) or "").strip()
            old_value = (getattr(self, name) or "").strip()
            setattr(out, name, new_value if new_value and new_value.lower() != "none" else old_value)
        return out


def parse_spine(text: str) -> Spine:
    """从模型输出里抽六行。多写的解释性文字一律丢掉。"""
    fields: dict[str, str] = {}
    for line in (text or "").splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        value = match.group(2).strip().strip("*` ")
        if key not in fields:  # 只认第一次出现，后面重复的是模型在自我解释
            fields[key] = value[:_MAX_FIELD]
    return Spine(**{k: v for k, v in fields.items() if k in FIELDS})


def inject_spine(messages: list[dict], spine: Spine | None) -> list[dict]:
    """把骨架写进第一条 system，已有则原地替换。

    为什么不新插一条 system：骨架要能在后续「没有触发裁剪」的轮次里继续存在，
    所以必须落到 merged_messages 本身。插新消息会一轮涨一条；用标记块原地替换，
    长度恒定，也不会破坏 chat 模板对 system 位置的约束。
    """
    if spine is None or spine.empty():
        return messages

    block = f"{_BEGIN}\n{spine.as_text()}\n{_END}"
    out = list(messages)
    for i, msg in enumerate(out):
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            out[i] = {**msg, "content": block}
            return out
        if _BEGIN in content and _END in content:
            head = content.split(_BEGIN, 1)[0].rstrip()
            tail = content.split(_END, 1)[1].lstrip()
            out[i] = {**msg, "content": "\n".join(p for p in (head, block, tail) if p)}
        else:
            out[i] = {**msg, "content": f"{content.rstrip()}\n\n{block}"}
        return out
    out.insert(0, {"role": "system", "content": block})
    return out


# ---------------------------------------------------------------- 渲染被丢弃区域


def _content_of(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "\n".join(parts)
    return ""


def render_region(messages: list[dict], max_chars: int) -> str:
    """把被丢弃的消息渲染成一段有角色标注的文本。

    头和尾都要取：头里有任务的来龙去脉（为什么做、原来怎么定），尾里有
    「做到哪一步了」（current / next）。只取开头的话，模型写出来的 current
    会停留在几百条消息之前，等于没写。

    单条内部再按角色分配额度：user / assistant 是判断句，截得宽一点；
    工具输出多半已经落盘，截窄一点——需要时能读回来，判断句丢了就真没了。
    """
    entries: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "")
        body = _content_of(msg).strip()
        if msg.get("tool_calls"):
            names = [str((c.get("function") or {}).get("name") or "") for c in msg["tool_calls"]]
            body = (body + "\n[调用了工具: " + ", ".join(n for n in names if n) + "]").strip()
        if not body:
            continue
        cap = 800 if role in ("user", "assistant") else 400
        entries.append(f"<{role}> {body[:cap]}")

    if not entries:
        return ""

    # 尾部先占 60%，剩下的给头部——时间上最近的那段对 current/next 最关键
    tail_budget = int(max_chars * 0.6)
    tail: set[int] = set()
    used = 0
    for i in range(len(entries) - 1, -1, -1):
        if used + len(entries[i]) > tail_budget:
            continue
        tail.add(i)
        used += len(entries[i])

    head: set[int] = set()
    for i in range(len(entries)):
        if i in tail or used + len(entries[i]) > max_chars:
            continue
        head.add(i)
        used += len(entries[i])

    picked = head | tail
    out: list[str] = []
    omitted = 0
    for i, entry in enumerate(entries):
        if i in picked:
            if omitted:
                out.append(f"[.. 网关省略了 {omitted} 条 ..]")
                omitted = 0
            out.append(entry)
        else:
            omitted += 1
    if omitted:
        out.append(f"[.. 网关省略了 {omitted} 条 ..]")
    return "\n".join(out)


# ---------------------------------------------------------------- 生成器


class SpineBuilder:
    """生成并缓存任务骨架。

    client_factory 返回一个已配置好的本地客户端（或 None 表示不可用）。
    传工厂而不是实例，是因为客户端有连接生命周期，建在 app 启动时更合适。
    """

    def __init__(
        self,
        cfg: ContextCfg,
        client_factory: Callable[[], object | None],
        state_path: str | Path | None = None,
    ):
        self.cfg = cfg
        self.client_factory = client_factory
        self.path = Path(state_path) if state_path else None
        self._cache: dict[str, dict] = {}
        self._load()

    # ---- 对外 ----

    async def build(
        self,
        dropped: list[dict],
        prev: Spine | None = None,
        anchor_goal: str = "",
        allow_model: bool = True,
    ) -> Spine | None:
        """返回新骨架；丢得太少或彻底失败时返回 None（调用方沿用旧的）。

        anchor_goal 是「裁剪后仍然健在的那句任务目标」。首条 user 现在受保护、
        不会被丢，所以它不在 dropped 里——机械兜底得靠调用方把它递进来，
        否则产出的骨架 goal 是空的。

        allow_model=False 时只走缓存。一个请求里通常要裁 2–3 轮，每轮丢弃区
        略有不同，但描述的是同一段旧历史；为此每轮都调一次模型，等于拿
        20 秒换几行几乎一样的字。调用方据此把每个请求的模型调用压到一次。
        """
        if not self.cfg.spine or not dropped:
            return None
        if len(dropped) < 2:
            return None

        region = render_region(dropped, self.cfg.spine_max_chars)
        if not region.strip():
            return None

        key = self._key(region, prev)
        cached = self._cache.get(key)
        if cached:
            spine = Spine(**{**cached, "source": "cache"})
            spine.dropped = len(dropped)
            return spine if not spine.empty() else None
        if not allow_model:
            return None

        spine = await self._generate(region, prev, anchor_goal)
        if spine is None or spine.empty():
            logger.warning("骨架提炼未产出有效内容，退回机械骨架")
            spine = self._mechanical(dropped, prev, anchor_goal)
        if spine.empty():
            return None
        # 目标只认用户原话。模型看不到目标句（它在受保护段里），
        # 让它自己写就会把「最近在干什么」当成目标——
        # 实测把「改配额计量」写成了「逐个阅读 mod_1..mod_43」。
        if anchor_goal:
            spine.goal = anchor_goal

        self._remember(key, spine)
        spine.dropped = len(dropped)
        return spine

    # ---- 模型 ----

    async def _generate(self, region: str, prev: Spine | None, anchor_goal: str = "") -> Spine | None:
        client = None
        try:
            client = self.client_factory()
        except Exception:  # noqa: BLE001 - 客户端构造失败不该拖垮主请求
            logger.warning("骨架生成拿不到本地客户端，退回机械骨架", exc_info=True)
            return None
        if client is None:
            return None

        parts: list[str] = []
        if anchor_goal:
            # 目标句不在被压缩的区域里（它受保护），必须直接告诉模型，
            # 否则它会从「最近在做什么」反推目标。
            parts.append(f"当前任务目标（用户原话，goal 一行原样照抄，不要改写）：\n{anchor_goal}")
        if prev and not prev.empty():
            parts.append(
                "已有骨架（上一轮压缩留下的，仍然成立的事实请保留）：\n" f"{prev.as_text()}"
            )
        parts.append(f"本次新压缩掉的内容：\n{region}")
        user = "\n\n".join(parts)

        payload = {
            "model": getattr(getattr(client, "cfg", None), "model", "") or "local",
            "messages": [
                {"role": "system", "content": SPINE_PROMPT},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.cfg.spine_max_tokens,
            "temperature": 0.1,
            "stream": False,
            # 提炼不需要思维链：开着会把 900 token 的预算烧在思考上，
            # 最后连六行都吐不完。
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            data = await asyncio.wait_for(
                client.chat(payload), timeout=max(1.0, float(self.cfg.spine_timeout_s))
            )
        except (asyncio.TimeoutError, httpx.HTTPError, ValueError, KeyError):
            logger.warning("骨架提炼调用失败", exc_info=True)
            return None
        except Exception:  # noqa: BLE001
            logger.warning("骨架提炼异常", exc_info=True)
            return None

        try:
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            return None
        spine = parse_spine(text)
        if spine.empty():
            return None
        spine.source = "model"
        # 新骨架覆盖旧骨架，但旧的非空字段要保住
        return prev.merge(spine) if prev and not prev.empty() else spine

    # ---- 机械兜底 ----

    def _mechanical(self, dropped: list[dict], prev: Spine | None, anchor_goal: str = "") -> Spine:
        """模型不可用时的退路：只保证不丢目标和当前位置。

        产不出 facts / rejected，但至少让模型知道任务是什么、做到哪了，
        比只给一个「压缩了 N 条」的计数强。
        """
        goal = ""
        for msg in dropped:
            if msg.get("role") == "user":
                goal = _content_of(msg).strip()[:300]
                break

        current = ""
        for msg in reversed(dropped):
            if msg.get("role") == "assistant":
                body = _content_of(msg).strip()
                if body:
                    current = body[:200]
                    break

        spine = Spine(
            goal=goal or anchor_goal or (prev.goal if prev else ""),
            constraints=prev.constraints if prev else "",
            facts=prev.facts if prev else "",
            rejected=prev.rejected if prev else "",
            current=current or (prev.current if prev else ""),
            next=prev.next if prev else "",
            source="mechanical",
        )
        return spine

    # ---- 缓存 ----

    def _key(self, region: str, prev: Spine | None) -> str:
        raw = region + "\x00" + (prev.goal if prev else "")
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _remember(self, key: str, spine: Spine) -> None:
        self._cache[key] = {k: getattr(spine, k) for k in FIELDS}
        while len(self._cache) > max(1, self.cfg.spine_cache_size):
            self._cache.pop(next(iter(self._cache)))
        self._save()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._cache = {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=0),
                encoding="utf-8",
            )
        except OSError:
            pass
