"""骨架的端到端验证：裁剪发生后，模型收到的 system 里到底有没有那六行。

单元测试只能证明 parse/inject 是对的，证明不了它真的被接进请求。
这里用一个会说话的上游：看到提炼提示就回一份骨架，看到正常对话就回文本。
"""

from __future__ import annotations

import asyncio
import json

import httpx

from steward.config import AppCfg, ArtifactsCfg, AuthCfg, ContextCfg, EndpointCfg, GuardCfg
from steward.context import Estimator
from steward.server import create_app

SPINE_REPLY = """goal: 把配额从按次改成按 token 计量
constraints: key 格式不能变
facts: prefill 实测 1305 tok/s
rejected: Redis 计数（运维不批）
current: 改完 guard.py
next: 补测试"""

GOAL = "把这套服务的配额从按次改成按 token 计量，并补测试。"


def history(n: int = 120) -> list[dict]:
    """造一段必须先裁剪才能装下的历史，中间夹着会被丢掉的决策句。

    assistant 必须带 tool_calls、tool 必须带 tool_call_id：真实的 agent 历史
    就长这样，不带的话裁剪会走硬截断而不是成对丢弃，测不到主路径。
    """
    msgs: list[dict] = [{"role": "system", "content": "你是编码助手。"}, {"role": "user", "content": GOAL}]
    for i in range(n):
        msgs.append(
            {
                "role": "assistant",
                "content": f"第 {i} 步：跑测试并记录结果。",
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": "run_tests", "arguments": json.dumps({"batch": i})},
                    }
                ],
            }
        )
        msgs.append(
            {
                "role": "tool",
                "tool_call_id": f"call_{i}",
                "content": f"PASSED test_{i}\n" + "日志行\n" * 40,
            }
        )
    msgs.append({"role": "user", "content": "继续。"})
    return msgs


class Upstream:
    def __init__(self):
        self.local: list[dict] = []
        self.spine_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"default_generation_settings": {"n_ctx": 6000}, "total_slots": 1}
            )
        if request.url.path == "/tokenize":
            # 让精确计数不可用：否则 factor 会被 mock 的假 token 数带着漂，
            # 裁剪点跟着漂，骨架缓存键就永远对不上。
            return httpx.Response(404, json={"error": "not supported"})
        body = json.loads(request.content or b"{}")
        return self.local_chat(body)

    def local_chat(self, body: dict) -> httpx.Response:
        self.local.append(body)
        system = str((body.get("messages") or [{}])[0].get("content") or "")
        if "只输出六行" in system:
            self.spine_calls += 1
            return self._text(SPINE_REPLY, body)
        return self._text("已完成。", body)

    def _text(self, text: str, body: dict | None = None) -> httpx.Response:
        # prompt_tokens 必须跟着请求走。写死一个小值会让估算器每次
        # 都判成「静默截断」，预算一路缩，裁剪点漂移，测试就不稳定了。
        # 工具定义也要算进去：真实上游的 prompt_tokens 是含 tools 的，
        # 漏掉会让估算器每轮把系数压低约 7%。
        prompt_tokens = 20
        if body is not None:
            est = Estimator()
            msgs = body.get("messages") or []
            prompt_tokens = max(20, est.messages(msgs, est.tools_tokens(body.get("tools") or [])))
        return httpx.Response(
            200,
            json={
                "id": "l1",
                "object": "chat.completion",
                "created": 0,
                "model": "local-m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 10},
            },
        )


def make_cfg(tmp_path, **ctx_overrides) -> AppCfg:
    params = dict(
        state_file=str(tmp_path / "ctx.json"),
        spine_state_file=str(tmp_path / "spine.json"),
        ctx_limit=6000,
        usable_ratio=0.85,
        reserve_output=False,
        safety_tokens=0,
        min_clear_tokens=0,
        keep_recent=2,
        spine_min_dropped=100,
    )
    params.update(ctx_overrides)
    return AppCfg(
        auth=AuthCfg(mode="off", keys_file=str(tmp_path / "keys.json")),
        base=EndpointCfg(base_url="http://local.test/v1", model="local-m", api_key="k", timeout_s=10, max_tokens=256),
        advanced=EndpointCfg(base_url="http://cloud.test/v1", model="cloud-m", api_key="k", timeout_s=10),
        guard=GuardCfg(
            state_file=str(tmp_path / "usage.json"),
            max_calls_per_turn=1,
            max_tool_rounds=2,
            min_interval_s=0,
            daily_limit=5,
        ),
        context=ContextCfg(**params),
        artifacts=ArtifactsCfg(dir=str(tmp_path / "art"), enabled=True),
    )


def ask(app, messages: list[dict]) -> httpx.Response:
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
            return await client.post("/v1/chat/completions", json={"model": "x", "messages": messages})

    return asyncio.run(go())


# ---------------------------------------------------------------- 主链路


def test_spine_reaches_local_model_after_trim(tmp_path):
    up = Upstream()
    app = create_app(make_cfg(tmp_path), httpx.MockTransport(up.handler))
    resp = ask(app, history())
    assert resp.status_code == 200
    assert up.spine_calls == 1

    sent = str((up.local[-1].get("messages") or [{}])[0].get("content") or "")
    assert "<<<SPINE" in sent
    assert "rejected: Redis 计数" in sent
    assert "facts: prefill 实测 1305 tok/s" in sent


def test_spine_keeps_task_goal_after_trim(tmp_path):
    """骨架的核心价值：目标句不能被裁掉。"""
    up = Upstream()
    app = create_app(make_cfg(tmp_path), httpx.MockTransport(up.handler))
    ask(app, history())
    flat = json.dumps(up.local[-1]["messages"], ensure_ascii=False)
    assert "按 token 计量" in flat


def test_trim_still_happens_with_spine(tmp_path):
    """加了骨架不等于不裁剪，只是裁完留下线索。"""
    up = Upstream()
    app = create_app(make_cfg(tmp_path), httpx.MockTransport(up.handler))
    resp = ask(app, history())
    ctx = resp.json()["x_steward"]["context"]
    assert ctx["saved"] > 0
    assert len(up.local[-1]["messages"]) < len(history())


def test_spine_is_cached_across_requests(tmp_path):
    """客户端每轮都重发同样的历史，第二次不该再花一次提炼调用。"""
    up = Upstream()
    app = create_app(make_cfg(tmp_path), httpx.MockTransport(up.handler))
    ask(app, history())
    ask(app, history())
    assert up.spine_calls == 1


def test_spine_does_not_duplicate_across_rounds(tmp_path):
    up = Upstream()
    app = create_app(make_cfg(tmp_path), httpx.MockTransport(up.handler))
    ask(app, history())
    ask(app, history())
    sent = str((up.local[-1].get("messages") or [{}])[0].get("content") or "")
    assert sent.count("<<<SPINE") == 1


def test_spine_disabled_leaves_only_count_note(tmp_path):
    up = Upstream()
    app = create_app(make_cfg(tmp_path, spine=False), httpx.MockTransport(up.handler))
    ask(app, history())
    assert up.spine_calls == 0
    sent = str((up.local[-1].get("messages") or [{}])[0].get("content") or "")
    assert "<<<SPINE" not in sent
    assert "被压缩" in sent  # 至少还有那行计数提示


def test_small_drop_does_not_pay_for_a_spine(tmp_path):
    """只丢了几百 token 就调一次模型，纯属浪费。"""
    up = Upstream()
    app = create_app(make_cfg(tmp_path, spine_min_dropped=10**9), httpx.MockTransport(up.handler))
    ask(app, history())
    assert up.spine_calls == 0


def test_spine_failure_falls_back_to_mechanical(tmp_path):
    class Dumb(Upstream):
        def local_chat(self, body):
            self.local.append(body)
            system = str((body.get("messages") or [{}])[0].get("content") or "")
            if "只输出六行" in system:
                self.spine_calls += 1
                return self._text("我不太确定……", body)  # 模型没按要求输出
            return self._text("已完成。", body)

    up = Dumb()
    app = create_app(make_cfg(tmp_path), httpx.MockTransport(up.handler))
    resp = ask(app, history())
    assert resp.status_code == 200
    sent = str((up.local[-1].get("messages") or [{}])[0].get("content") or "")
    # 机械兜底至少保住目标
    assert "按 token 计量" in sent


def test_admin_context_reports_spine(tmp_path):
    up = Upstream()
    app = create_app(make_cfg(tmp_path), httpx.MockTransport(up.handler))
    ask(app, history())
    r = asyncio.run(_get(app, "/admin/context"))
    data = r.json()
    assert data["spine"]["enabled"] is True
    # goal 必须来自用户原话，不是模型对「最近在干什么」的概括
    assert data["spine"]["last_goal"] == GOAL


async def _get(app, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        return await client.get(path)
