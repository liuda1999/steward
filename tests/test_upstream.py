# SPDX-License-Identifier: Apache-2.0
"""上游客户端：404 自愈、协议分发。"""

from __future__ import annotations

import asyncio

import httpx

from steward.config import EndpointCfg
from steward.upstream import BaseClient


def _chat(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_base_client_retries_404_on_fresh_connection():
    """真实设备上见过 200/404/200/404：第二次复用连接就 404。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("connection") or "")
        if len(seen) == 2:
            return httpx.Response(404, json={"error": {"message": "File Not Found"}})
        return httpx.Response(200, json=_chat("ok"))

    cfg = EndpointCfg(base_url="http://upstream.test/v1", model="m", retry_404=True)
    client = BaseClient(cfg, httpx.MockTransport(handler))

    async def go():
        try:
            return [await client.chat({"messages": []}) for _ in range(3)]
        finally:
            await client.aclose()

    results = asyncio.run(go())

    assert [r["choices"][0]["message"]["content"] for r in results] == ["ok", "ok", "ok"]
    assert len(seen) == 4  # 第 2 次 404 后重试了一次
    assert seen[2] == "close"  # 重试走短连接
    assert seen[3] == "close"  # 之后保持短连接，不再复发


def test_base_client_does_not_retry_when_disabled():
    seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        return httpx.Response(404, json={"error": {"message": "File Not Found"}})

    cfg = EndpointCfg(base_url="http://upstream.test/v1", model="m", retry_404=False)
    client = BaseClient(cfg, httpx.MockTransport(handler))

    async def go():
        try:
            await client.chat({"messages": []})
        finally:
            await client.aclose()

    try:
        asyncio.run(go())
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("应该抛出 404")
    assert seen == 1


def test_base_client_responses_protocol():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        )

    cfg = EndpointCfg(base_url="http://upstream.test/v1", model="m", protocol="responses")
    client = BaseClient(cfg, httpx.MockTransport(handler))

    async def go():
        try:
            return await client.chat(
                {"model": "m", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]}
            )
        finally:
            await client.aclose()

    data = asyncio.run(go())

    assert captured["url"].endswith("/v1/responses")
    assert '"instructions"' in captured["body"]
    assert data["choices"][0]["message"]["content"] == "hi"
