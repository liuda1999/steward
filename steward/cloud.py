# SPDX-License-Identifier: Apache-2.0
"""进阶模型客户端（通常是云端 API）+ 交付物解析。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .artifacts import Artifact, Store
from .config import EndpointCfg
from .prompts import CONSULT_SYSTEM_PROMPT, render_brief
from .responses import chat_to_responses, responses_to_chat
from .upstream import build_headers, join_url

logger = logging.getLogger("steward")


@dataclass
class ConsultResult:
    ok: bool
    text: str = ""
    error: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    not_sent: bool = False
    artifact: Artifact | None = None
    full_chars: int = 0


def flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""


class AdvancedClient:
    def __init__(self, cfg: EndpointCfg, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._transport = transport
        self._force_close = False
        self._client = self._build_client()

    def _build_client(self) -> httpx.AsyncClient:
        headers = build_headers(self.cfg.api_key, self.cfg.extra_headers)
        if self._force_close:
            headers["Connection"] = "close"
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.cfg.timeout_s, connect=15.0),
            headers=headers,
            transport=self._transport,
            limits=httpx.Limits(max_keepalive_connections=0) if self._force_close else httpx.Limits(),
        )

    async def _post(self, url: str, body: dict):
        try:
            resp = await self._client.post(url, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise
        if resp.status_code != 404 or not self.cfg.retry_404 or self._force_close:
            return resp
        logger.warning("进阶端点返回 404，疑似 keep-alive 复用问题，换短连接重试一次")
        self._force_close = True
        await self._client.aclose()
        self._client = self._build_client()
        return await self._client.post(url, json=body)

    async def consult(
        self,
        kind: str,
        ask: str,
        brief: str,
        max_brief_chars: int,
        max_result_chars: int,
        store: Store | None = None,
        turn_id: str = "",
        inline_chars: int = 800,
        refs_text: str = "",
    ) -> ConsultResult:
        prompt = render_brief(kind, ask, brief, max_brief_chars)
        if refs_text.strip():
            # 原文放在委托单后面：模型先有目标，再看材料，不至于被 500 行代码带跑
            prompt += (
                "\n\n## 参考文件\n"
                "以下文件由网关从本地磁盘原文读取，未经模型转述。行号可用于定位。\n"
                + refs_text
            )
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": CONSULT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        if self.cfg.thinking in ("off", "on"):
            payload["chat_template_kwargs"] = {"enable_thinking": self.cfg.thinking == "on"}

        started = time.perf_counter()
        use_responses = self.cfg.protocol == "responses"
        url = join_url(self.cfg.base_url, "responses" if use_responses else "chat/completions")
        body = chat_to_responses(payload) if use_responses else payload
        try:
            resp = await self._post(url, body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return ConsultResult(False, error=f"connect: {exc}", elapsed_ms=int((time.perf_counter() - started) * 1000), not_sent=True)
        except httpx.TimeoutException:
            return ConsultResult(False, error="timeout", elapsed_ms=int((time.perf_counter() - started) * 1000))
        except httpx.HTTPError as exc:
            return ConsultResult(False, error=f"transport: {exc}", elapsed_ms=int((time.perf_counter() - started) * 1000))

        elapsed = int((time.perf_counter() - started) * 1000)

        if resp.status_code >= 400:
            body_text = resp.text[:300].replace("\n", " ")
            # 4xx 是上游直接拒绝，没消耗任何 token，不该占配额
            return ConsultResult(
                False,
                error=f"http_{resp.status_code}: {body_text}",
                elapsed_ms=elapsed,
                not_sent=400 <= resp.status_code < 500,
            )

        try:
            data = resp.json()
        except ValueError:
            return ConsultResult(False, error="invalid_json", elapsed_ms=elapsed)

        if use_responses:
            try:
                data = responses_to_chat(data, self.cfg.model)
            except ValueError as exc:
                return ConsultResult(False, error=f"responses: {exc}", elapsed_ms=elapsed)

        choices = data.get("choices") or []
        if not choices:
            return ConsultResult(False, error="empty_choices", elapsed_ms=elapsed)

        text = flatten_content((choices[0].get("message") or {}).get("content")).strip()
        if not text:
            return ConsultResult(False, error="empty_content", elapsed_ms=elapsed)

        usage = data.get("usage")
        # 上游理论上会给 dict，但它是外部输入：给个 list 或字符串就 AttributeError，
        # 而那个异常会一路冒到配额结算之前，把一次调用变成"扣了钱没退"。
        if not isinstance(usage, dict):
            usage = {}
        artifact = None
        full_chars = len(text)

        # 长交付物落盘，上下文只收摘要 + 目录 + 路径。
        # 先存全文再生成摘要，避免被 max_result_chars 截掉后半截。
        if store is not None and store.worth_storing(text):
            try:
                artifact = store.save(text, kind=kind, turn=turn_id)
                text = store.digest(artifact, text, inline_chars)
            except OSError as exc:
                logger.warning("交付物存档失败，退回内联截断: %s", exc)
                artifact = None

        if artifact is None and len(text) > max_result_chars:
            text = text[:max_result_chars] + "\n\n[交付物过长，已截断]"

        return ConsultResult(
            True,
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            elapsed_ms=elapsed,
            artifact=artifact,
            full_chars=full_chars,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
