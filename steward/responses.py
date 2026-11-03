"""OpenAI Responses 协议 <-> Chat Completions 协议互转。

有些上游只暴露 /v1/responses（新版 llama.cpp、vLLM、OpenAI 官方）。
网关内部统一用 chat 结构，这里只在边界做翻译，业务代码不用感知。

注意：只处理非流式。网关自己也不向上游要流式。
"""

from __future__ import annotations

import time
from typing import Any


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                kind = item.get("type")
                if kind in ("text", "input_text", "output_text"):
                    parts.append(str(item.get("text") or ""))
                elif kind == "image_url":
                    parts.append("[image]")
                else:
                    parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(content)


def _image_part(item: dict) -> dict | None:
    image = item.get("image_url")
    url = image.get("url") if isinstance(image, dict) else image
    if not url:
        return None
    return {"type": "input_image", "image_url": str(url)}


def chat_to_responses(payload: dict) -> dict:
    """把 chat/completions 请求体翻成 responses 请求体。"""
    instructions: list[str] = []
    items: list[dict] = []

    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")

        if role in ("system", "developer"):
            text = _text_of(content)
            if text:
                instructions.append(text)
            continue

        if role == "assistant":
            text = _text_of(content)
            if text:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
            continue

        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id") or "",
                    "output": _text_of(content),
                }
            )
            continue

        if isinstance(content, list):
            parts: list[dict] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "image_url":
                        part = _image_part(item)
                        if part:
                            parts.append(part)
                    else:
                        parts.append({"type": "input_text", "text": _text_of(item)})
                else:
                    parts.append({"type": "input_text", "text": str(item)})
            items.append({"type": "message", "role": "user", "content": parts})
        else:
            items.append({"type": "message", "role": "user", "content": _text_of(content)})

    out: dict = {"model": payload.get("model"), "input": items, "stream": False}
    if instructions:
        out["instructions"] = "\n\n".join(instructions)

    tools: list[dict] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") or {}
        tools.append(
            {
                "type": "function",
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    if tools:
        out["tools"] = tools
        choice = payload.get("tool_choice")
        if isinstance(choice, dict):
            name = (choice.get("function") or {}).get("name")
            out["tool_choice"] = {"type": "function", "name": name} if name else "auto"
        elif choice in ("auto", "none", "required"):
            out["tool_choice"] = choice

    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        out["top_p"] = payload["top_p"]
    if payload.get("max_tokens") is not None:
        out["max_output_tokens"] = payload["max_tokens"]
    # llama.cpp 的 /v1/responses 也认这个（用来关掉推理型模型的思维链）
    if payload.get("chat_template_kwargs"):
        out["chat_template_kwargs"] = payload["chat_template_kwargs"]
    return out


def responses_to_chat(data: dict, fallback_model: str = "") -> dict:
    """把 responses 响应体翻成 chat/completions 响应体。"""
    if data.get("status") == "failed":
        err = data.get("error") or {}
        raise ValueError(str(err.get("message") or "responses 调用失败"))

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: list[dict] = []

    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                    text_parts.append(str(part.get("text") or ""))
        elif kind == "function_call":
            calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            )
        elif kind == "reasoning":
            for part in item.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "reasoning_text":
                    reasoning_parts.append(str(part.get("text") or ""))

    message: dict = {"role": "assistant", "content": "".join(text_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if calls:
        message["tool_calls"] = calls

    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    finish = "tool_calls" if calls else "stop"
    if data.get("status") == "incomplete":
        finish = "length"

    return {
        "id": data.get("id") or f"resp-{int(time.time())}",
        "object": "chat.completion",
        "created": data.get("created_at") or int(time.time()),
        "model": data.get("model") or fallback_model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": int(usage.get("total_tokens") or (prompt_tokens + completion_tokens)),
        },
    }
