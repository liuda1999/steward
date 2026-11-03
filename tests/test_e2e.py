import asyncio
import json

import httpx

from steward.config import AppCfg, AuthCfg, ContextCfg, EndpointCfg, GuardCfg
from steward.server import create_app


class MockAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


class Upstreams:
    """假本地模型 + 假云端，行为由用户消息里的关键词驱动。"""

    def __init__(self):
        self.cloud_calls: list[dict] = []
        self.local_calls = 0
        self.last_messages: list[dict] = []
        self.last_body: dict = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        body = json.loads(request.content or b"{}")
        if host == "local.test":
            return self.local(body)
        if host == "cloud.test":
            return self.cloud(body)
        return httpx.Response(404, json={"error": "unexpected host"})

    # ---- 假本地模型 ----

    def local(self, body: dict) -> httpx.Response:
        self.local_calls += 1
        self.last_body = body
        msgs = body.get("messages") or []
        self.last_messages = msgs
        tool_count = sum(1 for m in msgs if m.get("role") == "tool")
        user = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                c = m.get("content")
                user = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                break

        if "DIRECT" in user:
            return self._text("直接回答，不需要外援。")

        if "EMPTY" in user:
            # 模拟推理吃光预算：小预算时空回复 + finish_reason=length
            if int(body.get("max_tokens") or 0) < 2000:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "", "reasoning_content": "想了很久"},
                                "finish_reason": "length",
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 1024},
                    },
                )
            return self._text("预算够了，正常回答。")

        if "ONE" in user:
            if tool_count == 0:
                return self._consult("design", "缓存层该怎么分层", "goal: 提升读性能\ngiven: 单机 QPS 2k")
            return self._text("已按交付物执行完毕。")

        if "LOOP" in user:
            # 三个问题必须实质不同：相似度过高会被去重闸拦下，
            # 那样测到的是去重，不是轮次上限。
            asks = [
                ("缓存穿透该怎么防", "goal: 挡住不存在的 key\ngiven: 单机 QPS 2k"),
                ("分片键该按什么选", "goal: 避免热点分片\ngiven: 订单表 4 亿行"),
                ("加节点时一致性哈希怎么迁移", "goal: 扩容不中断\ngiven: 现在 8 个节点"),
            ]
            ask, brief = asks[min(tool_count, len(asks) - 1)]
            return self._consult("design", ask, brief)

        if "DUP" in user:
            if tool_count < 2:
                return self._consult("design", "同一个核心问题", "goal: 同一个目标\ngiven: 同一批事实")
            return self._text("done")

        if "TOOLPASS" in user:
            return self._toolcall("read_file", {"path": "a.py"})

        return self._text("默认回答。")

    # ---- 假云端 ----

    def cloud(self, body: dict) -> httpx.Response:
        self.cloud_calls.append(body)
        doc = (
            "## 1. 结论\n用两级缓存，L1 进程内、L2 Redis。\n\n"
            "## 2. 执行步骤\n1. 动作：接 L1\n   输入：热点 key 列表\n   输出：命中率指标\n   失败判据：命中率 < 60%\n\n"
            "## 3. 风险与不确定项\n- 一致性延迟，触发条件是写多读少\n\n"
            "## 4. 待本地补充\n- 现有 Redis 规格"
        )
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "cloud-m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": doc}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 80},
            },
            request=None,
        )

    # ---- 响应构造 ----

    def _text(self, text: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "l1",
                "object": "chat.completion",
                "created": 0,
                "model": "local-m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    def _consult(self, kind: str, ask: str, brief: str) -> httpx.Response:
        return self._toolcall("consult_cloud", {"kind": kind, "ask": ask, "brief": brief})

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
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )


def make_cfg(tmp_path, **guard_overrides) -> AppCfg:
    params = dict(
        state_file=str(tmp_path / "usage.json"),
        max_calls_per_turn=2,
        max_tool_rounds=3,
        min_interval_s=0,
        daily_limit=10,
        dedupe=True,
    )
    params.update(guard_overrides)
    return AppCfg(
        auth=AuthCfg(mode="off", keys_file=str(tmp_path / "keys.json")),
        base=EndpointCfg(base_url="http://local.test/v1", model="local-m", api_key="local-key", timeout_s=10),
        advanced=EndpointCfg(base_url="http://cloud.test/v1", model="cloud-m", api_key="cloud-key", timeout_s=10),
        guard=GuardCfg(**params),
        # 显式指定，否则骨架缓存会落到 cwd 的 state/ 里，测试互相污染
        context=ContextCfg(
            state_file=str(tmp_path / "ctx.json"),
            spine_state_file=str(tmp_path / "spine.json"),
        ),
    )


def post(app, payload: dict) -> httpx.Response:
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            return await client.post("/v1/chat/completions", json=payload)

    return asyncio.run(go())


def ask(app, text: str, **extra) -> httpx.Response:
    payload = {"model": "local", "messages": [{"role": "user", "content": text}]}
    payload.update(extra)
    return post(app, payload)


def test_direct_answer_costs_nothing(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(up.handler))
    resp = ask(app, "DIRECT 帮我改个变量名")

    assert resp.status_code == 200
    data = resp.json()
    assert data["x_steward"]["cloud_calls"] == 0
    assert len(up.cloud_calls) == 0
    assert "直接回答" in data["choices"][0]["message"]["content"]


def test_empty_reply_escalates_max_tokens(tmp_path):
    """推理型模型把预算烧光时，网关应加倍重试而不是返回空。"""
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(up.handler))
    resp = ask(app, "EMPTY 说点什么", max_tokens=500)

    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "预算够了，正常回答。"
    assert up.local_calls == 2


def test_thinking_off_injects_chat_template_kwargs(tmp_path):
    up = Upstreams()
    cfg = make_cfg(tmp_path)
    cfg.base.thinking = "off"
    app = create_app(cfg, MockAsyncTransport(up.handler))

    ask(app, "DIRECT hi")

    assert up.last_body["chat_template_kwargs"] == {"enable_thinking": False}


def test_thinking_auto_does_not_inject(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(up.handler))

    ask(app, "DIRECT hi")

    assert "chat_template_kwargs" not in up.last_body


def test_one_consult_returns_deliverable(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(up.handler))
    resp = ask(app, "ONE 设计一个缓存层")

    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    assert len(up.cloud_calls) == 1
    assert data["x_steward"]["cloud_calls"] == 1
    assert data["x_steward"]["remaining_today"] == 9
    assert "已按交付物执行完毕" in content
    assert resp.headers["x-steward-calls"] == "1"

    injected = [m for m in up.last_messages if m.get("role") == "tool"]
    assert injected and "## 1. 结论" in str(injected[0].get("content"))
    assert injected[0]["tool_call_id"] == "call_1"


def test_loop_stopped_by_turn_limit(tmp_path):
    up = Upstreams()
    cfg = make_cfg(tmp_path, max_calls_per_turn=2, max_tool_rounds=3)
    app = create_app(cfg, MockAsyncTransport(up.handler))
    resp = ask(app, "LOOP 无休止地问")

    data = resp.json()
    assert len(up.cloud_calls) == 2
    assert data["x_steward"]["cloud_calls"] == 2
    assert data["x_steward"]["cloud_denied"] >= 1
    assert data["x_steward"]["rounds"] <= 3


def test_duplicate_brief_blocked(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(up.handler))
    resp = ask(app, "DUP 换着法问同一个问题")

    data = resp.json()
    assert len(up.cloud_calls) == 1
    assert data["x_steward"]["cloud_denied"] >= 1
    assert data["x_steward"]["cloud_calls"] == 1


def test_daily_limit_across_requests(tmp_path):
    up = Upstreams()
    cfg = make_cfg(tmp_path, daily_limit=1, max_calls_per_turn=5)
    app = create_app(cfg, MockAsyncTransport(up.handler))

    first = ask(app, "ONE 第一个请求").json()
    second = ask(app, "ONE 第二个请求").json()

    assert len(up.cloud_calls) == 1
    assert first["x_steward"]["cloud_calls"] == 1
    assert second["x_steward"]["cloud_calls"] == 0
    assert second["x_steward"]["cloud_denied"] >= 1


def test_client_tools_pass_through(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(up.handler))
    resp = ask(
        app,
        "TOOLPASS 读一下文件",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "读文件",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ],
    )

    data = resp.json()
    calls = data["choices"][0]["message"]["tool_calls"]
    assert calls[0]["function"]["name"] == "read_file"
    assert data["choices"][0]["finish_reason"] == "tool_calls"
    assert len(up.cloud_calls) == 0


def test_streaming_sse(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(up.handler))
    resp = ask(app, "ONE 流式返回", stream=True)

    assert resp.status_code == 200
    assert "[DONE]" in resp.text
    assert "chat.completion.chunk" in resp.text
    assert "已按交付物执行完毕" in resp.text


def test_cloud_not_configured_blocks_without_charging(tmp_path):
    up = Upstreams()
    cfg = make_cfg(tmp_path)
    cfg.advanced.model = ""  # 没有模型名 = 没配好
    app = create_app(cfg, MockAsyncTransport(up.handler))

    data = ask(app, "ONE 设计缓存层").json()

    assert len(up.cloud_calls) == 0
    assert data["x_steward"]["cloud_calls"] == 0
    assert data["x_steward"]["cloud_denied"] >= 1


def test_advanced_without_key_is_allowed(tmp_path):
    """进阶槽位可以是本地端点，不该强制要 key。"""
    up = Upstreams()
    cfg = make_cfg(tmp_path)
    cfg.advanced.api_key = ""
    app = create_app(cfg, MockAsyncTransport(up.handler))

    data = ask(app, "ONE 设计缓存层").json()

    assert len(up.cloud_calls) == 1
    assert data["x_steward"]["cloud_calls"] == 1


def test_health_and_models(tmp_path):
    up = Upstreams()
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(up.handler))

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            h = await client.get("/health")
            m = await client.get("/v1/models")
            return h, m

    health, models = asyncio.run(go())
    assert health.status_code == 200
    assert health.json()["quota"]["daily_limit"] == 10
    assert models.json()["data"][0]["id"] == "auto"
