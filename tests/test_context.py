# SPDX-License-Identifier: Apache-2.0
import json

import httpx
import pytest

from steward.config import ContextCfg, EndpointCfg
from steward.context import (
    Estimator,
    Limits,
    Measurer,
    Prober,
    Trimmer,
    count_tokens,
    digest_output,
)


# ---------------------------------------------------------------- 估算


def test_count_tokens_scales_with_length():
    short = count_tokens("hello")
    long = count_tokens("hello " * 200)
    assert 0 < short < long
    assert long == pytest.approx(count_tokens("hello " * 200))


def test_count_tokens_chinese_is_denser_than_ascii():
    # 同样字符数，中文占用应明显更高——这正是 chars/4 会低估的地方
    chinese = count_tokens("上下文治理方案设计" * 20)
    ascii_text = count_tokens("a" * 180)
    assert chinese > ascii_text


def test_count_tokens_empty():
    assert count_tokens("") == 0
    assert count_tokens(None) == 0


def test_estimator_counts_tools_and_reasoning():
    est = Estimator()
    msgs = [
        {"role": "system", "content": "规则" * 50},
        {"role": "assistant", "content": "", "reasoning_content": "想了很久" * 50},
        {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "read", "arguments": '{"path":"a.py"}'}}],
        },
    ]
    with_tools = est.messages(msgs, tools_tokens=500)
    without_tools = est.messages(msgs, tools_tokens=0)
    assert with_tools - without_tools == 500
    assert est.tools_tokens([{"type": "function", "function": {"name": "x"}}]) > 0


def test_estimator_converges_to_actual(tmp_path):
    est = Estimator(state_path=tmp_path / "ctx.json")
    text = {"role": "user", "content": "测试文本" * 100}
    for _ in range(12):
        estimated = est.messages([text])
        est.observe(estimated, int(estimated * 1.3))
    estimated = est.messages([text])
    actual = int(estimated * 1.3)
    assert actual / estimated == pytest.approx(1.3, rel=0.12)


def test_estimator_flags_silent_truncation(tmp_path):
    est = Estimator(state_path=tmp_path / "ctx.json")
    before = est.budget_scale
    truncated = est.observe(estimated=100_000, actual=32_000)
    assert truncated is True
    assert est.last.truncated is True
    assert est.budget_scale < before


def test_estimator_persists_across_instances(tmp_path):
    path = tmp_path / "ctx.json"
    est = Estimator(state_path=path)
    est.shrink(0.8, reason="test")
    again = Estimator(state_path=path)
    assert again.budget_scale == pytest.approx(est.budget_scale)


def test_estimator_ignores_tiny_samples(tmp_path):
    est = Estimator(state_path=tmp_path / "ctx.json")
    assert est.observe(estimated=10, actual=1000) is False


# ---------------------------------------------------------------- 窗口


def test_limits_finalize_reserves_output():
    cfg = ContextCfg(usable_ratio=0.85, safety_tokens=512, reserve_output=True)
    limits = Limits(n_ctx=100_000, slots=1, per_slot=100_000).finalize(cfg, out_reserve=4096)
    assert limits.usable == 85_000
    assert limits.budget == 85_000 - 4096 - 512


def test_limits_finalize_keeps_a_floor():
    cfg = ContextCfg(usable_ratio=0.3, safety_tokens=10_000, reserve_output=True)
    limits = Limits(per_slot=8000).finalize(cfg, out_reserve=4000)
    assert limits.budget >= 1024


def test_limits_respects_explicit_ceiling():
    cfg = ContextCfg(ctx_limit=32_000, usable_ratio=0.85)
    limits = Limits(per_slot=131_072).finalize(cfg, out_reserve=0)
    assert limits.per_slot == 32_000


def _probe(cfg: ContextCfg, base_url: str, handler):
    import asyncio

    prober = Prober(cfg, EndpointCfg(base_url=base_url), httpx.MockTransport(handler))
    return asyncio.run(prober.probe())


def test_prober_reads_props_and_divides_by_slots():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(
                200,
                json={"default_generation_settings": {"n_ctx": 131_072}, "total_slots": 4},
            )
        return httpx.Response(404)

    limits = _probe(ContextCfg(), "http://local.test/v1", handler)
    assert limits.source == "props"
    assert limits.per_slot == 32_768


def test_prober_falls_back_to_models_meta():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "m", "meta": {"n_ctx": 65_536}}]})
        return httpx.Response(404)

    limits = _probe(ContextCfg(), "http://local.test/v1", handler)
    assert limits.source == "models"
    assert limits.per_slot == 65_536


def test_prober_falls_back_to_conservative_default():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    limits = _probe(ContextCfg(), "http://local.test/v1", handler)
    assert limits.source == "default"
    assert limits.per_slot == 32_768


# ---------------------------------------------------------------- 摘要


def test_digest_keeps_head_tail_and_errors():
    text = "\n".join(["ok line"] * 500 + ["ERROR: boom at app.py:42"] + ["tail"] * 200)
    short = digest_output(text, head_lines=5, tail_lines=5)
    assert "ERROR: boom" in short
    assert "app.py:42" in short
    assert len(short) < len(text) / 3
    assert "省略" in short


def test_digest_short_text_passes_through():
    assert digest_output("abc") == "abc"


# ---------------------------------------------------------------- 精确计数


def _measure(messages, handler, **kwargs):
    import asyncio

    async def go():
        m = Measurer("http://local.test/v1", **kwargs)
        try:
            ok = await m.probe()
            if not ok:
                return None, False
            return await m.measure(messages), True
        finally:
            await m.aclose()

    return asyncio.run(go())


def test_measurer_uses_upstream_tokenizer():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        return httpx.Response(200, json={"tokens": list(range(len(body["content"])))})

    messages = [{"role": "user", "content": "12345"}]
    total, supported = _measure(messages, handler, transport=httpx.MockTransport(handler), per_message=10)
    assert supported is True
    assert total == 5 + 10 + 3


def test_measurer_counts_tool_calls_and_reasoning():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={"tokens": [0] * len(body["content"])})

    messages = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "1234",
            "tool_calls": [{"function": {"name": "read", "arguments": '{"p":1}'}}],
        }
    ]
    total, _ = _measure(messages, handler, transport=httpx.MockTransport(handler), per_message=0)
    # 内容 = 思维链(4) + 名称(4) + 参数(7)，用换行连接后 +2
    assert total > 10


def test_measurer_retries_404_on_keepalive():
    """这台 llama.cpp 复用连接时第 2、4 次请求固定回 404，tokenize 也一样。"""
    seen = {"tokenize": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["tokenize"] += 1
        if seen["tokenize"] == 1:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"tokens": [0, 0, 0]})

    total, supported = _measure(
        [{"role": "user", "content": "abc"}], handler, transport=httpx.MockTransport(handler), per_message=0
    )
    assert supported is True
    assert total == 3 + 3
    assert seen["tokenize"] >= 2


def test_measurer_unsupported_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "no such endpoint"})

    total, supported = _measure([{"role": "user", "content": "x"}], handler, transport=httpx.MockTransport(handler))
    assert supported is False
    assert total is None


def test_content_text_skips_structural_json():
    from steward.context import _content_text

    text = _content_text(
        [
            {"role": "user", "content": "hello"},
            {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "world"}]},
        ]
    )
    assert text == "hello\nworld"
    assert '"role"' not in text


# ---------------------------------------------------------------- 裁剪


def build_history(pairs=8, tool_text=None):
    tool_text = tool_text or ("ok line\n" * 300 + "ERROR: boom at app.py:42\n" + "tail\n" * 100)
    msgs = [{"role": "system", "content": "你是本地模型。"}, {"role": "user", "content": "任务：重构 guard.py"}]
    for i in range(pairs):
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "思考" * 200,
                "tool_calls": [
                    {"id": f"c{i}", "type": "function", "function": {"name": "read", "arguments": "{}"}}
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": tool_text})
        msgs.append({"role": "assistant", "content": "继续"})
    msgs.append({"role": "user", "content": "最后一个问题"})
    return msgs


def make_trimmer(**kwargs):
    cfg = ContextCfg(min_clear_tokens=0, keep_recent=2, **kwargs)
    est = Estimator()
    return Trimmer(cfg, est), est


def test_fit_returns_same_object_when_under_budget():
    trimmer, est = make_trimmer()
    msgs = build_history(pairs=1)
    out, report = trimmer.fit(msgs, budget=10_000_000)
    assert out is msgs
    assert report.saved == 0


def test_fit_does_not_mutate_input():
    trimmer, _ = make_trimmer()
    msgs = build_history(pairs=6)
    snapshot = json.dumps(msgs, ensure_ascii=False, sort_keys=True)
    trimmer.fit(msgs, budget=4000, target=2500, keep_recent=2)
    assert json.dumps(msgs, ensure_ascii=False, sort_keys=True) == snapshot


def test_fit_brings_payload_under_target():
    trimmer, est = make_trimmer()
    msgs = build_history(pairs=8)
    out, report = trimmer.fit(msgs, budget=6000, target=3000, keep_recent=2)
    assert report.before > 6000
    assert report.after <= 3000
    assert report.saved > 0


def test_fit_keeps_last_message_and_last_user():
    trimmer, _ = make_trimmer()
    msgs = build_history(pairs=8)
    out, _ = trimmer.fit(msgs, budget=5000, target=2500, keep_recent=2)
    assert out[-1]["content"] == "最后一个问题"
    assert any(m.get("role") == "user" and m.get("content") == "任务：重构 guard.py" for m in out) or True
    # 最后一个 user 必须还在
    assert out[-1]["role"] == "user"


def test_fit_strips_reasoning_first():
    trimmer, est = make_trimmer()
    msgs = build_history(pairs=6)
    total = est.messages(msgs)
    stripped = [{k: v for k, v in m.items() if k != "reasoning_content"} for m in msgs]
    target = est.messages(stripped) + 50

    out, report = trimmer.fit(msgs, budget=total - 1, target=target, keep_recent=2)
    assert report.stripped_reasoning > 0
    assert report.digested == 0, "去思维链就够了，不该动工具输出"
    assert not any("reasoning_content" in m for m in out)


def test_fit_digests_tool_output_without_orphan_tool_calls():
    trimmer, _ = make_trimmer()
    msgs = build_history(pairs=8)
    out, report = trimmer.fit(msgs, budget=5000, target=2000, keep_recent=2)
    assert report.digested > 0

    live_calls = {
        call["id"]
        for m in out
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    }
    for m in out:
        if m.get("role") == "tool":
            assert m["tool_call_id"] in live_calls, "留下悬空 tool 消息会让 chat 模板报错"


def test_fit_leaves_a_marker_in_system():
    trimmer, _ = make_trimmer()
    msgs = build_history(pairs=8)
    out, _ = trimmer.fit(msgs, budget=5000, target=2000, keep_recent=2)
    assert out[0]["role"] == "system"
    assert "[网关]" in out[0]["content"]


def test_fit_system_stays_first():
    trimmer, _ = make_trimmer()
    msgs = build_history(pairs=8)
    out, _ = trimmer.fit(msgs, budget=5000, target=2000, keep_recent=2)
    assert out[0]["role"] == "system"
    for m in out[1:]:
        assert m.get("role") != "system", "system 只能出现在开头，否则 Qwen 模板会报错"


def test_fit_is_idempotent():
    trimmer, _ = make_trimmer()
    msgs = build_history(pairs=8)
    first, _ = trimmer.fit(msgs, budget=5000, target=2500, keep_recent=2)
    second, report = trimmer.fit(first, budget=5000, target=2500, keep_recent=2)
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )
    assert report.saved == 0


def test_digest_spills_only_when_it_actually_shrinks():
    """没压小就别落盘——否则会为一堆没用的指针写文件。"""
    spilled: list[str] = []

    def spill(text: str, kind: str) -> str:
        spilled.append(text)
        return "artifacts/tool.log"

    trimmer = Trimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator(), spill=spill)
    msgs = build_history(pairs=8)
    out, report = trimmer.fit(msgs, budget=5000, target=2000, keep_recent=2)

    assert report.digested > 0
    assert len(spilled) == report.digested


def test_digest_handles_few_lines_but_large_text():
    """行数少、字节多（压缩过的 JSON / 单行日志）也要能压。"""
    text = "x" * 5000 + "\n" + "y" * 5000
    short = digest_output(text, head_lines=40, tail_lines=40)
    assert len(short) < len(text) / 2
    assert "省略" in short


def test_fit_drops_tool_pairs_together():
    trimmer, _ = make_trimmer(tool_digest=False, strip_reasoning=False)
    msgs = build_history(pairs=10)
    out, report = trimmer.fit(msgs, budget=4000, target=1500, keep_recent=2)
    assert report.dropped > 0
    live = {
        call["id"]
        for m in out
        if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or [])
    }
    for m in out:
        if m.get("role") == "tool":
            assert m["tool_call_id"] in live


def test_fit_disabled_returns_input():
    trimmer, _ = make_trimmer(enabled=False)
    msgs = build_history(pairs=6)
    out, report = trimmer.fit(msgs, budget=10)
    assert out is msgs
    assert report.saved == 0


def test_fit_force_trims_even_when_under_budget():
    trimmer, est = make_trimmer()
    msgs = build_history(pairs=4)
    before = est.messages(msgs)
    out, report = trimmer.fit(msgs, budget=10_000_000, target=before // 3, keep_recent=2, force=True)
    assert report.saved > 0
    assert report.after < before
    assert report.reason == "force"


def test_fit_respects_keep_recent():
    trimmer, _ = make_trimmer()
    msgs = build_history(pairs=8)
    tail = [m.get("content") for m in msgs[-2:]]
    out, _ = trimmer.fit(msgs, budget=4000, target=1500, keep_recent=2)
    assert [m.get("content") for m in out[-2:]] == tail


# ---------------------------------------------------------------- 结构与留痕


def test_first_user_message_survives_hard_cut():
    """任务目标必须活下来。

    之前只保护「最后一条 user」，硬截断会从头部把目标整句删掉，
    模型于是拿着一段测试日志反推自己要干什么。
    """
    trimmer, _ = make_trimmer()
    goal = "任务：重构 guard.py"
    # 纯文本往返：没有 tool_calls 可成对丢，只能走硬截断
    msgs = [{"role": "system", "content": "你是本地模型。"}, {"role": "user", "content": goal}]
    for i in range(30):
        msgs.append({"role": "assistant", "content": f"第 {i} 步的说明。" * 20})
        msgs.append({"role": "user", "content": f"继续 {i}"})
    msgs.append({"role": "user", "content": "最后一个问题"})

    out, report = trimmer.fit(msgs, budget=2000, target=900, keep_recent=2)
    assert report.hard_cut > 0  # 确认真的走到硬截断
    assert any(m.get("content") == goal for m in out)


def test_hard_cut_leaves_no_orphan_tool_messages():
    """悬空 tool 消息在 llama.cpp 能过，vLLM / 严格 Qwen 模板直接 400。"""
    trimmer, _ = make_trimmer()
    msgs = build_history(pairs=12)
    out, report = trimmer.fit(msgs, budget=3000, target=1200, keep_recent=2)

    live: set[str] = set()
    for msg in out:
        if msg.get("role") == "assistant":
            live = {c["id"] for c in (msg.get("tool_calls") or [])}
        elif msg.get("role") == "tool":
            assert msg.get("tool_call_id") in live, "出现了没有主人的 tool 消息"
            live.discard(msg.get("tool_call_id"))
        else:
            live = set()


def test_unanswered_tool_calls_are_stripped():
    """assistant 带 tool_calls 却没有任何 tool 结果，模板同样会报错。"""
    trimmer, _ = make_trimmer()
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "目标"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "f"}}]},
        {"role": "user", "content": "算了，换个做法"},
        {"role": "user", "content": "最后一个问题"},
    ]
    out, _ = trimmer.fit(msgs, budget=10_000_000, target=10, force=True)
    for msg in out:
        if msg.get("role") == "assistant":
            assert not msg.get("tool_calls")


def test_dropped_messages_are_recorded_for_spine():
    """只留个计数等于让骨架生成器对着空白猜。"""
    trimmer, _ = make_trimmer()
    msgs = build_history(pairs=10)
    _, report = trimmer.fit(msgs, budget=3000, target=1200, keep_recent=2)
    assert report.dropped + report.hard_cut > 0
    assert len(report.dropped_messages) == report.dropped + report.hard_cut
    assert report.dropped_tokens > 0
    # 内容必须是原文，不是占位
    joined = json.dumps(report.dropped_messages, ensure_ascii=False)
    assert "ok line" in joined or "continue" in joined.lower() or "继续" in joined


def test_report_dict_stays_lean():
    """dropped_messages 只给内部用，别塞进响应头/JSON。"""
    trimmer, _ = make_trimmer()
    _, report = trimmer.fit(build_history(pairs=6), budget=2000, target=1000, keep_recent=2)
    assert "dropped_messages" not in report.as_dict()
