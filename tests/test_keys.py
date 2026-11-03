# SPDX-License-Identifier: Apache-2.0
import asyncio
from pathlib import Path

import httpx

from steward.__main__ import main
from steward.config import AppCfg, AuthCfg, EndpointCfg, GuardCfg
from steward.keys import KeyStore
from steward.server import create_app


class Echo:
    """上游永远返回一句话，只用来验证鉴权是否放行。"""

    def handler(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "l1",
                "object": "chat.completion",
                "created": 0,
                "model": "local-m",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


class MockAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


def make_cfg(tmp_path, **auth_overrides) -> AppCfg:
    params = dict(mode="required", keys_file=str(tmp_path / "keys.json"), auto_issue=True)
    params.update(auth_overrides)
    return AppCfg(
        auth=AuthCfg(**params),
        base=EndpointCfg(base_url="http://local.test/v1", model="local-m", api_key="local-key"),
        advanced=EndpointCfg(base_url="http://cloud.test/v1", model="cloud-m", api_key="cloud-key"),
        guard=GuardCfg(state_file=str(tmp_path / "usage.json"), min_interval_s=0, daily_limit=10),
    )


def call(app, headers=None, payload=None):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
            return await client.post(
                "/v1/chat/completions",
                json=payload or {"model": "local", "messages": [{"role": "user", "content": "hi"}]},
                headers=headers or {},
            )

    return asyncio.run(go())


# ---------------------------------------------------------------- 存储层


def test_issue_format_and_hash_only(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    token, rec = store.issue("workbuddy")

    assert token.startswith("st-")
    assert token.split("-")[1] == rec.id
    assert rec.label == "workbuddy"

    raw = (tmp_path / "keys.json").read_text(encoding="utf-8")
    assert token not in raw, "明文 key 不应该落盘"
    assert rec.hash in raw


def test_verify_and_revoke(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    token, rec = store.issue()

    assert store.verify(token) is not None
    assert store.verify(token[:-1] + "x") is None
    assert store.verify("sk-whatever") is None
    assert store.verify("") is None

    assert store.revoke(rec.id) is True
    assert store.verify(token) is None
    assert store.revoke(rec.id) is False


def test_store_reloads_from_disk(tmp_path):
    path = tmp_path / "keys.json"
    token, _ = KeyStore(path).issue("a")
    assert KeyStore(path).verify(token) is not None


# ---------------------------------------------------------------- 鉴权闸


def test_missing_key_rejected(tmp_path):
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(Echo().handler))
    resp = call(app, headers={})
    assert resp.status_code == 401


def test_bad_key_rejected(tmp_path):
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(Echo().handler))
    assert call(app, headers={"Authorization": "Bearer st-deadbeef-nope"}).status_code == 401
    assert call(app, headers={"Authorization": "Bearer random"}).status_code == 401


def test_issued_key_works(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    token, _ = store.issue("agent")

    cfg = make_cfg(tmp_path, auto_issue=False)
    app = create_app(cfg, MockAsyncTransport(Echo().handler))

    assert call(app, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert call(app, headers={"X-API-Key": token}).status_code == 200


def test_bootstrap_key_issued_and_usable(tmp_path):
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(Echo().handler))
    token = app.state.bootstrap_key

    assert token.startswith("st-")
    resp = call(app, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["x_steward"]["key"] == token.split("-")[1]

    raw = Path(tmp_path / "keys.json").read_text(encoding="utf-8")
    assert token not in raw


def test_existing_key_not_reissued(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    store.issue("first")
    app = create_app(make_cfg(tmp_path), MockAsyncTransport(Echo().handler))
    assert app.state.bootstrap_key == ""


def test_legacy_static_key_still_works(tmp_path):
    cfg = make_cfg(tmp_path, auto_issue=False)
    cfg.server.api_key = "static-secret"
    app = create_app(cfg, MockAsyncTransport(Echo().handler))

    assert call(app, headers={"Authorization": "Bearer static-secret"}).status_code == 200
    assert call(app, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_auth_off_allows_anonymous(tmp_path):
    app = create_app(make_cfg(tmp_path, mode="off"), MockAsyncTransport(Echo().handler))
    assert call(app, headers={}).status_code == 200


def test_key_usage_recorded(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    token, rec = store.issue("agent")
    app = create_app(make_cfg(tmp_path, auto_issue=False), MockAsyncTransport(Echo().handler))

    call(app, headers={"Authorization": f"Bearer {token}"})

    reloaded = KeyStore(tmp_path / "keys.json").get(rec.id)
    assert reloaded is not None
    assert reloaded.last_used_at != ""


# ---------------------------------------------------------------- CLI


def test_cli_init_creates_config_env_and_key(tmp_path, capsys):
    cfg_path = tmp_path / "config.yaml"
    code = main(
        [
            "init",
            "--no-input",
            "-c",
            str(cfg_path),
            "--base-url",
            "http://127.0.0.1:11434/v1",
            "--base-model",
            "qwen3-30b-a3b",
            "--adv-url",
            "https://api.example.com/v1",
            "--adv-model",
            "planner",
            "--adv-key",
            "sk-cloud-123",
            "--port",
            "8011",
        ]
    )
    assert code == 0
    assert cfg_path.exists()
    assert "qwen3-30b-a3b" in cfg_path.read_text(encoding="utf-8")

    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ADVANCED_API_KEY=sk-cloud-123" in env

    out = capsys.readouterr().out
    token = [line.strip() for line in out.splitlines() if line.strip().startswith("st-")][0]
    assert KeyStore(tmp_path / "state" / "keys.json").verify(token) is not None


def test_cli_init_refuses_overwrite(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("server: {}\n", encoding="utf-8")
    assert main(["init", "--no-input", "-c", str(cfg_path)]) == 1


def test_cli_key_new_list_revoke(tmp_path, capsys):
    cfg_path = tmp_path / "config.yaml"
    main(["init", "--no-input", "-c", str(cfg_path), "--key-label", "first"])

    assert main(["key", "new", "-c", str(cfg_path), "--label", "second"]) == 0
    out = capsys.readouterr().out
    token = [line.strip() for line in out.splitlines() if line.strip().startswith("st-")][0]
    key_id = token.split("-")[1]

    assert main(["key", "list", "-c", str(cfg_path)]) == 0
    listing = capsys.readouterr().out
    assert "first" in listing and "second" in listing

    assert main(["key", "revoke", key_id, "-c", str(cfg_path)]) == 0
    assert KeyStore(tmp_path / "state" / "keys.json").verify(token) is None


def test_cli_check_reports_auth(tmp_path, capsys):
    cfg_path = tmp_path / "config.yaml"
    main(["init", "--no-input", "-c", str(cfg_path), "--base-model", "m", "--adv-model", "c", "--adv-key", "k"])
    capsys.readouterr()

    assert main(["serve", "--check", "-c", str(cfg_path)]) == 0
    assert "鉴权=required" in capsys.readouterr().out
