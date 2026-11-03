"""审计测试：三项修复的对照实验。

**对照组不是"假设的旧行为"，而是把修复前的代码原样搬进来跑。**
`Legacy*` 类里的每一行都对应修复前的一行，谁看都能复核。这样结论才站得住：
差异是修复带来的，不是测试构造出来的。

三组对照，每组用多个不同形态的案例（不是同一个案例跑两遍）：

  一、委托时机  —— 旧：没有质量闸，任何 brief 直接进配额闸
  二、裁剪保真  —— 旧：只保护「最后一条 user」，且没有结构修复
  三、落盘与回收 —— 旧：路径含轮次、无抬头、逐条落盘、无回收

看对照表：`python -m pytest tests/test_audit.py -s -q`
只跑断言：`python -m pytest tests/test_audit.py -q`

审计的价值一半在"新版本变好了"，另一半在"没变的场景确实没变"——
所以每组都有一条 `*_no_regression` 用例，专门证明修复是精准的，不是到处改。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from steward.artifacts import Artifact, Store
from steward.config import (
    AppCfg,
    ArtifactsCfg,
    AuthCfg,
    ContextCfg,
    EndpointCfg,
    GuardCfg,
)
from steward.context import Estimator, Trimmer, count_tokens
from steward.prompts import (
    DEFAULT_SYSTEM_TEXT,
    precheck,
    render_quota_note,
)
from steward.server import create_app


def _table(headers: list[str], rows: list[list]) -> str:
    """等宽对齐的对照表。中文字符按两格算，否则列会歪。"""
    def width(text: str) -> int:
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(text))

    cells = [[str(c) for c in row] for row in rows]
    widths = [max([width(h)] + [width(r[i]) for r in cells]) for i, h in enumerate(headers)]
    line = "  ".join(h + " " * (widths[i] - width(h)) for i, h in enumerate(headers))
    out = [line, "  ".join("-" * w for w in widths)]
    for row in cells:
        out.append("  ".join(row[i] + " " * (widths[i] - width(row[i])) for i in range(len(headers))))
    return "\n".join(out)


# ==================================================================
# 一、委托时机
# ==================================================================
# 修复前：server.py 里 guard.reserve 之前没有任何检查，
# 只要 ask / brief 是非空字符串就直接进配额闸。
def legacy_precheck(kind: str, ask: str, brief: str, *_args, **_kwargs) -> str:
    return ""


GOOD_DIAG = (
    "goal: 定位 refresh 返回旧 token 的原因\n"
    "given: 只在并发下出现\n"
    "tried: 单测跑三遍，第三遍必现；日志在 /tmp/r.log，报错是 AssertionError"
)
GOOD_DESIGN = "goal: 选出缓存分层方案\ngiven: 单机 QPS 2k，读多写少\ntried: none"
PROSE_LONG = (
    "现在订单表 4 亿行要分片，读多写少，高峰期 3k QPS，不能停机迁移，"
    "也回不了滚，想听听有没有比先双写更稳的顺序。"
)

# (名称, kind, ask, brief, 是否应该被拦)
DELEGATION_CASES = [
    ("完整诊断单", "diagnosis", "为什么第三遍才复现", GOOD_DIAG, False),
    ("设计题无尝试", "design", "缓存怎么分层", GOOD_DESIGN, False),
    ("中文键写法", "diagnosis", "为什么偶发 502", "目标: 修 502\n已试: 重启后恢复，半小时又出现", False),
    ("自由文本够长", "design", "分片顺序怎么排", PROSE_LONG, False),
    ("诊断题没试过", "diagnosis", "为什么偶发 502", "goal: 修 502\ngiven: 线上偶发", True),
    ("评审题 tried=N/A", "review", "迁移脚本能跑吗", "goal: 迁数据\ntried: N/A", True),
    ("自由文本过短", "design", "缓存怎么分层", "brief 1", True),
    ("一次问多个", "design", "问" * 500, GOOD_DESIGN, True),
    ("五行缺 goal", "design", "缓存怎么分层", "given: QPS 2k\ntried: none", True),
]


def test_audit_delegation_gate_matches_expectation():
    """每个案例的判决必须与预期一致，且新旧确实不同。"""
    rows = []
    wrong: list[str] = []
    for name, kind, ask, brief, should_block in DELEGATION_CASES:
        old = legacy_precheck(kind, ask, brief)
        new = precheck(kind, ask, brief)
        old_blocked = bool(old)
        new_blocked = bool(new)
        rows.append([name, kind, "拦" if old_blocked else "放行", "拦" if new_blocked else "放行", "OK" if new_blocked == should_block else "不符"])
        if new_blocked != should_block:
            wrong.append(f"{name}: 期望{'拦' if should_block else '放行'}，实际{'拦' if new_blocked else '放行'}")
    report = _table(["案例", "类型", "旧行为", "新行为", "判决"], rows)
    print("\n[委托质量闸]\n" + report)
    assert not wrong, "判决与预期不符：\n" + "\n".join(wrong) + "\n" + report


def test_audit_delegation_gate_is_newly_added():
    """旧实现零拦截 —— 证明这道闸是新加的，不是把原有逻辑调严。"""
    old_blocks = [legacy_precheck(c[1], c[2], c[3]) for c in DELEGATION_CASES]
    assert all(v == "" for v in old_blocks), "旧实现不该有任何拦截"
    new_blocks = sum(1 for c in DELEGATION_CASES if precheck(c[1], c[2], c[3]))
    assert new_blocks == 5, f"新实现应拦下 5 个，实际 {new_blocks}"


def test_audit_delegation_gate_no_regression_on_good_briefs():
    """能干的活别挡在门外：4 个合格案例必须原样放行。"""
    for name, kind, ask, brief, should_block in DELEGATION_CASES:
        if not should_block:
            assert precheck(kind, ask, brief) == "", f"{name} 被误拦"


def test_audit_quota_is_invisible_in_legacy_prompt():
    """旧 system 文本对三种配额状态完全相同 —— 模型对剩余额度零感知。"""
    legacy = [DEFAULT_SYSTEM_TEXT for _ in (None, 0, 3, 50)]
    assert len(set(legacy)) == 1

    modern = [
        DEFAULT_SYSTEM_TEXT + "\n\n" + render_quota_note(remaining, 2)
        for remaining in (None, 0, 3, 50)
    ]
    assert len(set(modern)) == 4, "四种配额状态应给出四段不同提示"
    rows = [
        [state, "无配额信息", note.split("]")[-1].strip()[:34]]
        for state, note in zip(("不限量", "已用尽", "剩 3 次", "剩 50 次"), modern)
    ]
    print("\n[配额可见性]\n" + _table(["状态", "旧 system", "新 system 追加"], rows))


# ---------------------------------------------------------------- 端到端：退回限次


class _Upstream:
    """本地模型反复发同一个烂委托单，看网关拦几次。"""

    def __init__(self, script: list[str | None]):
        self.script = script
        self.local: list[dict] = []
        self.cloud_calls = 0
        self.turn = -1

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 32000}, "total_slots": 1})
        if request.url.path == "/tokenize":
            return httpx.Response(404, json={"error": "unsupported"})
        body = json.loads(request.content or b"{}")
        if request.url.host == "cloud.test":
            return self._cloud(body)
        return self._local(body)

    def _local(self, body: dict) -> httpx.Response:
        self.local.append(body)
        self.turn += 1
        nxt = self.script[self.turn] if self.turn < len(self.script) else None
        if nxt is not None:
            return httpx.Response(200, json={
                "id": "l", "object": "chat.completion", "created": 0, "model": "local",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "",
                    "tool_calls": [{"id": f"call_{self.turn}", "type": "function",
                                    "function": {"name": "consult_cloud",
                                                 "arguments": json.dumps(nxt, ensure_ascii=False)}}],
                }, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 20},
            })
        return httpx.Response(200, json={
            "id": "l", "object": "chat.completion", "created": 0, "model": "local",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "本地解决了。"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 420, "completion_tokens": 12},
        })

    def _cloud(self, body: dict) -> httpx.Response:
        self.cloud_calls += 1
        return httpx.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": "cloud",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "## 1. 结论\n用 A。"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 400},
        })


def _audit_cfg(tmp_path) -> AppCfg:
    return AppCfg(
        auth=AuthCfg(mode="off", keys_file=str(tmp_path / "keys.json")),
        base=EndpointCfg(base_url="http://local.test/v1", model="local", api_key="k", timeout_s=10),
        advanced=EndpointCfg(base_url="http://cloud.test/v1", model="cloud", api_key="k", timeout_s=10),
        guard=GuardCfg(
            state_file=str(tmp_path / "usage.json"),
            max_calls_per_turn=3,
            max_tool_rounds=5,
            min_interval_s=0,
            daily_limit=5,
            dedupe=False,
        ),
        context=ContextCfg(
            state_file=str(tmp_path / "ctx.json"),
            spine_state_file=str(tmp_path / "spine.json"),
            spine=False,
        ),
        artifacts=ArtifactsCfg(dir=str(tmp_path / "art"), enabled=True),
    )


def _post(app, messages: list[dict]) -> httpx.Response:
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            return await c.post("/v1/chat/completions", json={"model": "x", "messages": messages})

    return asyncio.run(go())


def test_audit_repeated_bad_brief_is_capped_at_two(tmp_path):
    """同一个烂委托单连发三次：前两次退回，第三次放行 —— 防死循环。"""
    bad = {"kind": "diagnosis", "ask": "为什么偶发 502", "brief": "goal: 修 502"}
    up = _Upstream([bad, bad, bad])
    app = create_app(_audit_cfg(tmp_path), httpx.MockTransport(up.handler))
    resp = _post(app, [{"role": "user", "content": "帮我看看这个 502"}])

    assert resp.status_code == 200
    rejects = [
        m for m in up.local[-1]["messages"]
        if m.get("role") == "tool" and "网关退回" in str(m.get("content"))
    ]
    assert len(rejects) == 2, f"应退回 2 次，实际 {len(rejects)}"
    assert up.cloud_calls == 1, f"第三次必须放行，云端调用数={up.cloud_calls}"
    print(f"\n[退回限次] 三次烂委托 → 退回 {len(rejects)} 次 / 云端真正调用 {up.cloud_calls} 次")


def test_audit_good_brief_goes_through_first_time(tmp_path):
    """对照组：合格委托单不该被退 —— 一次通过。"""
    good = {"kind": "diagnosis", "ask": "为什么第三遍才复现",
            "brief": "goal: 修并发刷新\ntried: 单测跑三遍，第三遍必现"}
    up = _Upstream([good, None])
    app = create_app(_audit_cfg(tmp_path), httpx.MockTransport(up.handler))
    _post(app, [{"role": "user", "content": "看下并发问题"}])
    rejects = [
        m for m in up.local[-1]["messages"]
        if m.get("role") == "tool" and "网关退回" in str(m.get("content"))
    ]
    assert rejects == []
    assert up.cloud_calls == 1


def test_audit_quota_reaches_the_model_in_request(tmp_path):
    """配额必须真的进到发出去的 payload 里，不只是函数能拼出来。"""
    up = _Upstream([None])
    app = create_app(_audit_cfg(tmp_path), httpx.MockTransport(up.handler))
    _post(app, [{"role": "user", "content": "随便问问"}])
    system = str(up.local[0]["messages"][0].get("content") or "")
    assert "[配额]" in system
    assert "还剩 5 次" in system


# ==================================================================
# 二、裁剪保真
# ==================================================================
# 修复前的 Trimmer：只保护「头 3 条 system + 末尾 keep_recent + 最后一条 user」，
# 且没有 _step_orphans 这一步。以下两段是修复前代码的原样搬运。
class LegacyTrimmer(Trimmer):
    def _protect(self, items, keep_recent):  # type: ignore[override]
        n = len(items)
        for i in range(min(3, n)):
            if items[i][0].get("role") == "system":
                items[i][2] = True
            else:
                break
        for i in range(max(0, n - max(0, keep_recent)), n):
            items[i][2] = True
        for i in range(n - 1, -1, -1):
            if items[i][0].get("role") == "user":
                items[i][2] = True
                break

    def _step_orphans(self, items, total, goal, rep):  # type: ignore[override]
        return total  # 修复前根本没有这一步


GOAL = "把这套服务的配额从按次改成按 token 计量，并补测试。"


def history_pairs(n: int = 24) -> list[dict]:
    """形态 A：工具调用密集，assistant(tool_calls) + tool 成对。"""
    msgs = [{"role": "system", "content": "你是编码助手。"}, {"role": "user", "content": GOAL}]
    for i in range(n):
        msgs.append({
            "role": "assistant", "content": f"第 {i} 步",
            "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "run", "arguments": "{}"}}],
        })
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "结果\n" * 200})
    msgs.append({"role": "user", "content": "继续。"})
    return msgs


def history_text(n: int = 26) -> list[dict]:
    """形态 B：纯文本往返，没有 tool_calls 可成对丢，只能硬截断。"""
    msgs = [{"role": "system", "content": "你是编码助手。"}, {"role": "user", "content": GOAL}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": f"第 {i} 步的说明。" * 40})
        msgs.append({"role": "user", "content": f"继续 {i}"})
    msgs.append({"role": "user", "content": "最后确认一下。"})
    return msgs


def history_tail_tool(n: int = 14) -> list[dict]:
    """形态 C：末尾 keep_recent 窗口正好切在 assistant(tool_calls) 与 tool 之间。"""
    msgs = [{"role": "system", "content": "你是编码助手。"}, {"role": "user", "content": GOAL}]
    for i in range(n):
        msgs.append({"role": "assistant", "content": f"第 {i} 步的说明。" * 40})
        msgs.append({"role": "user", "content": f"继续 {i}"})
    msgs.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "z1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
    })
    msgs.append({"role": "tool", "tool_call_id": "z1", "content": "大结果\n" * 300})
    msgs.append({"role": "assistant", "content": ""})
    msgs.append({"role": "tool", "tool_call_id": "z9", "content": "没有主人的结果\n" * 200})
    return msgs


def history_vision(n: int = 20) -> list[dict]:
    """形态 D：带思维链与图片 part，前两级裁剪会先动它们。"""
    msgs = [{"role": "system", "content": "你是编码助手。"}, {"role": "user", "content": GOAL}]
    for i in range(n):
        msgs.append({
            "role": "assistant", "content": [{"type": "text", "text": f"第 {i} 步说明。" * 30}],
            "reasoning_content": "思考" * 200,
        })
        msgs.append({"role": "user", "content": [
            {"type": "text", "text": f"继续 {i}"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]})
    msgs.append({"role": "user", "content": "最后确认。"})
    return msgs


TRIM_CASES = {
    "工具对密集": history_pairs,
    "纯文本往返": history_text,
    "尾部窗口切割": history_tail_tool,
    "多模态+思维链": history_vision,
}


def _goal_alive(msgs: list[dict]) -> bool:
    return any(m.get("content") == GOAL for m in msgs)


def _orphans(msgs: list[dict]) -> tuple[int, int]:
    """返回 (悬空 tool 消息数, 等不到结果的 tool_calls 数)。"""
    orphan_tool = 0
    dangling_calls = 0
    live: set[str] = set()
    for i, msg in enumerate(msgs):
        role = msg.get("role")
        if role == "assistant":
            calls = {c.get("id") for c in (msg.get("tool_calls") or [])}
            if calls and not (i + 1 < len(msgs) and msgs[i + 1].get("role") == "tool"):
                dangling_calls += 1
            live = calls
        elif role == "tool":
            if msg.get("tool_call_id") not in live:
                orphan_tool += 1
            live.discard(msg.get("tool_call_id"))
        else:
            live = set()
    return orphan_tool, dangling_calls


def _run(trimmer: Trimmer, msgs: list[dict], budget: int = 3200, target: int = 1400):
    return trimmer.fit(msgs, budget=budget, target=target, keep_recent=2)


def test_audit_trim_goal_survival():
    """旧版会丢掉任务目标；新版必须保住 —— 四种形态逐一对照。"""
    rows = []
    failures = []
    for name, build in TRIM_CASES.items():
        old_out, old_rep = _run(LegacyTrimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), build())
        new_out, new_rep = _run(Trimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), build())
        rows.append([
            name, old_rep.hard_cut, "是" if _goal_alive(old_out) else "否",
            new_rep.hard_cut, "是" if _goal_alive(new_out) else "否",
        ])
        if not _goal_alive(new_out):
            failures.append(name)
    print("\n[目标保护]\n" + _table(["案例", "旧硬截", "旧保目标", "新硬截", "新保目标"], rows))
    assert not failures, f"新版仍然丢目标：{failures}\n" + _table(["案例", "旧硬截", "旧保目标", "新硬截", "新保目标"], rows)


def test_audit_trim_legacy_actually_loses_goal():
    """对照前提：旧版确实会丢，否则这组对照没有意义。"""
    lost = []
    for name, build in TRIM_CASES.items():
        out, _ = _run(LegacyTrimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), build())
        if not _goal_alive(out):
            lost.append(name)
    assert lost, "旧版一个都没丢，说明测试案例没打到硬截断，对照失效"
    print(f"\n[对照前提] 旧版丢目标的形态：{', '.join(lost)}")


def test_audit_trim_no_orphan_tool():
    """旧版留下悬空 tool；新版必须零孤儿。”"""
    rows = []
    for name, build in TRIM_CASES.items():
        old_out, _ = _run(LegacyTrimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), build())
        new_out, _ = _run(Trimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), build())
        o1, o2 = _orphans(old_out)
        n1, n2 = _orphans(new_out)
        rows.append([name, f"{o1}/{o2}", f"{n1}/{n2}"])
        assert n1 == 0 and n2 == 0, f"{name} 新版仍有非法结构：孤儿={n1} 悬空调用={n2}"
    print("\n[结构合法性]\n" + _table(["案例", "旧 孤儿/悬空调用", "新 孤儿/悬空调用"], rows))


def test_audit_trim_legacy_produces_orphans_somewhere():
    """对照前提：旧版至少在一个形态上产生了非法结构。"""
    total = 0
    for build in TRIM_CASES.values():
        out, _ = _run(LegacyTrimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), build())
        o1, o2 = _orphans(out)
        total += o1 + o2
    assert total > 0, "旧版没产生非法结构，这组对照需换案例"


def test_audit_trim_no_regression_when_drop_is_paired():
    """工具对密集时不该走硬截断，新旧结果应完全一致。

    这条是审计的另一半：证明修复只在必要时生效，没有到处改。
    """
    old_out, old_rep = _run(LegacyTrimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), history_pairs())
    new_out, new_rep = _run(Trimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), history_pairs())
    assert old_rep.hard_cut == 0 and new_rep.hard_cut == 0
    assert [m.get("content") for m in old_out] == [m.get("content") for m in new_out]
    assert old_rep.dropped == new_rep.dropped


def test_audit_trim_under_budget_untouched():
    """没超预算时一个字节都不动 —— 两种实现都必须如此（保护 KV 前缀缓存）。"""
    msgs = history_pairs(4)
    for cls in (LegacyTrimmer, Trimmer):
        out, rep = cls(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()).fit(msgs, budget=10_000_000)
        assert out is msgs and rep.saved == 0


def test_audit_trim_dropped_messages_recorded():
    """新版要留下被丢弃的原文，否则骨架没料可写。"""
    _, rep = _run(Trimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), history_text())
    assert rep.dropped + rep.hard_cut > 0
    assert len(rep.dropped_messages) == rep.dropped + rep.hard_cut
    assert rep.dropped_tokens > 0


# ---------------------------------------------------------------- 骨架 A/B

DECISION = "结论：不用 Redis 做计数，运维不批，部署复杂度不可接受。"


def history_with_decision(n_text: int = 30, n_pairs: int = 16) -> list[dict]:
    """早期 assistant 消息里放一个「已否决的方案」，它会被硬截断吃掉。

    注意不能放在 user 里：user 消息（首条和末条）受保护，中间那些也不会被
    成对丢弃，所以放 user 反而丢不了。真正会消失的是早期的 assistant 文本
    和工具对 —— 决策写在 assistant 里才符合实际，也才测得出来。
    """
    msgs = [
        {"role": "system", "content": "你是编码助手。"},
        {"role": "user", "content": GOAL},
        {"role": "assistant", "content": DECISION},
    ]
    for i in range(n_text):
        msgs.append({"role": "assistant", "content": f"第 {i} 步的说明。" * 60})
        msgs.append({"role": "user", "content": f"继续 {i}"})
    for i in range(n_pairs):
        msgs.append({
            "role": "assistant", "content": f"跑第 {i} 批",
            "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "run", "arguments": "{}"}}],
        })
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "日志\n" * 60})
    msgs.append({"role": "user", "content": "继续。"})
    return msgs


class _SpineUpstream:
    """会读输入、按六行格式回骨架的假模型。它不做提炼判断，但数据流是真的。"""

    def __init__(self):
        self.local: list[dict] = []
        self.spine_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 6000}, "total_slots": 1})
        if request.url.path == "/tokenize":
            return httpx.Response(404, json={"error": "unsupported"})
        body = json.loads(request.content or b"{}")
        self.local.append(body)
        system = str((body.get("messages") or [{}])[0].get("content") or "")
        if "只输出六行" in system:
            self.spine_calls += 1
            user = str(body["messages"][1].get("content") or "")
            rejected = "Redis 做计数（运维不批，部署复杂度不可接受）" if "Redis" in user else "none"
            reply = (
                "goal: 把配额从按次改成按 token 计量\n"
                "constraints: none\n"
                "facts: 单槽 n_ctx=131072\n"
                f"rejected: {rejected}\n"
                "current: 改完 guard.py\n"
                "next: 补测试"
            )
            return self._text(reply, body)
        return self._text("已完成。", body)

    def _text(self, text: str, body: dict) -> httpx.Response:
        # prompt_tokens 必须跟着请求走：写死一个小值会让估算器每轮判成静默截断，
        # 预算一路缩、裁剪点漂移，骨架缓存就永远对不上。
        est = Estimator()
        msgs = body.get("messages") or []
        prompt = max(20, est.messages(msgs, est.tools_tokens(body.get("tools") or [])))
        return httpx.Response(200, json={
            "id": "l", "object": "chat.completion", "created": 0, "model": "local",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": 20},
        })


def _spine_cfg(tmp_path, spine: bool) -> AppCfg:
    return AppCfg(
        auth=AuthCfg(mode="off", keys_file=str(tmp_path / "keys.json")),
        base=EndpointCfg(base_url="http://local.test/v1", model="local", api_key="k", timeout_s=10, max_tokens=256),
        advanced=EndpointCfg(base_url="http://cloud.test/v1", model="cloud", api_key="k", timeout_s=10),
        guard=GuardCfg(
            state_file=str(tmp_path / "usage.json"),
            max_calls_per_turn=1, max_tool_rounds=2, min_interval_s=0, daily_limit=5, dedupe=False,
        ),
        context=ContextCfg(
            state_file=str(tmp_path / "ctx.json"),
            spine_state_file=str(tmp_path / "spine.json"),
            ctx_limit=6000, usable_ratio=0.85, reserve_output=False, safety_tokens=0,
            min_clear_tokens=0, keep_recent=2, spine=spine, spine_min_dropped=200,
        ),
        artifacts=ArtifactsCfg(dir=str(tmp_path / "art"), enabled=True),
    )


def _payload_text(up: _SpineUpstream) -> str:
    """整份 payload 的文本。只看 system[0] 会漏掉「决策还在，只是没被压掉」。"""
    return json.dumps(up.local[-1].get("messages") or [], ensure_ascii=False)


def _system_of(up: _SpineUpstream) -> str:
    return str((up.local[-1].get("messages") or [{}])[0].get("content") or "")


def test_audit_spine_keeps_rejected_decision(tmp_path):
    """开关对照：同一份历史，「已否决的方案」在裁剪后是否还到得了模型手里。"""
    rows = []
    for label, enabled in (("spine: false", False), ("spine: true", True)):
        sub = tmp_path / ("on" if enabled else "off")
        sub.mkdir()
        up = _SpineUpstream()
        app = create_app(_spine_cfg(sub, enabled), httpx.MockTransport(up.handler))
        resp = _post(app, history_with_decision())
        assert resp.status_code == 200
        payload = _payload_text(up)
        system = _system_of(up)
        rows.append([
            label,
            "有" if "Redis" in payload else "无",
            "有" if "<<<SPINE" in system else "无",
            up.spine_calls,
            len(system),
        ])
        if enabled:
            assert "Redis" in payload, "开了骨架仍然丢了已否决方案"
            assert "<<<SPINE" in system
            assert "rejected: Redis" in system, "决策不是经骨架回来的，而是碰巧没被删"
        else:
            assert "Redis" not in payload, "没开骨架却还留着决策，说明案例没压到它，对照失效"

    print("\n[骨架 A/B]\n" + _table(["配置", "决策是否到手", "骨架块", "提炼调用", "system 字数"], rows))


def test_audit_spine_costs_one_call_per_request(tmp_path):
    """开骨架的代价：一个请求一次本地调用，不是每轮一次。"""
    up = _SpineUpstream()
    app = create_app(_spine_cfg(tmp_path, True), httpx.MockTransport(up.handler))
    _post(app, history_with_decision())
    assert up.spine_calls == 1, f"应只提炼 1 次，实际 {up.spine_calls}"


def test_audit_spine_is_cached_across_identical_requests(tmp_path):
    up = _SpineUpstream()
    app = create_app(_spine_cfg(tmp_path, True), httpx.MockTransport(up.handler))
    _post(app, history_with_decision())
    _post(app, history_with_decision())
    assert up.spine_calls == 1, f"同样的历史第二次应命中缓存，实际调用 {up.spine_calls} 次"


# ==================================================================
# 三、落盘与回收
# ==================================================================
# 修复前：路径带轮次、无抬头、一次裁剪逐条 spill、完全没有回收。
class LegacyStore(Store):
    def save(self, text: str, kind: str = "consult", turn: str = "t0", ext: str = "md") -> Artifact:
        import hashlib
        import re

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        day = datetime.now().strftime("%Y%m%d")
        safe_turn = re.sub(r"[^A-Za-z0-9._-]", "_", turn or "t0")[:40]
        target = self.root / day / safe_turn / f"{kind}-{digest}.{ext}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(text, encoding="utf-8")  # 没有抬头
        return Artifact(
            id=f"art_{digest}", path=str(target), abs_path=str(target.resolve()),
            chars=len(text), tokens=count_tokens(text), kind=kind,
            created=datetime.now().isoformat(timespec="seconds"),
        )

    def spill_batch(self, texts: list[str], kind: str = "tool") -> list[str]:
        return [self.save(t, kind=kind, turn="trim", ext="log").path for t in texts]

    def maintain(self, force: bool = False, dry_run: bool = False) -> dict:
        return {"removed_days": [], "removed_files": 0, "freed_bytes": 0, "index_trimmed": 0, "dry_run": dry_run}


LONG = "## 1. 结论\n用两级缓存。\n" + "\n".join(f"{i}. 步骤，失败判据是错误率超过 5%" for i in range(80))


def _store(base: Path, cls=Store, **kw) -> Store:
    params = dict(dir=str(base), enabled=True, inline_chars=800, min_store_chars=1500)
    params.update(kw)
    return cls(ArtifactsCfg(**params))


def test_audit_archive_scenarios(tmp_path):
    """五个不同场景，逐项对照旧/新存档行为。"""
    texts = [f"输出 {i}\n" + "日志行\n" * 40 for i in range(60)]
    rows = []

    # 场景 1：一次裁剪 60 条工具输出
    old = _store(tmp_path / "s1o", LegacyStore)
    new = _store(tmp_path / "s1n", Store)
    old.spill_batch(texts, "tool")
    new.spill_batch(texts, "tool")
    o_files = list(Path(old.root).rglob("*.log"))
    n_files = list(Path(new.root).rglob("*.log"))
    o_bytes = sum(p.stat().st_size for p in o_files)
    n_bytes = sum(p.stat().st_size for p in n_files)
    o = len(o_files)
    n = len(n_files)
    rows.append(["一次裁剪 60 条工具输出", f"{o} 个 / {o_bytes // 1024} KB", f"{n} 个 / {n_bytes // 1024} KB"])
    assert (o, n) == (60, 1)
    # 合并不能带来体积膨胀：抬头是每文件几十字节，60 份抬头才 2 KB 量级
    assert abs(n_bytes - o_bytes) < o_bytes * 0.05, f"合并后体积异常：{o_bytes} -> {n_bytes}"

    # 场景 2：同一份内容跨三个轮次
    old = _store(tmp_path / "s2o", LegacyStore)
    new = _store(tmp_path / "s2n", Store)
    for i in range(3):
        old.save(LONG, kind="consult", turn=f"t{i}")
        new.save(LONG, kind="consult", turn=f"t{i}")
    o = len(list(Path(old.root).rglob("*.md")))
    n = len(list(Path(new.root).rglob("*.md")))
    rows.append(["同内容三个轮次", f"{o} 个文件", f"{n} 个文件"])
    assert (o, n) == (3, 1)

    # 场景 3：文件是否自描述
    old = _store(tmp_path / "s3o", LegacyStore)
    new = _store(tmp_path / "s3n", Store)
    old_text = Path(old.save(LONG, kind="consult", turn="t5").abs_path).read_text(encoding="utf-8")
    new_text = Path(new.save(LONG, kind="consult", turn="t5").abs_path).read_text(encoding="utf-8")
    rows.append(["正文首行", repr(old_text[:6]), repr(new_text[:6])])
    assert not old_text.startswith("---")
    assert new_text.startswith("---\n")
    meta = dict(line.split(": ", 1) for line in new_text.split("---\n", 2)[1].splitlines() if ": " in line)
    assert meta["kind"] == "consult" and meta["turn"] == "t5" and int(meta["chars"]) == len(LONG)

    # 场景 4：60 天前的目录 + 一个非日期目录
    old = _store(tmp_path / "s4o", LegacyStore)
    new = _store(tmp_path / "s4n", Store, max_age_days=30)
    old_day = Path(old.root) / "20200101" / "consult"
    new_day = Path(new.root) / "20200101" / "consult"
    keep = Path(new.root) / "manual" / "notes"
    for d in (old_day, new_day, keep):
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.md").write_text("x" * 100, encoding="utf-8")
    old.maintain(force=True)
    new.maintain(force=True)
    rows.append(["60 天前的日目录", "保留" if old_day.exists() else "删除", "保留" if new_day.exists() else "删除"])
    rows.append(["非日期目录 manual/", "保留", "保留" if keep.exists() else "误删"])
    assert old_day.exists() and not new_day.exists() and keep.exists()

    # 场景 5：旧日期但刚写过（mtime 很新）
    store = _store(tmp_path / "s5", Store, max_age_days=30)
    touched = Path(store.root) / "20200101" / "consult"
    touched.mkdir(parents=True)
    (touched / "x.md").write_text("x", encoding="utf-8")
    os.utime(touched, (time.time(), time.time()))
    store.maintain(force=True)
    rows.append(["旧日期 + 刚写过", "无回收", "删除" if not touched.exists() else "保留"])
    assert not touched.exists()

    print("\n[存档对照]\n" + _table(["场景", "旧行为", "新行为"], rows))


def test_audit_archive_no_regression_on_digest(tmp_path):
    """摘要逻辑没被改动：仍然给路径 + 目录 + 开头片段，且比原文短。"""
    store = _store(tmp_path, Store)
    art = store.save(LONG, kind="consult", turn="t1")
    digest = store.digest(art, LONG, inline_chars=600)
    assert "[交付物已存档]" in digest
    assert art.path in digest
    assert len(digest) < len(LONG)
    # 短文档不该走摘要（摘要可能比原文还长）
    short = "## 结论\n用 A。"
    art2 = store.save(short, kind="consult", turn="t2")
    assert store.digest(art2, short) == short


# ==================================================================
# 四、审计自身是否有效（元验证）
# ==================================================================


def test_audit_meta_detects_goal_regression():
    """把旧实现喂给审计判定，必须判它不合格。

    否则这套审计就是「写了但测不出」—— 那比没有更危险，
    因为它会给人已经验证过的错觉。
    """
    old_out, _ = _run(LegacyTrimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), history_text())
    new_out, _ = _run(Trimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), history_text())
    assert _goal_alive(old_out) is False, "审计判定对旧实现失效，无法抓回归"
    assert _goal_alive(new_out) is True


def test_audit_meta_detects_orphan_regression():
    old_out, _ = _run(LegacyTrimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), history_tail_tool())
    new_out, _ = _run(Trimmer(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()), history_tail_tool())
    assert _orphans(old_out)[0] > 0, "审计判定对旧实现失效，无法抓到孤儿"
    assert _orphans(new_out) == (0, 0)


def test_audit_meta_detects_gate_regression():
    """没有闸的旧实现必须被判为「全放行」。"""
    blocked = [c for c in DELEGATION_CASES if legacy_precheck(c[1], c[2], c[3])]
    assert blocked == []


# ==================================================================
# 五、真实历史样本（有就跑，没有跳过）
# ==================================================================

REAL_HISTORY = Path(__file__).resolve().parent / "fixtures" / "big_history.json"


@pytest.mark.skipif(not REAL_HISTORY.exists(), reason="没有真实历史样本 tests/fixtures/big_history.json")
def test_audit_real_history_goal_survival():
    """363 条真实消息：目标在旧实现下确实被删了，新实现保住。"""
    msgs = json.loads(REAL_HISTORY.read_text(encoding="utf-8"))
    goal = next((m.get("content") for m in msgs if m.get("role") == "user"), "")
    assert goal, "样本里没有 user 消息"

    rows = []
    for label, cls in (("旧", LegacyTrimmer), ("新", Trimmer)):
        out, rep = cls(ContextCfg(min_clear_tokens=0, keep_recent=2), Estimator()).fit(
            msgs, budget=30000, target=18000
        )
        alive = any(m.get("content") == goal for m in out)
        rows.append([label, rep.before, rep.after, rep.hard_cut, "是" if alive else "否", len(out)])

    print("\n[真实历史 363 条]\n" + _table(["实现", "裁剪前", "裁剪后", "硬截", "目标在", "消息数"], rows))
    assert rows[0][4] == "否", "旧实现没丢目标，样本或参数需调整"
    assert rows[1][4] == "是", "新实现仍然丢目标"
