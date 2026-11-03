# SPDX-License-Identifier: Apache-2.0
"""Responses 协议互转。"""

from __future__ import annotations

import pytest

from steward.responses import chat_to_responses, responses_to_chat


def test_chat_to_responses_maps_roles_and_tools():
    payload = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "问题"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "consult", "arguments": '{"ask":"x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "consult",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": "auto",
        "max_tokens": 100,
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    body = chat_to_responses(payload)

    assert body["chat_template_kwargs"] == {"enable_thinking": False}

    assert body["instructions"] == "规则"
    assert body["max_output_tokens"] == 100
    assert body["temperature"] == 0.2
    assert body["tools"][0]["name"] == "consult"
    assert body["tool_choice"] == "auto"
    assert [i["type"] for i in body["input"]] == ["message", "function_call", "function_call_output"]
    assert body["input"][2]["call_id"] == "call_1"
    assert body["stream"] is False


def test_chat_to_responses_named_tool_choice():
    body = chat_to_responses(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "consult", "parameters": {}}}],
            "tool_choice": {"type": "function", "function": {"name": "consult"}},
        }
    )
    assert body["tool_choice"] == {"type": "function", "name": "consult"}


def test_responses_to_chat_reads_text_reasoning_and_calls():
    data = {
        "id": "resp_1",
        "created_at": 123,
        "model": "m",
        "status": "completed",
        "output": [
            {
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": "想一想"}],
            },
            {"type": "message", "content": [{"type": "output_text", "text": "答案"}]},
            {"type": "function_call", "call_id": "call_9", "name": "consult", "arguments": '{"a":1}'},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }

    chat = responses_to_chat(data, "fallback")

    msg = chat["choices"][0]["message"]
    assert msg["content"] == "答案"
    assert msg["reasoning_content"] == "想一想"
    assert msg["tool_calls"][0]["id"] == "call_9"
    assert chat["choices"][0]["finish_reason"] == "tool_calls"
    assert chat["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert chat["model"] == "m"


def test_responses_incomplete_maps_to_length():
    chat = responses_to_chat(
        {"status": "incomplete", "output": [{"type": "message", "content": [{"type": "output_text", "text": "半截"}]}]}
    )
    assert chat["choices"][0]["finish_reason"] == "length"


def test_responses_failed_raises():
    with pytest.raises(ValueError, match="额度不足"):
        responses_to_chat({"status": "failed", "error": {"message": "额度不足"}})
