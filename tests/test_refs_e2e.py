# SPDX-License-Identifier: Apache-2.0
"""refs 的端到端：真的走一遍网关，看文件有没有进云端、有没有留在本地。"""

from __future__ import annotations

import asyncio
import json

import httpx

from steward.config import (
    AppCfg,
    ArtifactsCfg,
    AuthCfg,
    ContextCfg,
    EndpointCfg,
    GuardCfg,
    RefsCfg,
)
from steward.server import create_app

SRC = """\
import time


def refresh_token(user, now=None):
    now = now or time.time()
    if user.token_expires_at < now:
        user.token = issue_new(user)
        user.token_expires_at = now + 3600
    return user.token
"""


class Mock:
    """本地槽位第一轮要求带 refs 咨询，第二轮收尾；云端原样记录收到的请求体。"""

    def __init__(self, refs_payload=None):
        self.refs_payload = refs_payload
        self.local_requests: list[dict] = []
        self.cloud_requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(
                200, json={"default_generation_settings": {"n_ctx": 131072}, "total_slots": 1}
            )
        body = json.loads(request.content or b"{}")
        if request.url.host == "cloud.test":
            self.cloud_requests.append(body)
            return self._completion("## 结论\n加锁。", prompt_tokens=1200)
        return self.local(body)

    def local(self, body: dict) -> httpx.Response:
        self.local_requests.append(body)
        done = any(m.get("role") == "tool" for m in body.get("messages") or [])
        if done or self.refs_payload is None:
            return self._completion("本地收尾。")
        args = {
            "kind": "diagnosis",
            "ask": "刷新逻辑哪里有问题",
            "brief": "goal: 修 token 刷新\ntried: 单测跑三遍，第三遍 refresh 返回旧的 access_token",
        }
        if self.refs_payload is not False:
            args["refs"] = self.refs_payload
        return httpx.Response(
            200,
            json={
                "id": "l",
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
                                    "function": {
                                        "name": "consult_cloud",
                                        "arguments": json.dumps(args, ensure_ascii=False),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 500, "completion_tokens": 20},
            },
        )

    def _completion(self, text: str, prompt_tokens: int = 500) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 30},
            },
        )


def make_app(tmp_path, mock: Mock, refs: RefsCfg | None = None):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "auth.py").write_text(SRC, encoding="utf-8")
    (root / ".env").write_text("API_KEY=sk-real-secret", encoding="utf-8")

    cfg = AppCfg(
        auth=AuthCfg(mode="off", keys_file=str(tmp_path / "keys.json")),
        base=EndpointCfg(base_url="http://local.test/v1", model="local-m", api_key="k", timeout_s=10, max_tokens=512),
        advanced=EndpointCfg(
            base_url="http://cloud.test/v1", model="cloud-m", api_key="k", timeout_s=10, max_tokens=2048
        ),
        guard=GuardCfg(
            state_file=str(tmp_path / "usage.json"),
            max_calls_per_turn=2,
            max_tool_rounds=3,
            min_interval_s=0,
            daily_limit=10,
        ),
        context=ContextCfg(state_file=str(tmp_path / "ctx.json"), min_clear_tokens=0),
        artifacts=ArtifactsCfg(dir=str(tmp_path / "artifacts"), enabled=False),
        refs=refs
        or RefsCfg(
            enabled=True,
            roots=[str(root)],
            audit_file=str(tmp_path / "refs.jsonl"),
        ),
    )
    transport = httpx.MockTransport(mock.handler)
    return create_app(cfg, transport=transport), root


def post(app, payload: dict) -> httpx.Response:
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
            return await client.post("/v1/chat/completions", json=payload)

    return asyncio.run(go())


def ask(app, **extra):
    payload = {"model": "local", "messages": [{"role": "user", "content": "刷新的 token 每次都重建"}]}
    payload.update(extra)
    return post(app, payload)


# ---------------------------------------------------------------- 工具描述


def test_refs_param_advertised_only_when_enabled(tmp_path):
    mock = Mock(refs_payload=[{"path": "src/auth.py"}])
    app, _ = make_app(tmp_path, mock, refs=RefsCfg(enabled=False, roots=[]))
    ask(app)
    tools = mock.local_requests[0]["tools"]
    consult = next(t for t in tools if t["function"]["name"] == "consult_cloud")
    assert "refs" not in consult["function"]["parameters"]["properties"]


def test_refs_param_present_when_enabled(tmp_path):
    mock = Mock(refs_payload=[{"path": "src/auth.py"}])
    app, _ = make_app(tmp_path, mock)
    ask(app)
    consult = next(t for t in mock.local_requests[0]["tools"] if t["function"]["name"] == "consult_cloud")
    assert "refs" in consult["function"]["parameters"]["properties"]


# ---------------------------------------------------------------- 展开


def test_file_content_reaches_cloud_not_local(tmp_path):
    """整个功能的意义就在这一条：原文进云端，不进本地窗口。"""
    mock = Mock(refs_payload=[{"path": "src/auth.py"}])
    app, _ = make_app(tmp_path, mock)
    resp = ask(app)
    assert resp.status_code == 200

    cloud_text = mock.cloud_requests[0]["messages"][-1]["content"]
    assert "def refresh_token" in cloud_text
    assert "[ref] src/auth.py" in cloud_text
    assert "```python" in cloud_text

    # 本地模型的每一轮请求里都不该出现这段源码
    for req in mock.local_requests:
        blob = json.dumps(req, ensure_ascii=False)
        assert "def refresh_token" not in blob


def test_range_is_honoured(tmp_path):
    mock = Mock(refs_payload=[{"path": "src/auth.py", "range": "4-6"}])
    app, _ = make_app(tmp_path, mock)
    ask(app)
    cloud_text = mock.cloud_requests[0]["messages"][-1]["content"]
    assert "def refresh_token" in cloud_text
    assert "issue_new" not in cloud_text


def test_deliverable_and_refs_coexist(tmp_path):
    mock = Mock(refs_payload=[{"path": "src/auth.py"}])
    app, _ = make_app(tmp_path, mock)
    resp = ask(app)
    tool_msgs = [
        m for m in mock.local_requests[1]["messages"] if m.get("role") == "tool"
    ]
    assert "已附上 1 个文件" in tool_msgs[0]["content"]
    assert "加锁" in tool_msgs[0]["content"]
    assert resp.headers.get("X-Steward-Refs") == "1"


# ---------------------------------------------------------------- 拒绝


def test_denied_file_never_leaves_and_is_reported(tmp_path):
    mock = Mock(refs_payload=[{"path": "src/auth.py"}, {"path": ".env"}])
    app, _ = make_app(tmp_path, mock)
    ask(app)
    cloud_blob = json.dumps(mock.cloud_requests[0], ensure_ascii=False)
    assert "sk-real-secret" not in cloud_blob
    tool_msgs = [m for m in mock.local_requests[1]["messages"] if m.get("role") == "tool"]
    assert "未附" in tool_msgs[0]["content"]
    assert ".env" in tool_msgs[0]["content"]


def test_escape_attempt_is_reported(tmp_path):
    mock = Mock(refs_payload=[{"path": "../../etc/passwd"}])
    app, _ = make_app(tmp_path, mock)
    resp = ask(app)
    assert resp.status_code == 200
    tool_msgs = [m for m in mock.local_requests[1]["messages"] if m.get("role") == "tool"]
    assert "不在白名单内" in tool_msgs[0]["content"]


def test_all_refs_denied_still_consult(tmp_path):
    """引用全废不该让整次咨询失败，brief 本身还有价值。"""
    mock = Mock(refs_payload=[{"path": ".env"}])
    app, _ = make_app(tmp_path, mock)
    resp = ask(app)
    assert resp.status_code == 200
    assert mock.cloud_requests  # 还是问了


def test_disabled_ignores_refs_gracefully(tmp_path):
    mock = Mock(refs_payload=[{"path": "src/auth.py"}])
    app, _ = make_app(tmp_path, mock, refs=RefsCfg(enabled=False, roots=[]))
    resp = ask(app)
    assert resp.status_code == 200
    cloud_text = mock.cloud_requests[0]["messages"][-1]["content"]
    assert "def refresh_token" not in cloud_text
    tool_msgs = [m for m in mock.local_requests[1]["messages"] if m.get("role") == "tool"]
    assert "未启用" in tool_msgs[0]["content"]


# ---------------------------------------------------------------- 审计


def test_audit_records_expansion(tmp_path):
    mock = Mock(refs_payload=[{"path": "src/auth.py"}, {"path": ".env"}])
    app, _ = make_app(tmp_path, mock)
    ask(app)
    lines = (tmp_path / "refs.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["files"] == ["src/auth.py"]
    assert any(".env" in r for r in rec["rejected"])


def test_admin_context_exposes_refs_policy(tmp_path):
    mock = Mock(refs_payload=None)
    app, root = make_app(tmp_path, mock)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
            return await client.get("/admin/context")

    body = asyncio.run(go()).json()
    assert body["refs"]["enabled"] is True
    assert body["refs"]["roots"] == [str(root.resolve())]
