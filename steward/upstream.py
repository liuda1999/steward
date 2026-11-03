"""基础模型客户端（OpenAI 兼容，支持 chat 与 responses 两种协议）。

带一个"404 自愈"：某些 llama.cpp 部署在 keep-alive 连接上会把第 2、4… 个请求
当成不存在的路径，返回 404（已在真实设备上复现：200/404/200/404…）。
一旦遇到，就切到短连接模式，后续请求不再复用连接。
"""

from __future__ import annotations

import logging

import httpx

from .config import EndpointCfg
from .responses import chat_to_responses, responses_to_chat

logger = logging.getLogger("steward")


def join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def build_headers(api_key: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    return headers


class BaseClient:
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

    async def _send(self, payload: dict) -> dict:
        if self.cfg.protocol == "responses":
            url = join_url(self.cfg.base_url, "responses")
            body = chat_to_responses(payload)
        else:
            url = join_url(self.cfg.base_url, "chat/completions")
            body = payload

        resp = await self._client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        if self.cfg.protocol == "responses":
            return responses_to_chat(data, self.cfg.model)
        return data

    async def chat(self, payload: dict) -> dict:
        try:
            return await self._send(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404 or not self.cfg.retry_404 or self._force_close:
                raise
            logger.warning("基础端点返回 404，疑似 keep-alive 复用问题，换短连接重试一次")
            self._force_close = True
            await self._client.aclose()
            self._client = self._build_client()
            return await self._send(payload)

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------- 探测（CLI 用，同步）


def _root_of(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def probe(ep: EndpointCfg, timeout: float = 10.0) -> dict:
    """探一下上游是什么、有哪些模型、上下文多大、key 是否被接受。

    只读，不产生推理开销。两个请求都会带上 extra_headers，兼容反代鉴权。
    """
    info: dict = {
        "ok": False,
        "status": 0,
        "models": [],
        "owned_by": "",
        "context": 0,
        "slots": 0,
        "detail": "",
    }
    headers = build_headers(ep.api_key, ep.extra_headers)
    base = ep.base_url.rstrip("/")

    try:
        resp = httpx.get(f"{base}/models", headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        info["detail"] = f"{type(exc).__name__}: {exc}"
        return info

    info["status"] = resp.status_code
    if resp.status_code >= 400:
        info["detail"] = f"HTTP {resp.status_code}: {resp.text[:160]}"
        return info

    try:
        data = resp.json()
    except ValueError:
        info["detail"] = "返回不是 JSON"
        return info

    items = data.get("data") or data.get("models") or []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("model") or item.get("name")
        if model_id:
            info["models"].append(str(model_id))
        if not info["owned_by"]:
            info["owned_by"] = str(item.get("owned_by") or item.get("type") or "")

    # llama.cpp 的 /props：上下文、槽位
    try:
        props = httpx.get(f"{_root_of(base)}/props", headers=headers, timeout=timeout)
        if props.status_code < 400:
            body = props.json()
            gen = body.get("default_generation_settings") or {}
            info["context"] = int(gen.get("n_ctx") or body.get("n_ctx") or 0)
            info["slots"] = int(body.get("total_slots") or 0)
    except (httpx.HTTPError, ValueError):
        pass

    info["ok"] = True
    return info
