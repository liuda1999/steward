# SPDX-License-Identifier: Apache-2.0
"""端口策略与空槽位。

这两件事都属于「配错了不会报错，只会莫名其妙不好用」的类型：

- 端口 8080 被别的服务占了，报错只有一句 address in use，不告诉你是谁；
- 进阶槽位空着时，旧实现既不阻止启动、又把 500 tokens 的工具定义照发，
  模型看得见一个永远会拒绝它的工具。

所以两者都需要被断言固定下来，而不只是写在文档里。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from steward.__main__ import port_hint
from steward.config import (
    COMMON_PORTS,
    DEFAULT_PORT,
    AppCfg,
    AuthCfg,
    ContextCfg,
    EndpointCfg,
    GuardCfg,
    ServerCfg,
    looks_sequential,
)
from steward.server import create_app


# ---------------------------------------------------------------- 端口策略


def test_default_port_avoids_common_ports():
    """80/443/8080 这类端口几乎总被占，默认值不能落在里面。"""
    assert DEFAULT_PORT not in COMMON_PORTS
    assert DEFAULT_PORT not in (80, 443, 8000, 8080, 8888, 9000, 11434)


def test_default_port_is_not_sequential():
    """12345 / 56789 这种不仅难辨认，还常常被别人随手占用。"""
    assert not looks_sequential(DEFAULT_PORT)


def test_default_port_sits_in_the_safe_band():
    """低于 1024 要 root，高于 49151 会撞上 Windows 的临时端口段。"""
    assert 1024 <= DEFAULT_PORT <= 49151


@pytest.mark.parametrize("port", [12345, 54321, 56789, 98765, 1234, 4321, 9012])
def test_sequential_ports_are_detected(port):
    assert looks_sequential(port)


@pytest.mark.parametrize("port", [27418, 19267, 8080, 11434, 7021, 55555])
def test_non_sequential_ports_are_not_flagged(port):
    assert not looks_sequential(port)


def test_port_hint_explains_why():
    """提示必须说出「为什么不行」，只说「不建议」等于没说。"""
    assert "常见端口" in port_hint(8080)
    assert "连续数字" in port_hint(12345)
    assert "1024" in port_hint(80)  # 80 既常见又需要 root，两条理由都要说
    assert "常见端口" in port_hint(80)
    assert "49152" in port_hint(52000)
    assert port_hint(DEFAULT_PORT) == ""


def test_config_warns_about_conflicting_port(tmp_path):
    cfg = _cfg(tmp_path, server=ServerCfg(port=8080))
    assert any("常见端口" in w for w in cfg.warnings())
    assert cfg.validate() == []  # 只提示，不拦


def test_config_rejects_out_of_range_port(tmp_path):
    cfg = _cfg(tmp_path, server=ServerCfg(port=70000))
    assert any("server.port" in p for p in cfg.validate())


def test_shipped_yaml_uses_the_safe_default():
    """随包配置也得守规矩，否则 init 出来的就是 8080。"""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "steward" / "config.example.yaml").read_text(encoding="utf-8")
    assert f"port: {DEFAULT_PORT}" in text
    assert "8010" not in text


# ---------------------------------------------------------------- 空槽位


def _cfg(tmp_path, **overrides) -> AppCfg:
    params = dict(
        state_file=str(tmp_path / "usage.json"),
        max_calls_per_turn=2,
        max_tool_rounds=2,
        min_interval_s=0,
        daily_limit=10,
        dedupe=False,
    )
    params.update(overrides.pop("guard", {}))
    base = dict(
        auth=AuthCfg(mode="off", keys_file=str(tmp_path / "keys.json")),
        base=EndpointCfg(base_url="http://local.test/v1", model="local-m", timeout_s=10),
        advanced=EndpointCfg(base_url="http://cloud.test/v1", model="cloud-m", timeout_s=10),
        guard=GuardCfg(**params),
        context=ContextCfg(
            state_file=str(tmp_path / "ctx.json"),
            spine_state_file=str(tmp_path / "spine.json"),
            ctx_limit=32768,
        ),
    )
    base.update(overrides)
    return AppCfg(**base)


class Echo:
    """只回一句话的假模型，够用来观察我们发了什么。"""

    def __init__(self):
        self.sent: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/props", "/tokenize"):
            return httpx.Response(404, json={"error": "unsupported"})
        body = json.loads(request.content or b"{}")
        self.sent.append(body)
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "好。"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 2},
            },
        )


def _ask(app) -> httpx.Response:
    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as client:
            return await client.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "你好"}]})

    return asyncio.run(go())


def test_empty_advanced_slot_still_serves(tmp_path):
    """进阶槽位空着，网关照常接活。纯本地也是一等公民。"""
    up = Echo()
    cfg = _cfg(tmp_path, advanced=EndpointCfg())  # 两个字段都空
    app = create_app(cfg, httpx.MockTransport(up.handler))
    resp = _ask(app)
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "好。"


def test_empty_advanced_slot_does_not_inject_tool(tmp_path):
    """空槽位不注入工具：模型看不到它，也就不会去调它。"""
    up = Echo()
    app = create_app(_cfg(tmp_path, advanced=EndpointCfg()), httpx.MockTransport(up.handler))
    _ask(app)
    assert "tools" not in up.sent[-1]
    # 不注入工具时连 system 提示也不发：默认那句话就是在介绍这个工具。
    systems = [m for m in up.sent[-1]["messages"] if m.get("role") == "system"]
    assert not any("consult_cloud" in str(m.get("content") or "") for m in systems)


def test_empty_advanced_slot_saves_tokens(tmp_path):
    """省下来的就是那把工具定义，约 500 tokens —— 这是空槽位的实际收益。"""
    import json

    from steward.context import count_tokens
    from steward.prompts import build_consult_tool

    with_tool = count_tokens(json.dumps(build_consult_tool(), ensure_ascii=False))

    up = Echo()
    app = create_app(_cfg(tmp_path, advanced=EndpointCfg()), httpx.MockTransport(up.handler))
    _ask(app)
    assert not up.sent[-1].get("tools")
    assert with_tool > 400  # 工具定义本身确实不便宜，省它是有意义的


def test_full_advanced_slot_injects_tool(tmp_path):
    """对照组：槽位填满时工具必须在，否则整套咨询机制就断了。"""
    up = Echo()
    app = create_app(_cfg(tmp_path), httpx.MockTransport(up.handler))
    _ask(app)
    names = [(t.get("function") or {}).get("name") for t in up.sent[-1].get("tools") or []]
    assert "consult_cloud" in names


def test_partial_advanced_slot_counts_as_empty(tmp_path):
    """只填了地址没填模型名，等于没有——半个槽位比空槽位更危险。"""
    up = Echo()
    cfg = _cfg(tmp_path, advanced=EndpointCfg(base_url="http://cloud.test/v1"))
    app = create_app(cfg, httpx.MockTransport(up.handler))
    _ask(app)
    assert "tools" not in up.sent[-1]
    assert any("进阶槽位空着" in w for w in cfg.warnings())


def test_empty_slots_are_reported_in_admin(tmp_path):
    """/health 要能一眼看出当前是纯本地模式。"""
    up = Echo()
    app = create_app(_cfg(tmp_path, advanced=EndpointCfg()), httpx.MockTransport(up.handler))

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as client:
            return await client.get("/health")

    data = asyncio.run(go()).json()
    assert data["advanced"]["configured"] is False


# ---------------------------------------------------------------- 端口占用探测


def test_port_owner_is_silent_when_free():
    """空闲端口必须返回空串，否则每次启动都会被自己拦住。"""
    import socket

    from steward.__main__ import _port_owner

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free = probe.getsockname()[1]
    probe.close()
    assert _port_owner("127.0.0.1", free) == ""


def test_port_owner_reports_who_holds_it():
    """被占用时不仅要说「占了」，还要说「谁占的」。

    这里踩过两个坑：一是查不到进程名就返回空串，被调用方当成端口空闲；
    二是中文 Windows 的 netstat 输出是 GBK，按 utf-8 解会在 reader 线程里
    抛异常，看着像「命令没有输出」。
    """
    import socket

    from steward.__main__ import _port_owner

    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        detail = _port_owner("127.0.0.1", port)
    finally:
        holder.close()
    assert detail, "端口被占用却报成空闲"
    assert "PID" in detail or "未知" in detail


# ---------------------------------------------------------------- 版本


def test_version_is_semver():
    """版本号要能被工具解析，别写成 "1.0" 或 "v1.0.0"。"""
    import re

    import steward

    assert re.fullmatch(r"\d+\.\d+\.\d+", steward.__version__), steward.__version__


def test_cli_reports_version(capsys):
    """`--version` 是顶层参数，不能被当成子命令的参数塞进 serve。"""
    from steward.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "steward" in capsys.readouterr().out


def test_health_reports_version(tmp_path):
    """报版本号应该是 /health 的义务：用户提问第一句总是「我这是哪个版本」。"""
    import steward

    up = Echo()
    app = create_app(_cfg(tmp_path), httpx.MockTransport(up.handler))

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as client:
            return await client.get("/health")

    assert asyncio.run(go()).json()["version"] == steward.__version__
