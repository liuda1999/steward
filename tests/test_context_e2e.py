# SPDX-License-Identifier: Apache-2.0
import asyncio
import json
from pathlib import Path

import httpx

from steward.artifacts import Store
from steward.config import AppCfg, ArtifactsCfg, AuthCfg, ContextCfg, EndpointCfg, GuardCfg
from steward.context import Estimator, count_tokens
from steward.server import create_app

LONG_DOC = (
    "## 1. 结论\n用两级缓存，L1 进程内、L2 Redis。\n\n"
    "## 2. 执行步骤\n"
    + "\n".join(f"{i}. 动作：第 {i} 步，失败判据是错误率上升超过 5%" for i in range(1, 80))
    + "\n\n## 3. 风险与不确定项\n- 一致性延迟\n\n## 4. 待本地补充\n- Redis 规格"
)


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


class Upstreams:
    """可控的假上游：能拒绝超长请求、能谎报 token 数、能给长交付物。"""

    def __init__(self, *, max_tokens_limit=None, prompt_tokens=None, consult_first=False, cloud_text=None):
        self.max_tokens_limit = max_tokens_limit
        self.prompt_tokens = prompt_tokens
        self.consult_first = consult_first
        self.cloud_text = cloud_text or LONG_DOC
        self.local_requests: list[dict] = []
        self.cloud_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(
                200,
                json={"default_generation_settings": {"n_ctx": 131072}, "total_slots": 1},
            )
        body = json.loads(request.content or b"{}")
        if request.url.host == "local.test":
            return self.local(body)
        if request.url.host == "cloud.test":
            return self.cloud(body)
        return httpx.Response(404, json={"error": "unexpected host"})

    def local(self, body: dict) -> httpx.Response:
        self.local_requests.append(body)
        msgs = body.get("messages") or []
        if self.max_tokens_limit:
            used = count_tokens(json.dumps(msgs, ensure_ascii=False))
            if used > self.max_tokens_limit:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "message": (
                                "the request exceeds the available context size. "
                                "try increasing the context size or enable context shift"
                            )
                        }
                    },
                )

        tool_count = sum(1 for m in msgs if m.get("role") == "tool")
        if self.consult_first and tool_count == 0:
            return self._toolcall(
                "consult_cloud",
                {"kind": "design", "ask": "缓存怎么分层", "brief": "goal: 提升读性能"},
            )
        return self._text("已完成。")

    def cloud(self, body: dict) -> httpx.Response:
        self.cloud_calls += 1
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "cloud-m",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": self.cloud_text}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 900},
            },
        )

    def _text(self, text: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "l1",
                "object": "chat.completion",
                "created": 0,
                "model": "local-m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": self.prompt_tokens if self.prompt_tokens is not None else 800,
                    "completion_tokens": 10,
                },
            },
        )

    def _toolcall(self, name: str, args: dict) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "l2",
                "object": "chat.completion",
                "created": 0,
                "model": "local-m",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 800, "completion_tokens": 10},
            },
        )


def make_cfg(tmp_path, context: ContextCfg | None = None) -> AppCfg:
    return AppCfg(
        auth=AuthCfg(mode="off", keys_file=str(tmp_path / "keys.json")),
        base=EndpointCfg(
            base_url="http://local.test/v1", model="local-m", api_key="k", timeout_s=10, max_tokens=512
        ),
        advanced=EndpointCfg(
            base_url="http://cloud.test/v1", model="cloud-m", api_key="k", timeout_s=10, max_tokens=2048
        ),
        guard=GuardCfg(
            state_file=str(tmp_path / "usage.json"),
            max_calls_per_turn=2,
            max_tool_rounds=3,
            min_interval_s=0,
            daily_limit=10,
            dedupe=True,
        ),
        context=context
        or ContextCfg(
            state_file=str(tmp_path / "ctx.json"),
            spine_state_file=str(tmp_path / "spine.json"),
            min_clear_tokens=0,
            keep_recent=2,
            # 这一组测的是裁剪本身；骨架会额外发一次本地调用，把断言搅乱。
            # 骨架有独立的 test_spine_e2e.py。
            spine=False,
        ),
        artifacts=ArtifactsCfg(dir=str(tmp_path / "artifacts"), enabled=True, inline_chars=600, min_store_chars=1500),
    )


def post(app, payload: dict) -> httpx.Response:
    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as client:
            return await client.post("/v1/chat/completions", json=payload)

    return asyncio.run(go())


def get(app, path: str) -> httpx.Response:
    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        ) as client:
            return await client.get(path)

    return asyncio.run(go())


def long_history(turns=40, chars=300) -> list[dict]:
    msgs = [{"role": "system", "content": "你是本地模型。"}]
    for i in range(turns):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"[{i}] " + ("内容" * (chars // 2))})
    return msgs


def ask(app, messages, **extra):
    payload = {"model": "local", "messages": messages}
    payload.update(extra)
    return post(app, payload)


# ---------------------------------------------------------------- 溢出恢复


def test_overflow_is_recovered_not_502(tmp_path):
    """上游说装不下时，应该压缩后重试，而不是把整轮扔掉。"""
    up = Upstreams(max_tokens_limit=7000)
    app = create_app(make_cfg(tmp_path), MockTransport(up.handler))

    resp = ask(app, long_history(turns=60))

    assert resp.status_code == 200, resp.text
    assert len(up.local_requests) >= 2

    sizes = [count_tokens(json.dumps(r.get("messages") or [], ensure_ascii=False)) for r in up.local_requests]
    assert sizes[-1] < sizes[0], "重试时的请求应该比第一次小"
    assert sizes[-1] <= 7000
    assert "x-steward-ctx" in resp.headers


def test_overflow_retry_learns_a_smaller_budget(tmp_path):
    up = Upstreams(max_tokens_limit=7000)
    cfg = make_cfg(tmp_path)
    app = create_app(cfg, MockTransport(up.handler))

    ask(app, long_history(turns=60))

    est = Estimator(state_path=cfg.context.state_file)
    assert est.budget_scale < 1.0, "溢出过一次就应该把预算缩下来并记住"


# ---------------------------------------------------------------- 静默截断


def test_silent_truncation_is_reported(tmp_path):
    """上游偷偷从头截断时不会报错，只能靠 token 数对不上来发现。"""
    up = Upstreams(prompt_tokens=3000)
    app = create_app(make_cfg(tmp_path), MockTransport(up.handler))

    resp = ask(app, long_history(turns=60))

    assert resp.status_code == 200
    assert resp.headers.get("x-steward-truncated") == "1"


def test_small_estimate_error_is_not_treated_as_truncation(tmp_path):
    """估算偏 20% 是常态，不该因此永久收缩预算。"""
    up = Upstreams(prompt_tokens=1700)
    cfg = make_cfg(tmp_path)
    app = create_app(cfg, MockTransport(up.handler))

    resp = ask(app, long_history(turns=10))

    assert resp.status_code == 200
    assert "x-steward-truncated" not in resp.headers
    est = Estimator(state_path=cfg.context.state_file)
    assert est.budget_scale == 1.0


def test_normal_usage_is_not_flagged(tmp_path):
    up = Upstreams()
    cfg = make_cfg(tmp_path)
    app = create_app(cfg, MockTransport(up.handler))

    # 依据上一次的估算值回一个合理的真实值
    resp = ask(app, [{"role": "user", "content": "简短问题"}])
    assert resp.status_code == 200
    assert "x-steward-truncated" not in resp.headers


# ---------------------------------------------------------------- 预算内裁剪


def test_over_budget_payload_is_trimmed_before_send(tmp_path):
    ctx = ContextCfg(
        state_file=str(tmp_path / "ctx.json"),
        ctx_limit=4000,
        usable_ratio=0.85,
        reserve_output=False,
        safety_tokens=0,
        min_clear_tokens=0,
        keep_recent=2,
        spine=False,
    )
    up = Upstreams()
    app = create_app(make_cfg(tmp_path, context=ctx), MockTransport(up.handler))

    resp = ask(app, long_history(turns=60))

    assert resp.status_code == 200
    sent = up.local_requests[0]["messages"]
    est = Estimator()
    assert est.messages(sent) <= 4000
    assert "[网关]" in sent[0]["content"]


def test_under_budget_payload_is_untouched(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockTransport(up.handler))

    msgs = [{"role": "user", "content": "很短的问题"}]
    resp = ask(app, msgs)

    assert resp.status_code == 200
    sent = up.local_requests[0]["messages"]
    assert all("[网关]" not in str(m.get("content")) for m in sent)


# ---------------------------------------------------------------- 交付物外置


def test_deliverable_is_archived_not_inlined(tmp_path):
    up = Upstreams(consult_first=True)
    cfg = make_cfg(tmp_path)
    app = create_app(cfg, MockTransport(up.handler))

    resp = ask(app, [{"role": "user", "content": "帮我设计一个缓存分层"}])

    assert resp.status_code == 200
    # 第二轮请求里应该带的是摘要，不是全文
    tool_msgs = [m for m in up.local_requests[-1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs, "交付物应该以 tool 消息回灌"
    payload = tool_msgs[-1]["content"]
    assert "[交付物已存档]" in payload
    assert len(payload) < 1200
    assert len(payload) < len(LONG_DOC) * 0.55

    files = list((tmp_path / "artifacts").rglob("*.md"))
    assert files, "全文应该落盘"
    body = files[0].read_text(encoding="utf-8")
    assert body.startswith("---\n")  # 统一抬头
    assert LONG_DOC in body


def test_deliverable_cost_stays_flat_as_document_grows(tmp_path):
    """关键不变量：交付物再长，回灌进上下文的也只是一小段。"""
    big = LONG_DOC * 8
    up = Upstreams(consult_first=True, cloud_text=big)
    app = create_app(make_cfg(tmp_path), MockTransport(up.handler))

    resp = ask(app, [{"role": "user", "content": "设计缓存"}])

    assert resp.status_code == 200
    payload = [m for m in up.local_requests[-1]["messages"] if m.get("role") == "tool"][-1]["content"]
    assert len(big) > 18_000
    assert len(payload) < 1300
    assert "[交付物已存档]" in payload


def test_short_deliverable_is_not_archived(tmp_path):
    up = Upstreams(consult_first=True, cloud_text="## 1. 结论\n短答复。")
    app = create_app(make_cfg(tmp_path), MockTransport(up.handler))

    resp = ask(app, [{"role": "user", "content": "一个小问题"}])

    assert resp.status_code == 200
    tool_msgs = [m for m in up.local_requests[-1]["messages"] if m.get("role") == "tool"]
    assert "[交付物已存档]" not in tool_msgs[-1]["content"]
    assert not list((tmp_path / "artifacts").rglob("*.md"))


# ---------------------------------------------------------------- 元信息


def test_models_exposes_real_context_window(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockTransport(up.handler))

    resp = get(app, "/v1/models")
    assert resp.status_code == 200
    item = resp.json()["data"][0]
    assert item["context_length"] > 0
    assert item["max_output_tokens"] == 512


def test_admin_context_reports_budget(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockTransport(up.handler))

    resp = get(app, "/admin/context")
    assert resp.status_code == 200
    data = resp.json()
    assert data["budget"] > 0
    assert data["estimator"]["factor"] > 0
    assert data["artifacts"]["enabled"] is True


def test_probe_endpoint_reads_upstream(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockTransport(up.handler))

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
            return await client.post("/admin/context/probe")

    resp = asyncio.run(go())
    assert resp.status_code == 200
    data = resp.json()
    assert data["limits"]["source"] == "props"
    assert data["limits"]["per_slot"] == 131072


def test_health_reports_context(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockTransport(up.handler))
    resp = get(app, "/health")
    assert resp.status_code == 200
    assert resp.json()["context"]["budget"] > 0
