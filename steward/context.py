# SPDX-License-Identifier: Apache-2.0
"""上下文治理：探测真实窗口、记账、分级裁剪。

为什么需要这一层：网关是唯一同时知道「客户端发了多少」和「本地模型真能装多少」
的一方。客户端按它以为的窗口做决策，本地模型按真实窗口拒绝请求，中间这道信息差
只能由网关补。

一条实测约束决定了整个策略：本地 prefill 约 1300 tok/s，任何中间改动都会让 KV
前缀缓存失效，代价 ≈ 重算 tokens / 1300 秒。所以这里的原则是**很少动、一动到底**：
只在超过预算时触发，一次裁到目标水位，而不是每轮微调。

裁剪顺序按成本从低到高，前四级是纯字符串操作，不调模型：
  1 丢历史思维链      2 图片换占位      3 旧工具输出机械摘要
  4 合并/删空消息     5 丢最旧的工具对   6 硬截断
每一级都保留「最近 N 条 + 最后一条 user」，且删除时在 system 里留痕。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from .config import ContextCfg, EndpointCfg

logger = logging.getLogger("steward")

# ---------------------------------------------------------------- token 估算

_CJK_SUB = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]+")
_ALNUM_ONLY = re.compile(r"[A-Za-z0-9]")

# 实测（Qwen3 系 tokenizer，本机 27B）：
#   中文 0.94x、英文 1.03x、代码 0.72x、JSON 0.60x（旧的两类模型）
# 代码和 JSON 之所以被低估，是因为标点符号每个几乎都单独成 token：
#   {"id":1,"name":"x"} 24 个字符里 12 个是标点，每个都算一个 token。
# 所以把标点单独拎出来给高系数，别混在"其他"里按 0.3 算。
CJK_RATIO = 0.70
ALNUM_RATIO = 0.28
SPACE_RATIO = 0.16
PUNCT_RATIO = 0.65

# 一张图在 Qwen 视觉塔里大约 1000–1600 tokens，取中值
IMAGE_TOKENS = 1200


def count_tokens(text: Any) -> int:
    """按字符类分段估算，不做 tokenizer。

    中文 0.70/字、字母数字 0.28/字符、空白 0.16/字符、标点 0.65/字符。
    比全局 chars/4 准得多：中文场景下 chars/4 会低估 2.4 倍，
    代码/JSON 场景下又会低估 1.4–1.7 倍，两个方向都很难看。
    残余误差交给 Estimator 用上游返回的真实 prompt_tokens 校准。
    """
    if not text:
        return 0
    if not isinstance(text, str):
        text = str(text)
    n = len(text)
    if n < 8:
        return int(n * 0.6 + 0.5)
    cjk = n - len(_CJK_SUB.sub("", text))
    alnum = n - len(_ALNUM_ONLY.sub("", text))
    spaces = text.count(" ") + text.count("\n") + text.count("\t")
    punct = max(0, n - cjk - alnum - spaces)
    return int(cjk * CJK_RATIO + alnum * ALNUM_RATIO + spaces * SPACE_RATIO + punct * PUNCT_RATIO + 0.5)


_IMG_TYPES = {"image_url", "input_image", "image", "video", "input_video", "video_url"}


def content_tokens(content: Any) -> int:
    if isinstance(content, str):
        return count_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += count_tokens(part)
            elif isinstance(part, dict):
                kind = str(part.get("type") or "")
                if kind == "text":
                    total += count_tokens(part.get("text") or "")
                elif kind in _IMG_TYPES:
                    total += IMAGE_TOKENS
                else:
                    total += count_tokens(part)
        return total
    return 0


def _content_text(msgs: Iterable[dict]) -> str:
    """把消息的可计费部分拼成一段文本，交给上游 tokenizer。

    只拼内容、思维链、工具调用参数，不拼 JSON 结构——模板不会把
    {"role":"user"} 这些字面量喂给模型，拼进去就成了虚报。
    """
    parts: list[str] = []
    for msg in msgs:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif isinstance(item, str):
                    parts.append(item)
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str):
            parts.append(reasoning)
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            parts.append(str(fn.get("name") or ""))
            parts.append(str(fn.get("arguments") or ""))
    return "\n".join(parts)


class Measurer:
    """用上游 /tokenize 拿精确 token 数，拿不到就退回启发式。

    为什么值得多这一次调用：字符启发式在代码和 JSON 上会低估 25%–40%
    （数字几乎一位一 token，标点也几乎一个一 token），而它恰恰是 agent
    上下文的主体。32 万字符实测 337ms，相对几十秒的 prefill 可以忽略。

    /tokenize 只算内容本身，不含 chat 模板。每条消息的模板开销
    （Qwen 实测约 13.6 token）单独加，实测后可以学习校正。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        extra_headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        per_message: float = 14.0,
    ):
        self.url = root_of(base_url).rstrip("/") + "/tokenize" if base_url else ""
        self._transport = transport
        self._force_close = False
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"
        self._headers.update({k: v for k, v in (extra_headers or {}).items() if v})
        self._client = self._build_client()
        self.per_message = per_message
        self.supported: bool | None = None
        self._fails = 0
        self.last_content = 0

    def _build_client(self) -> httpx.AsyncClient:
        headers = dict(self._headers)
        if self._force_close:
            headers["Connection"] = "close"
        return httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=3.0),
            headers=headers,
            transport=self._transport,
            limits=httpx.Limits(max_keepalive_connections=0) if self._force_close else httpx.Limits(),
        )

    async def _post(self, body: dict) -> httpx.Response:
        """同样的 keep-alive 404 自愈：这类部署复用连接时会固定回 404。"""
        resp = await self._client.post(self.url, json=body)
        if resp.status_code != 404 or self._force_close:
            return resp
        logger.warning("tokenize 返回 404，疑似 keep-alive 复用问题，换短连接重试一次")
        self._force_close = True
        await self._client.aclose()
        self._client = self._build_client()
        return await self._client.post(self.url, json=body)

    async def probe(self) -> bool:
        if not self.url:
            self.supported = False
            return False
        try:
            resp = await self._post({"content": "ping 你好 123", "add_special": False})
            body = resp.json() if resp.status_code < 400 else {}
            self.supported = isinstance(body.get("tokens"), list)
        except (httpx.HTTPError, ValueError):
            self.supported = False
        return bool(self.supported)

    async def measure(self, msgs: Iterable[dict], tools_tokens: int = 0) -> int | None:
        if not self.supported:
            return None
        msgs = list(msgs)
        text = _content_text(msgs)
        if not text:
            return None
        try:
            resp = await self._post({"content": text, "add_special": False})
            if resp.status_code >= 400:
                raise ValueError(f"http {resp.status_code}")
            tokens = resp.json().get("tokens")
            if not isinstance(tokens, list):
                raise ValueError("no tokens")
        except (httpx.HTTPError, ValueError):
            self._fails += 1
            if self._fails >= 2:
                self.supported = False
                logger.warning("上游 /tokenize 连续失败，退回字符估算")
            return None
        self._fails = 0
        self.last_content = len(tokens)
        return len(tokens) + int(len(msgs) * self.per_message) + tools_tokens + 3

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass
class Usage:
    estimated: int = 0
    actual: int = 0
    truncated: bool = False

    @property
    def ratio(self) -> float:
        return round(self.actual / self.estimated, 3) if self.estimated else 0.0


# ---------------------------------------------------------------- 估算器


class Estimator:
    """分段启发式 + 用上游返回的 usage.prompt_tokens 自校准。

    自校准是这里最划算的一招：上游每次都告诉我们真实 token 数，
    拿它做 EWMA 修正系数，两三轮就能收敛到 5% 以内，不需要 tokenizer。
    """

    def __init__(self, factor: float = 1.0, state_path: str | Path | None = None):
        self.path = Path(state_path) if state_path else None
        self.factor = max(0.3, min(4.0, float(factor or 1.0)))
        # 被上游静默截断过就下调（实际能装的比标称少）
        self.budget_scale = 1.0
        self.last = Usage()
        self._suspect = 0
        self._load()

    # ---- 记账 ----

    def text(self, value: Any) -> int:
        return int(count_tokens(value) * self.factor + 0.5)

    def message(self, msg: dict) -> int:
        total = 4
        total += content_tokens(msg.get("content")) * self.factor
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str):
            total += self.text(reasoning)
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            total += 4 + self.text(fn.get("name") or "") + self.text(fn.get("arguments") or "")
        if msg.get("tool_call_id"):
            total += 2
        return int(total)

    def messages(self, msgs: Iterable[dict], tools_tokens: int = 0) -> int:
        return int(tools_tokens + 8 + sum(self.message(m) for m in msgs))

    def tools_tokens(self, tools: Any) -> int:
        if not tools:
            return 0
        try:
            raw = json.dumps(tools, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(tools)
        return self.text(raw)

    # ---- 校准 ----

    def observe(self, estimated: int, actual: int) -> bool:
        """用真实 prompt_tokens 校准。返回 True 表示疑似被上游静默截断。

        截断的特征是 actual 远小于 estimated（上游从头部丢掉一截但 HTTP 仍 200）。
        但估算器本身也可能偏 20%，所以不能一看到差值就报警——那会把一次正常的
        估算误差变成永久性的预算收缩。这里用两道门槛：
          - 差得特别狠（< 50%）：立刻判定
          - 差得中等（< 75%）：连续两次才判定
          - 75%–90%：只校准，不报警
        """
        if actual <= 0:
            return False
        self.last = Usage(estimated=estimated, actual=actual)
        if estimated < 200:
            return False

        ratio = actual / estimated
        if ratio < 0.75:
            self._suspect += 1
            if ratio < 0.5 or self._suspect >= 2:
                self.last.truncated = True
                logger.warning(
                    "疑似上游静默截断：估算 %s tokens，上游只记了 %s（%.0f%%）。"
                    "预算收缩 10%%，请确认本地服务是否开了 context-shift 或窗口比标称小。",
                    estimated,
                    actual,
                    ratio * 100,
                )
                self.shrink(0.9, reason="truncation")
                return True
        else:
            self._suspect = 0

        if 0.5 <= ratio <= 2.0:
            self.factor = min(4.0, max(0.3, self.factor * (ratio**0.4)))
            self._save()
        return False

    def shrink(self, factor: float, reason: str = "") -> None:
        self.budget_scale = max(0.35, self.budget_scale * factor)
        logger.warning("上下文预算收缩到 %.0f%%（%s）", self.budget_scale * 100, reason or "overflow")
        self._save()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        try:
            self.factor = max(0.3, min(4.0, float(raw.get("factor") or 1.0)))
            self.budget_scale = max(0.35, min(1.0, float(raw.get("budget_scale") or 1.0)))
        except (TypeError, ValueError):
            pass

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "factor": round(self.factor, 4),
                        "budget_scale": round(self.budget_scale, 4),
                        "usage": {
                            "estimated": self.last.estimated,
                            "actual": self.last.actual,
                            "truncated": self.last.truncated,
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError:
            pass


# ---------------------------------------------------------------- 窗口探测


def root_of(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


@dataclass
class Limits:
    n_ctx: int = 0
    n_ctx_train: int = 0
    slots: int = 1
    per_slot: int = 0
    source: str = "default"
    usable: int = 0
    budget: int = 0

    def finalize(self, cfg: ContextCfg, out_reserve: int, scale: float = 1.0) -> "Limits":
        per = self.per_slot or self.n_ctx or cfg.ctx_limit or 32768
        if cfg.ctx_limit:
            per = min(per, cfg.ctx_limit)
        self.per_slot = per
        self.usable = int(per * cfg.usable_ratio)
        budget = self.usable
        if cfg.reserve_output:
            budget -= max(0, out_reserve)
        budget -= max(0, cfg.safety_tokens)
        self.budget = max(1024, int(budget * scale))
        return self

    def as_dict(self) -> dict:
        return {
            "n_ctx": self.n_ctx,
            "n_ctx_train": self.n_ctx_train,
            "slots": self.slots,
            "per_slot": self.per_slot,
            "source": self.source,
            "usable": self.usable,
            "budget": self.budget,
        }


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class Prober:
    """问上游「你到底能装多少」。

    不同实现暴露的位置不一样，按可靠性依次尝试：
      llama.cpp  GET /props            default_generation_settings.n_ctx + total_slots
      llama.cpp  GET /v1/models        meta.n_ctx
      vLLM       GET /v1/models        max_model_len
      LM Studio  GET /api/v0/models    max_context_length
    全失败就用 config 的 ctx_limit，再兜底 32768。
    """

    def __init__(self, cfg: ContextCfg, ep: EndpointCfg, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.ep = ep
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.ep.api_key:
            headers["Authorization"] = f"Bearer {self.ep.api_key}"
        headers.update({k: v for k, v in (self.ep.extra_headers or {}).items() if v})
        return headers

    async def probe(self) -> Limits:
        if not self.cfg.enabled:
            return Limits(n_ctx=self.cfg.ctx_limit or 0, source="disabled")

        base = (self.ep.base_url or "").rstrip("/")
        if not base:
            return Limits(n_ctx=self.cfg.ctx_limit or 0, source="config")

        root = root_of(base)
        headers = self._headers()
        limits = Limits()

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(6.0, connect=3.0),
            headers=headers,
            transport=self._transport,
        ) as client:
            props = await self._get(client, f"{root}/props")
            if isinstance(props, dict):
                gen = props.get("default_generation_settings") or {}
                n_ctx = _as_int(gen.get("n_ctx")) or _as_int(props.get("n_ctx"))
                slots = _as_int(props.get("total_slots")) or 1
                if n_ctx:
                    limits.n_ctx = n_ctx
                    limits.slots = max(1, slots)
                    limits.per_slot = n_ctx // max(1, slots)
                    limits.source = "props"

            models = await self._get(client, f"{base}/models")
            for item in (models or {}).get("data") or [] if isinstance(models, dict) else []:
                if not isinstance(item, dict):
                    continue
                meta = item.get("meta") or {}
                n_ctx = _as_int(meta.get("n_ctx")) or _as_int(item.get("max_model_len")) or _as_int(
                    item.get("context_length")
                )
                if n_ctx and not limits.per_slot:
                    limits.n_ctx = n_ctx
                    limits.n_ctx_train = _as_int(meta.get("n_ctx_train"))
                    limits.per_slot = n_ctx // max(1, limits.slots)
                    limits.source = "models"
                break

            if not limits.per_slot:
                v0 = await self._get(client, f"{root}/api/v0/models")
                for item in (v0 or {}).get("data") or [] if isinstance(v0, dict) else []:
                    if isinstance(item, dict):
                        n_ctx = _as_int(item.get("max_context_length"))
                        if n_ctx:
                            limits.n_ctx = n_ctx
                            limits.per_slot = n_ctx
                            limits.source = "lmstudio"
                            break

        if not limits.per_slot and self.cfg.ctx_limit:
            limits.per_slot = self.cfg.ctx_limit
            limits.source = "config"
        if not limits.per_slot:
            limits.per_slot = 32768
            limits.source = "default"
        return limits

    @staticmethod
    async def _get(client: httpx.AsyncClient, url: str) -> Any:
        try:
            resp = await client.get(url)
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        try:
            return resp.json()
        except ValueError:
            return None


# ---------------------------------------------------------------- 裁剪


_ERROR_RE = re.compile(
    r"(error|fail|failed|failure|exception|traceback|panic|fatal|denied|refused|timeout|abort|✗|×)",
    re.IGNORECASE,
)
_LOC_RE = re.compile(r"[\w./\\-]+:\d+")
_HINT_MAX_ITEMS = 60


def digest_output(
    text: str,
    head_lines: int = 40,
    tail_lines: int = 40,
    hint_lines: int = _HINT_MAX_ITEMS,
) -> str:
    """把一段工具输出压成「头 + 尾 + 所有像错误的行」。

    对测试日志、构建输出、diff 效果最好——真正有用的就是错误行和头尾，
    中间那几千行 "ok" 是纯噪声。纯字符串操作，确定性，不花模型。

    行数少但字节多的内容（压缩过的 JS、单行 JSON）走字符截断，
    否则会出现「行数够少所以原样返回」，白跑一趟。
    """
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= head_lines + tail_lines + 8:
        if len(text) <= 1200:
            return text
        keep = max(400, min(1200, len(text) // 3))
        omitted = len(text) - keep * 2
        if omitted <= 0:
            return text
        return f"{text[:keep]}\n[.. 省略 {omitted} 字 ..]\n{text[-keep:]}"

    head = lines[:head_lines]
    tail = lines[-tail_lines:]
    middle = lines[head_lines : len(lines) - tail_lines]
    hints = [ln for ln in middle if _ERROR_RE.search(ln) or _LOC_RE.search(ln)][:hint_lines]

    out = list(head)
    if len(middle) > 0:
        out.append(f"[.. 省略 {len(middle)} 行 ..]")
    if hints:
        out.append("[.. 其中的错误/定位行 ..]")
        out.extend(hints)
    out.extend(tail)
    return "\n".join(out)


@dataclass
class TrimReport:
    before: int = 0
    after: int = 0
    stripped_reasoning: int = 0
    stripped_images: int = 0
    digested: int = 0
    merged: int = 0
    dropped: int = 0
    hard_cut: int = 0
    spilled: int = 0
    reason: str = ""
    # 被删掉的原文。骨架生成器要拿它提炼 facts/rejected，
    # 只留个计数等于让模型对着空白猜。
    dropped_messages: list[dict] = field(default_factory=list)
    dropped_tokens: int = 0

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after)

    @property
    def touched(self) -> bool:
        return self.saved > 0

    def as_dict(self) -> dict:
        return {
            "before": self.before,
            "after": self.after,
            "saved": self.saved,
            "stripped_reasoning": self.stripped_reasoning,
            "stripped_images": self.stripped_images,
            "digested": self.digested,
            "merged": self.merged,
            "dropped": self.dropped,
            "hard_cut": self.hard_cut,
            "spilled": self.spilled,
            "reason": self.reason,
        }


# items 结构：[message, tokens, protected]
_Item = list


def _discard(it: _Item, total: int, rep: TrimReport) -> int:
    """丢掉一条并记账。删掉的内容要留下来，骨架生成器要读它。"""
    rep.dropped_tokens += it[1]
    rep.dropped_messages.append(it[0])
    return total - it[1]


def _next_is_tool(items: list[_Item], idx: int) -> bool:
    return idx + 1 < len(items) and items[idx + 1][0].get("role") == "tool"


class Trimmer:
    def __init__(
        self,
        cfg: ContextCfg,
        estimator: Estimator,
        spill: Callable[[str, str], str] | None = None,
        spill_batch: Callable[[list[str], str], list[str]] | None = None,
    ):
        self.cfg = cfg
        self.est = estimator
        self.spill = spill
        # 一次裁剪能压掉上百条工具输出。逐条落盘就是上百个文件，
        # 合并成一个文件、用行锚点定位，目录才不至于失控。
        self.spill_batch = spill_batch

    # ---- 对外 ----

    def fit(
        self,
        messages: list[dict],
        budget: int,
        target: int | None = None,
        tools_tokens: int = 0,
        keep_recent: int | None = None,
        force: bool = False,
    ) -> tuple[list[dict], TrimReport]:
        """把 messages 裁到预算内。返回 (新列表, 报告)。

        未超预算时原样返回同一个对象——这点很重要：不动就不破坏前缀缓存。
        """
        items: list[_Item] = [[dict(m), 0, False] for m in messages]
        for it in items:
            it[1] = self.est.message(it[0])
        total = int(tools_tokens + 8 + sum(it[1] for it in items))
        report = TrimReport(before=total, after=total)

        if not self.cfg.enabled:
            return messages, report
        if total <= budget and not force:
            return messages, report

        self._protect(items, keep_recent if keep_recent is not None else self.cfg.keep_recent)

        goal = target if target is not None else int(budget * self.cfg.target_ratio)
        # 每次压缩至少清掉 min_clear_tokens，否则不值得让缓存失效
        goal = min(goal, total - max(0, self.cfg.min_clear_tokens))
        goal = max(1024, goal)

        if force:
            report.reason = "force"
        elif total > budget:
            report.reason = "over_budget"

        steps: list[Callable[[list[_Item], int, int, TrimReport], int]] = []
        if self.cfg.strip_reasoning:
            steps.append(self._step_reasoning)
        if self.cfg.strip_images:
            steps.append(self._step_images)
        if self.cfg.tool_digest:
            steps.append(self._step_digest)
        steps.append(self._step_tidy)
        steps.append(self._step_drop)
        steps.append(self._step_hard)

        for step in steps:
            if total <= goal:
                break
            total = step(items, total, goal, report)

        # 结构修复无条件执行：即使已经压到目标，前面某一步也可能留下了
        # 不配对的 tool 消息，而那会让严格模板直接 400。
        total = self._step_orphans(items, total, goal, report)

        out = [it[0] for it in items]
        report.after = int(tools_tokens + 8 + sum(it[1] for it in items))
        if report.touched:
            out = self._mark(out, report)
            report.after = int(self.est.messages(out, tools_tokens))
            logger.info(
                "上下文裁剪 %s：%s -> %s tokens（省 %s），思维链 %s 条 / 图片 %s / 摘要 %s 条 / 丢 %s 条 / 硬截 %s 条",
                report.reason or "manual",
                report.before,
                report.after,
                report.saved,
                report.stripped_reasoning,
                report.stripped_images,
                report.digested,
                report.dropped,
                report.hard_cut,
            )
        return out, report

    # ---- 保护 ----

    def _protect(self, items: list[_Item], keep_recent: int) -> None:
        n = len(items)
        for i in range(min(3, n)):
            if items[i][0].get("role") == "system":
                items[i][2] = True
            else:
                break
        # 首条 user 是任务目标本身。之前只保护了「最后一条 user」，
        # 结果 _step_hard 会把目标整句删掉，裁剪后模型是在没有任务陈述的
        # 情况下继续干活——它只能从后续消息里反推意图。
        for i in range(n):
            if items[i][0].get("role") == "user":
                items[i][2] = True
                break
        for i in range(max(0, n - max(0, keep_recent)), n):
            items[i][2] = True
        # 最后一条 user 是当前任务，永远保留
        for i in range(n - 1, -1, -1):
            if items[i][0].get("role") == "user":
                items[i][2] = True
                break

    # ---- 各级裁剪 ----

    def _step_reasoning(self, items: list[_Item], total: int, goal: int, rep: TrimReport) -> int:
        for it in items:
            if total <= goal:
                break
            msg = it[0]
            if "reasoning_content" not in msg:
                continue
            new = {k: v for k, v in msg.items() if k != "reasoning_content"}
            size = self.est.message(new)
            total += size - it[1]
            it[0], it[1] = new, size
            rep.stripped_reasoning += 1
        return total

    def _step_images(self, items: list[_Item], total: int, goal: int, rep: TrimReport) -> int:
        for it in items:
            if total <= goal:
                break
            msg = it[0]
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            replaced, count = _strip_images(content)
            if not count:
                continue
            new = dict(msg)
            new["content"] = replaced
            size = self.est.message(new)
            total += size - it[1]
            it[0], it[1] = new, size
            rep.stripped_images += count
        return total

    def _step_digest(self, items: list[_Item], total: int, goal: int, rep: TrimReport) -> int:
        pending: list[tuple[_Item, str]] = []
        for it in items:
            if total <= goal:
                break
            if it[2]:
                continue
            msg = it[0]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if len(text) < 1200:
                continue

            short = "[网关压缩] 原始 %d 字，保留头尾与关键行\n%s" % (
                len(text),
                digest_output(
                    text,
                    head_lines=self.cfg.tool_head_lines,
                    tail_lines=self.cfg.tool_tail_lines,
                ),
            )
            # 先确认确实压小了再落盘，否则会为一堆没用的指针写文件
            probe = dict(msg)
            probe["content"] = short
            if self.est.message(probe) >= it[1] * 0.92:
                continue

            new = dict(msg)
            new["content"] = short
            size = self.est.message(new)
            if size >= it[1]:
                continue
            total += size - it[1]
            it[0], it[1] = new, size
            rep.digested += 1
            pending.append((it, text))

        # 落盘放到最后统一做：一次裁剪一个文件，而不是一百个
        if pending and (self.spill_batch or self.spill):
            texts = [text for _, text in pending]
            if self.spill_batch is not None:
                try:
                    refs = self.spill_batch(texts, "tool")
                except OSError:
                    refs = ["" for _ in texts]
            else:
                refs = []
                for text in texts:
                    try:
                        refs.append(self.spill(text, "tool") or "")
                    except OSError:
                        refs.append("")
            for (it, _), ref in zip(pending, refs):
                if not ref:
                    continue
                new = dict(it[0])
                new["content"] = f"{new['content']}\n[.. 完整输出已存档：{ref} ..]"
                size = self.est.message(new)
                total += size - it[1]
                it[0], it[1] = new, size
                rep.spilled += 1
        return total

    def _step_tidy(self, items: list[_Item], total: int, goal: int, rep: TrimReport) -> int:
        keep: list[_Item] = []
        for it in items:
            msg = it[0]
            empty = msg.get("role") == "assistant" and not msg.get("tool_calls") and not msg.get("content")
            if empty and not it[2]:
                total -= it[1]
                rep.merged += 1
                continue
            keep.append(it)
        items[:] = keep
        return total

    def _step_drop(self, items: list[_Item], total: int, goal: int, rep: TrimReport) -> int:
        """丢最旧的「assistant(tool_calls) + 其后连续 tool」整对。

        必须整对丢：只丢 tool 会留下悬空的 tool_call_id，chat 模板会报错。
        """
        while total > goal:
            victim: tuple[int, int] | None = None
            for idx, it in enumerate(items):
                if it[2] or it[0].get("role") != "assistant" or not it[0].get("tool_calls"):
                    continue
                end = idx + 1
                while end < len(items) and items[end][0].get("role") == "tool" and not items[end][2]:
                    end += 1
                if end > idx + 1:
                    victim = (idx, end)
                    break
            if victim is None:
                break
            start, end = victim
            for it in items[start:end]:
                total -= it[1]
                rep.dropped += 1
                rep.dropped_tokens += it[1]
                rep.dropped_messages.append(it[0])
            del items[start:end]
        return total

    def _step_hard(self, items: list[_Item], total: int, goal: int, rep: TrimReport) -> int:
        """兜底：从头丢，跳过受保护项。"""
        idx = 0
        while total > goal and idx < len(items):
            it = items[idx]
            if it[2]:
                idx += 1
                continue
            total -= it[1]
            rep.hard_cut += 1
            rep.dropped_tokens += it[1]
            rep.dropped_messages.append(it[0])
            del items[idx]
        return total

    def _step_orphans(self, items: list[_Item], total: int, goal: int, rep: TrimReport) -> int:
        """收拾硬截断留下的残骸。

        `_step_hard` 按位置删，不认配对：可能删掉带 tool_calls 的 assistant
        却留下它的 tool 结果。这种悬空 tool 消息（以及反过来、有 tool_calls
        却没有任何结果的 assistant）在 llama.cpp 上能过，在 vLLM / 严格
        Qwen 模板上直接 400。裁剪的产物必须仍是合法对话。
        """
        # 1) 删掉悬空的 tool 消息
        keep: list[_Item] = []
        expected: set[str] = set()
        for it in items:
            role = it[0].get("role")
            if role == "assistant":
                expected = {str(c.get("id")) for c in (it[0].get("tool_calls") or []) if isinstance(c, dict)}
                keep.append(it)
            elif role == "tool":
                cid = str(it[0].get("tool_call_id") or "")
                if not expected or (cid and cid not in expected):
                    total = _discard(it, total, rep)
                    continue
                expected.discard(cid)
                keep.append(it)
            else:
                expected = set()
                keep.append(it)
        items[:] = keep

        # 2) 有 tool_calls 却一个结果都没等到：摘掉 tool_calls，别让模板报错
        fixed: list[_Item] = []
        for idx, it in enumerate(items):
            if it[0].get("tool_calls") and not _next_is_tool(items, idx):
                new = {k: v for k, v in it[0].items() if k != "tool_calls"}
                if not str(new.get("content") or "").strip():
                    total = _discard(it, total, rep)
                    continue
                size = self.est.message(new)
                total += size - it[1]
                it[0], it[1] = new, size
            fixed.append(it)
        items[:] = fixed
        return total

    # ---- 留痕 ----

    def _mark(self, msgs: list[dict], rep: TrimReport) -> list[dict]:
        """在 system 里写一句「前面被压过」。

        不写的话模型会不知道历史有缺口，然后用「根据我们之前的讨论……」自信地编。
        挂到第一条 system 而不是新插一条，是为了不破坏 Qwen 这类模板对
        system 位置的要求。
        """
        note = (
            f"[网关] 本次请求前部有 {rep.dropped + rep.hard_cut} 条旧消息、"
            f"{rep.digested} 条工具输出被压缩（约省 {rep.saved} tokens）。"
            "缺失的细节不要凭空猜测，需要时用工具重新确认。"
        )
        out = list(msgs)
        for i, msg in enumerate(out):
            if msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    out[i] = {**msg, "content": f"{content.rstrip()}\n\n{note}"}
                elif isinstance(content, list):
                    out[i] = {**msg, "content": content + [{"type": "text", "text": note}]}
                else:
                    out[i] = {**msg, "content": note}
                return out
        out.insert(0, {"role": "system", "content": note})
        return out


def _strip_images(content: list) -> tuple[list, int]:
    out: list = []
    count = 0
    for part in content:
        if isinstance(part, dict) and str(part.get("type") or "") in _IMG_TYPES:
            count += 1
            out.append({"type": "text", "text": "[图片/视频已由网关移除]"})
        else:
            out.append(part)
    return out, count
