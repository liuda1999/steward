# SPDX-License-Identifier: Apache-2.0
"""云端 API 与本地专有参数。

`chat_template_kwargs.enable_thinking` 是 llama.cpp / vLLM / Qwen 认识的
扩展开关。严格的 OpenAI 兼容云端（OpenAI、Azure）会因为不认识的参数直接
400：

    Unrecognized request argument supplied: chat_template_kwargs

两个槽位都可能指向云端 API（用户明确提过这一点），所以这个参数不能无脑
发。做法不是「预先不发」——本地服务需要它——而是**撞上 400 就摘掉重发
一次**，并记住这个端点不认，以后不再带。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from steward.cloud import AdvancedClient
from steward.config import EndpointCfg
from steward.netpolicy import mentions_unknown_param
from steward.upstream import BaseClient

CLOUD_ERROR = {
    "error": {
        "message": "Unrecognized request argument supplied: chat_template_kwargs",
        "type": "invalid_request_error",
    }
}


def test_recognizes_unknown_param_errors():
    assert mentions_unknown_param("Unrecognized request argument supplied: chat_template_kwargs")
    assert mentions_unknown_param("unknown parameter: chat_template_kwargs")
    assert mentions_unknown_param("Unsupported parameter 'chat_template_kwargs'")
    # 别的 400 不该被误判 —— 那种情况摘掉参数也没用
    assert not mentions_unknown_param("Unrecognized request argument supplied: foo")
    assert not mentions_unknown_param("invalid api key")
    assert not mentions_unknown_param("")


def test_advanced_strips_param_and_retries():
    """第一次 400，摘掉参数重发成功。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append(body)
        if "chat_template_kwargs" in body:
            return httpx.Response(400, json=CLOUD_ERROR)
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": "cloud",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        })

    client = AdvancedClient(
        EndpointCfg(base_url="http://cloud.test/v1", model="m", thinking="off"),
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(client.consult("design", "q", "goal: g", 100, 100))
    asyncio.run(client.aclose())

    assert result.ok is True
    assert len(seen) == 2
    assert "chat_template_kwargs" in seen[0]
    assert "chat_template_kwargs" not in seen[1]


def test_advanced_remembers_and_stops_sending():
    """记住之后不再带，省掉每轮多出来的那个 400。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append(body)
        if "chat_template_kwargs" in body:
            return httpx.Response(400, json=CLOUD_ERROR)
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": "cloud",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        })

    client = AdvancedClient(
        EndpointCfg(base_url="http://cloud.test/v1", model="m", thinking="off"),
        transport=httpx.MockTransport(handler),
    )
    for _ in range(3):
        asyncio.run(client.consult("design", "q", "goal: g", 100, 100))
    asyncio.run(client.aclose())

    # 第一次带、之后不带：总共 4 次请求（1 次 400 + 1 次重发 + 2 次干净请求）
    assert len(seen) == 4
    assert sum(1 for b in seen if "chat_template_kwargs" in b) == 1


def test_local_upstream_keeps_the_param():
    """本地服务认这个参数，不能一刀切地不发。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append(body)
        return httpx.Response(200, json={
            "id": "l", "object": "chat.completion", "created": 0, "model": "local",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        })

    client = BaseClient(
        EndpointCfg(base_url="http://local.test/v1", model="m"),
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(client.chat({
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "chat_template_kwargs": {"enable_thinking": False},
    }))
    asyncio.run(client.aclose())
    assert len(seen) == 1
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
