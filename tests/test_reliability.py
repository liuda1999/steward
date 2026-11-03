# SPDX-License-Identifier: Apache-2.0
"""可靠性审计：网关 / 鉴权 / 配额 / 裁剪。

这四块的问题都属于「平时看不出来，出事就是静默的」：
配额多扣一次没人发现，鉴权漏一个分支只是少拒绝一次请求，
裁剪把结构弄坏则要等上游报 400。所以断言都对着失败路径写。

对照用的输入尽量恶劣：并发、畸形 token、上游抛异常、客户端自带同名工具。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import httpx
import pytest

from steward.cloud import AdvancedClient
from steward.config import (
    AppCfg,
    ArtifactsCfg,
    AuthCfg,
    ContextCfg,
    EndpointCfg,
    GuardCfg,
)
from steward.context import Estimator, Trimmer
from steward.config import ContextCfg as Ctx
from steward.guard import Guard
from steward.keys import KeyStore
from steward.server import create_app


# ==================================================================
# 配额
# ==================================================================


def _guard(tmp_path, **kw) -> Guard:
    params = dict(
        state_file=str(tmp_path / "usage.json"),
        max_calls_per_turn=2,
        daily_limit=5,
        min_interval_s=0,
        dedupe=False,
        similarity=0.92,
    )
    params.update(kw)
    return Guard(GuardCfg(**params))


def _ask(i: int) -> tuple[str, str]:
    return f"问题 {i}", f"goal: 目标 {i}\ngiven: 事实 {i}"


def test_concurrent_reserve_never_exceeds_daily_limit(tmp_path):
    """10 个并发请求抢 3 个额度：锁必须让恰好 3 个通过。

    如果锁失效，`_used_today` 的读-改-写会交错，全都会看到"还没满"。
    """
    g = _guard(tmp_path, daily_limit=3, max_calls_per_turn=99)

    async def go():
        turns = [g.new_turn("k") for _ in range(10)]
        return await asyncio.gather(
            *(g.reserve(t, "design", *_ask(i)) for i, t in enumerate(turns))
        )

    results = asyncio.run(go())
    allowed = sum(1 for r in results if r.allowed)
    assert allowed == 3, f"应放行 3 个，实际 {allowed}"
    assert asyncio.run(g.snapshot())["used_today"] == 3


def test_concurrent_reserve_never_exceeds_turn_limit(tmp_path):
    """同一个 turn 内并发申请：上限同样不能被绕过。"""
    g = _guard(tmp_path, daily_limit=99, max_calls_per_turn=2)

    async def go():
        turn = g.new_turn("k")
        return await asyncio.gather(
            *(g.reserve(turn, "design", *_ask(i)) for i in range(8))
        )

    results = asyncio.run(go())
    assert sum(1 for r in results if r.allowed) == 2


def test_turn_limit_checked_before_daily_limit(tmp_path):
    """两道闸的顺序要稳定，否则提示语会指错原因。"""
    g = _guard(tmp_path, max_calls_per_turn=1, daily_limit=1)

    async def go():
        turn = g.new_turn("k")
        first = await g.reserve(turn, "design", *_ask(0))
        second = await g.reserve(turn, "design", *_ask(1))
        return first, second

    first, second = asyncio.run(go())
    assert first.allowed
    assert second.code == "turn_limit", f"应报 turn_limit，实际 {second.code}"


def test_daily_limit_survives_restart(tmp_path):
    """配额落盘，重启不清零 —— 否则重启就能无限续命。"""
    g = _guard(tmp_path, daily_limit=2)
    asyncio.run(g.reserve(g.new_turn("k"), "design", *_ask(0)))

    g2 = _guard(tmp_path, daily_limit=2)
    snap = asyncio.run(g2.snapshot())
    assert snap["used_today"] == 1
    assert snap["remaining_today"] == 1


def test_new_day_resets_counter(tmp_path):
    """跨天自动归零，不依赖手动 reset。"""
    g = _guard(tmp_path, daily_limit=2)
    asyncio.run(g.reserve(g.new_turn("k"), "design", *_ask(0)))
    g._day = date.today() - timedelta(days=1)  # 模拟昨天的状态
    snap = asyncio.run(g.snapshot())
    assert snap["used_today"] == 0


def test_refund_returns_exactly_one(tmp_path):
    g = _guard(tmp_path, daily_limit=5)

    async def go():
        turn = g.new_turn("k")
        await g.reserve(turn, "design", *_ask(0))
        await g.refund(turn)
        return await g.snapshot()

    snap = asyncio.run(go())
    assert snap["used_today"] == 0


def test_dedupe_does_not_swallow_genuinely_different_questions(tmp_path):
    """去重不能误伤：内容不同的委托单必须都能过。"""
    g = _guard(tmp_path, daily_limit=10, dedupe=True, max_calls_per_turn=10)
    asks = [
        ("缓存穿透怎么防", "goal: 挡住不存在的 key\ngiven: QPS 2k"),
        ("分片键按什么选", "goal: 避免热点\ngiven: 4 亿行"),
        ("一致性哈希迁移顺序", "goal: 扩容不中断\ngiven: 8 节点"),
    ]

    async def go():
        turn = g.new_turn("k")
        return [await g.reserve(turn, "design", a, b) for a, b in asks]

    assert [r.allowed for r in asyncio.run(go())] == [True, True, True]


def test_dedupe_catches_rephrasing(tmp_path):
    """换个标点和空格的同一个问题，应该被认出来。"""
    g = _guard(tmp_path, daily_limit=10, dedupe=True, max_calls_per_turn=10)

    async def go():
        turn = g.new_turn("k")
        a = await g.reserve(turn, "design", "缓存怎么分层？", "goal: 提升读性能\ngiven: QPS 2k")
        b = await g.reserve(turn, "design", "缓存  怎么分层", "goal: 提升读性能\ngiven: QPS 2k")
        return a, b

    a, b = asyncio.run(go())
    assert a.allowed and not b.allowed and b.code == "duplicate"


# ==================================================================
# 鉴权
# ==================================================================


def test_key_is_never_stored_in_plaintext(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    token, rec = store.issue("test")
    raw = (tmp_path / "keys.json").read_text(encoding="utf-8")
    assert token not in raw
    assert rec.hash in raw


def test_verify_rejects_malformed_tokens(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    token, _ = store.issue("test")
    bad = [
        "",
        "st-",
        "st-1234567",  # id 不够长
        "st-12345678",  # 缺分隔符后的 secret
        "st-12345678-",
        "nokey-abcdefgh-xxxx",
        token[:-1],  # 少一个字符
        token + "x",
        token.replace("st-", "cb_", 1),
    ]
    for t in bad:
        assert store.verify(t) is None, f"不该通过：{t!r}"
    assert store.verify(token) is not None


def test_revoked_key_stops_working(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    token, rec = store.issue("test")
    assert store.verify(token) is not None
    store.revoke(rec.id)
    assert store.verify(token) is None


def test_verify_key_id_lookup_does_not_leak_disabled_keys(tmp_path):
    """禁用后即使 secret 对也不能过 —— 别让比对绕过 enabled 判断。"""
    store = KeyStore(tmp_path / "keys.json")
    token, rec = store.issue("test")
    rec.enabled = False
    assert store.verify(token) is None


# ==================================================================
# 网关：异常路径
# ==================================================================


class _UpstreamTool:
    """本地模型第一轮就调 consult_cloud，之后正常收尾。"""

    def __init__(self):
        self.local: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 32000}, "total_slots": 1})
        if request.url.path == "/tokenize":
            return httpx.Response(404, json={"error": "unsupported"})
        body = json.loads(request.content or b"{}")
        self.local.append(body)
        if sum(1 for m in body.get("messages") or [] if m.get("role") == "tool") == 0:
            return httpx.Response(200, json={
                "id": "l", "object": "chat.completion", "created": 0, "model": "local",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {
                        "name": "consult_cloud",
                        "arguments": json.dumps(
                            {"kind": "design", "ask": "缓存怎么分层", "brief": "goal: 提升读性能\ngiven: QPS 2k"},
                            ensure_ascii=False,
                        ),
                    }}],
                }, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 20},
            })
        return httpx.Response(200, json={
            "id": "l", "object": "chat.completion", "created": 0, "model": "local",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "本地做完了。"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 420, "completion_tokens": 12},
        })


def _cfg(tmp_path, tools_free: bool = False) -> AppCfg:
    return AppCfg(
        auth=AuthCfg(mode="off", keys_file=str(tmp_path / "keys.json")),
        base=EndpointCfg(base_url="http://local.test/v1", model="local", api_key="k", timeout_s=10),
        advanced=EndpointCfg(base_url="http://cloud.test/v1", model="cloud", api_key="k", timeout_s=10),
        guard=GuardCfg(
            state_file=str(tmp_path / "usage.json"),
            max_calls_per_turn=2, max_tool_rounds=3, min_interval_s=0, daily_limit=5, dedupe=False,
        ),
        context=Ctx(state_file=str(tmp_path / "ctx.json"), spine_state_file=str(tmp_path / "spine.json"), spine=False),
        artifacts=ArtifactsCfg(dir=str(tmp_path / "art"), enabled=True),
    )


def _post(app, payload: dict) -> httpx.Response:
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            return await c.post("/v1/chat/completions", json=payload)

    return asyncio.run(go())


def test_unexpected_exception_refunds_quota_and_keeps_request_alive(tmp_path, monkeypatch):
    """进阶模型抛非预期异常时：配额要退，请求不能挂。

    修复前这里会把异常冒到最外层 → 整请求 500，且 quota 已经扣了。
    """
    async def boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(AdvancedClient, "consult", boom)

    up = _UpstreamTool()
    app = create_app(_cfg(tmp_path), httpx.MockTransport(up.handler))
    resp = _post(app, {"model": "x", "messages": [{"role": "user", "content": "做个缓存"}]})

    assert resp.status_code == 200, "异常不该让整请求挂掉"
    assert resp.json()["x_steward"]["remaining_today"] == 5, "失败调用没退还配额"

    tool_msgs = [m for m in up.local[-1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs and "未消耗额度" in str(tool_msgs[-1].get("content"))


def test_malformed_usage_does_not_lose_the_answer(tmp_path):
    """上游 usage 给成 list：应当照常拿到交付物，而不是退化成"调用失败"。

    只断言 200 不够——外层的 try/except 也会把异常兜成 200，但那样用户的
    答案就丢了、额度也退了。真正要保证的是「答案还在，钱照扣」。
    """
    class Weird(_UpstreamTool):
        def handler(self, request):
            if request.url.path in ("/props", "/tokenize"):
                return super().handler(request)
            body = json.loads(request.content or b"{}")
            if request.url.host == "cloud.test":
                return httpx.Response(200, json={
                    "id": "c", "object": "chat.completion", "created": 0, "model": "cloud",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "## 1. 结论\n用两级缓存"},
                                 "finish_reason": "stop"}],
                    "usage": ["not", "a", "dict"],
                })
            return super().handler(request)

    up = Weird()
    app = create_app(_cfg(tmp_path), httpx.MockTransport(up.handler))
    resp = _post(app, {"model": "x", "messages": [{"role": "user", "content": "做个缓存"}]})

    assert resp.status_code == 200
    # 答案必须回灌给本地模型，而不是变成一句"调用异常"
    assert "用两级缓存" in json.dumps(up.local[-1]["messages"], ensure_ascii=False)
    # 调用确实发生了，额度就该扣（不该被 refund）
    assert resp.json()["x_steward"]["remaining_today"] == 4


def test_client_owned_tool_suppresses_gateway_prompt(tmp_path):
    """客户端自己带 consult_cloud 时，system 不该再讲"外部专家"和配额。"""
    up = _UpstreamTool()
    app = create_app(_cfg(tmp_path), httpx.MockTransport(up.handler))
    _post(app, {
        "model": "x",
        "messages": [{"role": "user", "content": "你好"}],
        "tools": [{"type": "function", "function": {
            "name": "consult_cloud",
            "description": "客户端自己的定义",
            "parameters": {"type": "object", "properties": {}},
        }}],
    })
    system = str(up.local[0]["messages"][0].get("content") or "")
    assert "外部专家" not in system
    assert "[配额]" not in system


def test_gateway_still_injects_when_client_has_other_tools(tmp_path):
    """客户端带了别的工具但没带同名工具，网关照常注入。"""
    up = _UpstreamTool()
    app = create_app(_cfg(tmp_path), httpx.MockTransport(up.handler))
    _post(app, {
        "model": "x",
        "messages": [{"role": "user", "content": "你好"}],
        "tools": [{"type": "function", "function": {
            "name": "read_file", "description": "读文件",
            "parameters": {"type": "object", "properties": {}},
        }}],
    })
    body = up.local[0]
    names = [t["function"]["name"] for t in body.get("tools") or []]
    assert "consult_cloud" in names
    system = str(body["messages"][0].get("content") or "")
    assert "外部专家" in system and "[配额]" in system


# ==================================================================
# 裁剪
# ==================================================================


def _trim_cfg(**kw):
    base = dict(min_clear_tokens=0, keep_recent=2, spine=False)
    base.update(kw)
    return Ctx(**base)


def _history(pairs: int = 12) -> list[dict]:
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "目标：改配额"}]
    for i in range(pairs):
        msgs.append({"role": "assistant", "content": "说明" * 200})
        msgs.append({"role": "user", "content": f"继续 {i}"})
    msgs.append({"role": "user", "content": "最后"})
    return msgs


def test_trim_is_idempotent():
    """对已经裁过的结果再裁一次，不应该继续大幅缩水。"""
    tr = Trimmer(_trim_cfg(), Estimator())
    msgs = _history()
    once, _ = tr.fit(msgs, budget=3000, target=1200, keep_recent=2)
    twice, rep2 = tr.fit(once, budget=3000, target=1200, keep_recent=2)
    assert len(twice) == len(once), "第二遍不该再删消息"
    assert rep2.saved == 0


def test_trim_never_mutates_input():
    tr = Trimmer(_trim_cfg(), Estimator())
    msgs = _history()
    snapshot = json.dumps(msgs, ensure_ascii=False)
    tr.fit(msgs, budget=3000, target=1200, keep_recent=2)
    assert json.dumps(msgs, ensure_ascii=False) == snapshot


def test_trim_handles_degenerate_inputs():
    tr = Trimmer(_trim_cfg(), Estimator())
    for msgs in ([], [{"role": "system", "content": "s"}], [{"role": "user", "content": "只有一句"}]):
        out, rep = tr.fit(msgs, budget=1000, target=500, keep_recent=2)
        assert isinstance(out, list)
        assert rep.saved == 0


def test_trim_keeps_output_structurally_valid():
    """裁剪产物必须仍是合法对话：不出现孤儿 tool、不出现悬空 tool_calls。"""
    tr = Trimmer(_trim_cfg(), Estimator())
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "目标"}]
    for i in range(20):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "f", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "结果" * 300})
    msgs.append({"role": "user", "content": "收尾"})

    out, _ = tr.fit(msgs, budget=2000, target=900, keep_recent=2)
    live: set[str] = set()
    for i, m in enumerate(out):
        if m.get("role") == "assistant":
            live = {c.get("id") for c in (m.get("tool_calls") or [])}
            if live:
                assert i + 1 < len(out) and out[i + 1].get("role") == "tool", "悬空的 tool_calls"
        elif m.get("role") == "tool":
            assert m.get("tool_call_id") in live, "孤儿 tool 消息"
            live.discard(m.get("tool_call_id"))
        else:
            live = set()


def test_connection_reset_noise_is_suppressed():
    """浏览器断开连接不该在终端里打成 traceback。

    Windows 的 proactor 在 _call_connection_lost 里抛 ConnectionResetError，
    asyncio 默认会打一整段 Traceback，看起来像进程崩了。只允许吞这一类。
    """
    from steward.__main__ import _quiet_connection_reset

    seen: list[dict] = []

    class Loop:
        def default_exception_handler(self, context: dict) -> None:
            seen.append(context)

    loop = Loop()
    _quiet_connection_reset(loop, {"exception": ConnectionResetError(10054, "reset")})
    _quiet_connection_reset(loop, {"exception": ConnectionAbortedError(10053, "aborted")})
    assert seen == [], "连接重置被当成异常打印了"

    _quiet_connection_reset(loop, {"exception": RuntimeError("真的出错了")})
    assert len(seen) == 1, "真异常被一起吞掉了"

    _quiet_connection_reset(loop, {"message": "没有 exception 字段"})
    assert len(seen) == 2, "没有 exception 的上下文应交给默认处理器"
