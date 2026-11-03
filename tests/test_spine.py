"""任务骨架：解析、合并、注入、缓存、兜底。

这一层的失败模式很特别：它不报错，只是悄悄变成一句「压缩了 N 条」，
然后模型开始重复已经试过的方案。所以测试重点全在「退化了没有」。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from steward.config import ContextCfg
from steward.spine import (
    Spine,
    SpineBuilder,
    inject_spine,
    parse_spine,
    render_region,
)

MODEL_OUT = """goal: 把配额从按次改成按 token 计量
constraints: 不能改现有 key 格式；测试必须保留
facts: 当前 prefill 1305 tok/s；单槽 n_ctx=131072
rejected: 用 Redis 做计数（部署复杂度不可接受）
current: 已改完 guard.py，测试未跑
next: 补 test_guard.py 的计量用例
"""


# ---------------------------------------------------------------- 解析


def test_parse_reads_all_six_fields():
    s = parse_spine(MODEL_OUT)
    assert s.goal.startswith("把配额")
    assert "prefill" in s.facts
    assert "Redis" in s.rejected
    assert s.next.startswith("补 test_guard")


def test_parse_ignores_chatter():
    text = "好的，我来整理一下：\n\n" + MODEL_OUT + "\n希望有帮助。"
    s = parse_spine(text)
    assert s.goal.startswith("把配额")
    assert "希望有帮助" not in s.as_text()


def test_parse_keeps_first_occurrence_only():
    """模型有时会先给一行然后自我修正，只认第一行，别把两段拼起来。"""
    s = parse_spine("goal: 第一版\n说明一下……\ngoal: 第二版")
    assert s.goal == "第一版"


def test_parse_tolerates_fullwidth_colon_and_bullets():
    s = parse_spine("- goal：修 bug\n* facts：日志里有 timeout")
    assert s.goal == "修 bug"
    assert "timeout" in s.facts


def test_parse_empty_returns_empty_spine():
    assert parse_spine("随便说点什么").empty()
    assert parse_spine("").empty()


def test_long_field_is_capped():
    s = parse_spine("facts: " + "很长的内容" * 500)
    assert len(s.facts) <= 400


# ---------------------------------------------------------------- 合并


def test_merge_keeps_old_facts_when_new_is_none():
    """分次裁剪时，上一轮的事实不该因为这一轮没提就被抹掉。"""
    old = Spine(goal="目标A", facts="事实1", rejected="否决1")
    new = Spine(goal="目标A", facts="none", rejected="否决2", current="做到B")
    merged = old.merge(new)
    assert merged.facts == "事实1"
    assert merged.rejected == "否决2"
    assert merged.current == "做到B"


def test_merge_accumulates_dropped_count():
    old = Spine(goal="A", dropped=10)
    new = Spine(goal="B", dropped=5)
    assert old.merge(new).dropped == 15


# ---------------------------------------------------------------- 渲染


def test_render_region_keeps_decisions_over_tool_output():
    """工具输出多半已落盘，判断句丢了就真没了——所以优先保留后者。"""
    dropped = [
        {"role": "tool", "content": "x" * 4000},
        {"role": "user", "content": "不要用 Redis，运维不批"},
        {"role": "assistant", "content": "确认走本地文件计数"},
    ]
    text = render_region(dropped, max_chars=900)
    assert "不要用 Redis" in text
    assert "本地文件计数" in text


def test_render_region_labels_roles():
    text = render_region([{"role": "user", "content": "目标是什么"}], 500)
    assert text.startswith("<user>")


def test_render_region_reports_what_it_skipped():
    dropped = [{"role": "assistant", "content": "内容" * 300} for _ in range(10)]
    text = render_region(dropped, max_chars=500)
    assert "网关省略了" in text


def test_render_region_keeps_both_ends():
    """头里有来龙去脉，尾里有「做到哪一步」，只取一头都会写歪。"""
    dropped = [{"role": "user", "content": "最初的要求：不许改 schema"}]
    dropped += [{"role": "assistant", "content": f"第 {i} 步" + "细节" * 100} for i in range(30)]
    dropped.append({"role": "assistant", "content": "刚刚跑完第 30 步，准备收尾"})

    text = render_region(dropped, max_chars=3000)
    assert "不许改 schema" in text
    assert "刚刚跑完第 30 步" in text
    assert "网关省略了" in text


def test_render_region_mentions_tool_names():
    dropped = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "run_shell"}}],
        }
    ]
    assert "run_shell" in render_region(dropped, 500)


# ---------------------------------------------------------------- 注入


def test_inject_appends_to_first_system():
    msgs = [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "hi"}]
    out = inject_spine(msgs, Spine(goal="修 bug"))
    assert "你是助手" in out[0]["content"]
    assert "goal: 修 bug" in out[0]["content"]
    assert len(out) == len(msgs)


def test_inject_replaces_previous_block_not_grows():
    """每轮裁剪都追加一块会无界膨胀，必须原地替换。"""
    s1 = Spine(goal="第一版")
    s2 = Spine(goal="第二版")
    msgs = [{"role": "system", "content": "你是助手"}]
    once = inject_spine(msgs, s1)
    twice = inject_spine(once, s2)
    assert twice[0]["content"].count("<<<SPINE") == 1
    assert "第二版" in twice[0]["content"]
    assert "第一版" not in twice[0]["content"]
    assert len(twice[0]["content"]) < len(once[0]["content"]) + 40


def test_inject_preserves_text_after_block():
    """_mark 的压缩提示写在骨架后面，替换骨架时不能把它吃掉。"""
    msgs = [{"role": "system", "content": "你是助手"}]
    msgs = inject_spine(msgs, Spine(goal="A"))
    msgs[0]["content"] += "\n\n[网关] 压缩了 12 条"
    out = inject_spine(msgs, Spine(goal="B"))
    assert "压缩了 12 条" in out[0]["content"]
    assert "goal: B" in out[0]["content"]


def test_inject_does_not_mutate_input_list():
    msgs = [{"role": "system", "content": "原始"}, {"role": "user", "content": "hi"}]
    inject_spine(msgs, Spine(goal="X"))
    assert msgs[0]["content"] == "原始"


def test_inject_none_is_noop():
    msgs = [{"role": "system", "content": "原始"}]
    assert inject_spine(msgs, None) is msgs
    assert inject_spine(msgs, Spine()) is msgs


def test_inject_creates_system_when_absent():
    out = inject_spine([{"role": "user", "content": "hi"}], Spine(goal="X"))
    assert out[0]["role"] == "system"
    assert out[1]["role"] == "user"


# ---------------------------------------------------------------- 生成与缓存


class FakeClient:
    def __init__(self, reply: str = MODEL_OUT, fail: bool = False):
        self.cfg = type("Cfg", (), {"model": "local-m"})()
        self.calls = 0
        self.reply = reply
        self.fail = fail

    async def chat(self, payload: dict) -> dict:
        self.calls += 1
        assert payload["chat_template_kwargs"]["enable_thinking"] is False
        if self.fail:
            raise httpx.ConnectError("boom")
        return {"choices": [{"message": {"content": self.reply}}]}


DROPPED = [
    {"role": "user", "content": "把配额改成按 token 计量"},
    {"role": "assistant", "content": "先看 guard.py"},
    {"role": "tool", "content": "读到了 120 行"},
]


def build(cfg=None, client=None, tmp_path=None):
    cfg = cfg or ContextCfg()
    client = client or FakeClient()
    path = (tmp_path / "spine.json") if tmp_path else None
    return SpineBuilder(cfg, lambda: client, state_path=path), client


def test_build_returns_model_spine():
    b, c = build()
    s = asyncio.run(b.build(DROPPED))
    assert s is not None
    assert s.source == "model"
    assert "Redis" in s.rejected
    assert s.dropped == len(DROPPED)
    assert c.calls == 1


def test_build_hits_cache_on_same_region():
    """每轮请求都会重发同样的历史，缓存命中才不会每次都多花 10 秒。"""
    b, c = build()
    asyncio.run(b.build(DROPPED))
    s2 = asyncio.run(b.build(DROPPED))
    assert s2.source == "cache"
    assert c.calls == 1


def test_cache_survives_restart(tmp_path):
    b1, c1 = build(tmp_path=tmp_path)
    asyncio.run(b1.build(DROPPED))
    b2, c2 = build(tmp_path=tmp_path)
    s = asyncio.run(b2.build(DROPPED))
    assert s.source == "cache"
    assert c2.calls == 0
    assert json.loads((tmp_path / "spine.json").read_text(encoding="utf-8"))


def test_build_falls_back_when_model_fails():
    b, _ = build(client=FakeClient(fail=True))
    s = asyncio.run(b.build(DROPPED))
    assert s is not None
    assert s.source == "mechanical"
    # 机械兜底至少保住目标
    assert "token" in s.goal


def test_mechanical_fallback_keeps_previous_facts():
    b, _ = build(client=FakeClient(fail=True))
    prev = Spine(goal="旧目标", facts="旧事实", rejected="旧否决")
    s = asyncio.run(b.build(DROPPED, prev))
    assert s.facts == "旧事实"
    assert s.rejected == "旧否决"


def test_disabled_returns_none():
    b, c = build(cfg=ContextCfg(spine=False))
    assert asyncio.run(b.build(DROPPED)) is None
    assert c.calls == 0


def test_single_message_not_worth_a_call():
    b, c = build()
    assert asyncio.run(b.build([{"role": "user", "content": "x"}])) is None
    assert c.calls == 0


def test_garbage_reply_falls_back_to_mechanical():
    b, _ = build(client=FakeClient(reply="我不知道你在说什么"))
    s = asyncio.run(b.build(DROPPED))
    assert s.source == "mechanical"


def test_build_passes_previous_spine_into_prompt():
    seen: list[str] = []

    class Recorder(FakeClient):
        async def chat(self, payload):
            seen.append(payload["messages"][1]["content"])
            return await super().chat(payload)

    b, _ = build(client=Recorder())
    asyncio.run(b.build(DROPPED, Spine(goal="上一轮的目标", facts="上一轮的事实")))
    assert "上一轮的目标" in seen[0]
    assert "上一轮的事实" in seen[0]


def test_cache_size_is_bounded():
    b, _ = build(cfg=ContextCfg(spine_cache_size=2))
    for i in range(5):
        asyncio.run(b.build([{"role": "user", "content": f"第{i}个任务"}, {"role": "assistant", "content": "ok"}]))
    assert len(b._cache) <= 2


def test_timeout_falls_back_not_hangs():
    class Slow(FakeClient):
        async def chat(self, payload):
            await asyncio.sleep(5)
            return {"choices": [{"message": {"content": MODEL_OUT}}]}

    b, _ = build(cfg=ContextCfg(spine_timeout_s=0.2), client=Slow())
    s = asyncio.run(b.build(DROPPED))
    assert s is not None
    assert s.source == "mechanical"


def test_allow_model_false_skips_generation():
    """一个请求里裁剪多轮时，第二、三轮不该再调模型。"""
    b, c = build()
    assert asyncio.run(b.build(DROPPED, allow_model=False)) is None
    assert c.calls == 0


def test_allow_model_false_still_serves_cache():
    b, c = build()
    asyncio.run(b.build(DROPPED))
    s = asyncio.run(b.build(DROPPED, allow_model=False))
    assert s is not None and s.source == "cache"
    assert c.calls == 1


def test_allow_model_false_skips_generation():
    """一个请求里裁剪多轮时，第二、三轮不该再调模型。"""
    b, c = build()
    assert asyncio.run(b.build(DROPPED, allow_model=False)) is None
    assert c.calls == 0


def test_allow_model_false_still_serves_cache():
    b, c = build()
    asyncio.run(b.build(DROPPED))
    s = asyncio.run(b.build(DROPPED, allow_model=False))
    assert s is not None and s.source == "cache"
    assert c.calls == 1
