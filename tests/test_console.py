# SPDX-License-Identifier: Apache-2.0
"""Web 控制台。

这个模块里有三类容易出错、且出错不报错的地方，测试就盯这三类：

1. **改配置改坏文件**。整份重写会丢注释，按行替换会漏字段，两种都能让
   用户退回 CLI。所以既验证"值改对了"，也验证"注释还在"。
2. **密钥泄漏到不该去的地方**。密钥该进 `.env`，`config.yaml` 里留占位符；
   接口回报状态时只能说"有没有"，不能把值吐回来。
3. **控制台自己被别人用**。跨站 POST（CSRF）和没登录的写操作都必须挡住 ——
   这个入口能改配置、能读到密钥是否存在，摆在 127.0.0.1 也不等于安全。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from steward.config import load_config
from steward.keys import KeyStore
from steward.server import create_app
from steward.webconsole import CLEAR, ConsoleError, apply_changes, set_in_text

COMMENTED = """\
# 这份文件里的注释是给人看的，保存后必须还在
server:
  host: 127.0.0.1
  port: 27418
  api_key: ""
  log_level: info

auth:
  mode: required
  keys_file: {keys}
  auto_issue: false

# 槽位一
base:
  base_url: http://127.0.0.1:11434/v1    # 这行尾巴上的注释也要留着
  api_key: ${{BASE_API_KEY:-}}
  model: qwen3
  protocol: chat
  thinking: "off"

advanced:
  base_url: ""
  api_key: ${{ADVANCED_API_KEY}}
  model: ""
  protocol: chat

guard:
  state_file: {state}/usage.json
  max_calls_per_turn: 2
  daily_limit: 5
  min_interval_s: 30

context:
  state_file: {state}/ctx.json
  spine_state_file: {state}/spine.json
  ctx_limit: 0
  target_ratio: 0.65
  spine: true

artifacts:
  dir: {state}/artifacts
  max_age_days: 30
  max_total_mb: 512

refs:
  enabled: false
  roots: []
"""


def write_cfg(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    path = tmp_path / "config.yaml"
    path.write_text(
        COMMENTED.format(keys=(state / "keys.json").as_posix(), state=state.as_posix()),
        encoding="utf-8",
    )
    return path


def make_app(tmp_path: Path):
    cfg_path = write_cfg(tmp_path)
    cfg = load_config(cfg_path)
    app = create_app(cfg, config_path=cfg_path)
    return app, cfg_path, app.state.console


class Client:
    """带 cookie 的测试客户端。控制台靠 cookie 认人，所以得有会话。"""

    def __init__(self, app):
        self.app = app

    def __enter__(self):
        self._ctx = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:27418"
        )
        self.client = asyncio.run(self._ctx.__aenter__())
        return self.client

    def __exit__(self, *exc):
        asyncio.run(self._ctx.__aexit__(*exc))


def call(
    app,
    method: str,
    path: str,
    body: dict | None = None,
    token: str = "",
    headers: dict | None = None,
    use_header: bool = False,
):
    """一次性请求，返回 (status, json)。

    token 给了就顺便登录。use_header=True 时改用请求头带口令 ——
    浏览器的主路径，cookie 只是普通浏览器里的便利。
    """

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:27418"
        ) as client:
            extra = dict(headers or {})
            if token and use_header:
                extra["X-Console-Token"] = token
            if token and not use_header:
                await client.post("/console/api/login", json={"token": token})
            method_fn = getattr(client, method)
            if body is None:
                return await method_fn(path, headers=extra)
            return await method_fn(path, json=body, headers=extra)

    resp = asyncio.run(go())
    try:
        return resp.status_code, resp.json()
    except json.JSONDecodeError:
        return resp.status_code, {"raw": resp.text}


# ---------------------------------------------------------------- 页面与登录


def test_page_is_served(tmp_path):
    app, _, _ = make_app(tmp_path)
    status, body = call(app, "get", "/")
    assert status == 200
    assert "steward" in body["raw"]


def test_state_needs_login(tmp_path):
    app, _, _ = make_app(tmp_path)
    status, body = call(app, "get", "/console/api/state")
    assert status == 401
    assert "未登录" in body["error"]


def test_login_with_right_token(tmp_path):
    app, _, console = make_app(tmp_path)
    status, body = call(app, "post", "/console/api/login", {"token": console.token})
    assert status == 200 and body["ok"]


def test_login_with_wrong_token(tmp_path):
    app, _, _ = make_app(tmp_path)
    status, body = call(app, "post", "/console/api/login", {"token": "nope"})
    assert status == 401
    assert "口令不对" in body["error"]


def test_login_is_rate_limited(tmp_path):
    """能改配置的入口不该允许无限次猜口令。"""
    app, _, _ = make_app(tmp_path)

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:27418"
        ) as client:
            codes = []
            for _ in range(10):
                r = await client.post("/console/api/login", json={"token": "wrong"})
                codes.append(r.status_code)
            return codes

    codes = asyncio.run(go())
    assert codes[:8] == [401] * 8
    assert 429 in codes[8:], codes


def test_token_file_is_created(tmp_path):
    _, _, console = make_app(tmp_path)
    assert console.path.is_file()
    assert console.path.read_text(encoding="utf-8").strip() == console.token


# ---------------------------------------------------------------- 写配置


def test_set_in_text_keeps_comments():
    text = "base:\n  model: old    # 这是一句说明\n  url: x\n"
    out = set_in_text(text, "base", "model", "new")
    assert "model: new    # 这是一句说明" in out
    assert out.count("\n") == text.count("\n")


def test_set_in_text_leaves_other_sections_alone():
    text = "a:\n  v: 1\nb:\n  v: 2\n"
    out = set_in_text(text, "b", "v", 20)
    assert "a:\n  v: 1" in out
    assert "b:\n  v: 20" in out


def test_set_in_text_adds_missing_key():
    """段里没有这个键要补一行，不是静默放弃。

    旧配置缺的字段（比如 timeout_s）如果只能改已有的键，界面上填了却
    没反应，用户只能靠猜。
    """
    text = "a:\n  v: 1\n\nb:\n  w: 2\n"
    out = set_in_text(text, "a", "nope", 2)
    assert "  nope: 2" in out
    assert out.index("nope: 2") < out.index("b:")  # 补在 a 段里，没跑到 b 段去
    import yaml

    assert yaml.safe_load(out) == {"a": {"v": 1, "nope": 2}, "b": {"w": 2}}


def test_set_in_text_missing_section_is_noop():
    """连这个段都没有就不乱加：交给写盘前校验去报错。"""
    text = "a:\n  v: 1\n"
    assert set_in_text(text, "nope", "v", 2) == text


def test_save_rewrites_values_and_keeps_comments(tmp_path):
    cfg_path = write_cfg(tmp_path)
    before = cfg_path.read_text(encoding="utf-8")
    result = apply_changes(
        cfg_path,
        tmp_path / ".env",
        {
            "guard": {"daily_limit": 9},
            "context": {"ctx_limit": 65536},
            "refs": {"enabled": True, "roots": [tmp_path.as_posix()]},
        },
    )
    after = cfg_path.read_text(encoding="utf-8")

    assert "daily_limit: 9" in after
    assert "ctx_limit: 65536" in after
    # 注释、空行、字段顺序都得留着
    assert "# 这份文件里的注释是给人看的，保存后必须还在" in after
    assert "# 这行尾巴上的注释也要留着" in after
    assert before.count("\n") == after.count("\n")
    assert "restart_required" in result and result["restart_required"]


def test_save_puts_secrets_in_env_not_in_yaml(tmp_path):
    cfg_path = write_cfg(tmp_path)
    env_path = tmp_path / ".env"
    apply_changes(
        cfg_path,
        env_path,
        {
            "base": {"api_key": "sk-local-secret"},
            "advanced": {"base_url": "https://api.example.com/v1", "model": "m", "api_key": "sk-cloud-secret"},
        },
    )
    yaml_text = cfg_path.read_text(encoding="utf-8")
    env_text = env_path.read_text(encoding="utf-8")

    assert "sk-local-secret" not in yaml_text
    assert "sk-cloud-secret" not in yaml_text
    assert "${BASE_API_KEY" in yaml_text
    assert "BASE_API_KEY=sk-local-secret" in env_text
    assert "ADVANCED_API_KEY=sk-cloud-secret" in env_text


def test_empty_api_key_means_do_not_touch(tmp_path):
    """界面上留空的密码框不能把已有 key 抹掉 —— 那是最容易犯的错。"""
    cfg_path = write_cfg(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("BASE_API_KEY=sk-original\n", encoding="utf-8")

    apply_changes(cfg_path, env_path, {"base": {"api_key": ""}})
    assert "BASE_API_KEY=sk-original" in env_path.read_text(encoding="utf-8")


def test_clear_removes_key(tmp_path):
    cfg_path = write_cfg(tmp_path)
    env_path = tmp_path / ".env"
    env_path.write_text("BASE_API_KEY=sk-original\nADVANCED_API_KEY=sk-keep\n", encoding="utf-8")

    apply_changes(cfg_path, env_path, {"base": {"api_key": CLEAR}})
    text = env_path.read_text(encoding="utf-8")
    assert "BASE_API_KEY" not in text
    assert "ADVANCED_API_KEY=sk-keep" in text


def test_save_rejects_config_that_cannot_start(tmp_path):
    """能存进去却起不来的配置，比不让存更糟。"""
    cfg_path = write_cfg(tmp_path)
    before = cfg_path.read_text(encoding="utf-8")
    with pytest.raises(ConsoleError) as exc:
        apply_changes(cfg_path, tmp_path / ".env", {"refs": {"enabled": True, "roots": []}})
    assert "refs" in str(exc.value)
    assert cfg_path.read_text(encoding="utf-8") == before  # 一个字节都没动


def test_save_rejects_bad_type(tmp_path):
    cfg_path = write_cfg(tmp_path)
    with pytest.raises(ConsoleError) as exc:
        apply_changes(cfg_path, tmp_path / ".env", {"guard": {"daily_limit": "很多"}})
    assert "整数" in str(exc.value)


def test_save_endpoint_roundtrip(tmp_path):
    app, cfg_path, console = make_app(tmp_path)
    status, body = call(
        app,
        "post",
        "/console/api/save",
        {"values": {"guard": {"daily_limit": 7}}},
        token=console.token,
    )
    assert status == 200, body
    assert "guard.daily_limit" in body["data"]["changed"]
    assert "daily_limit: 7" in cfg_path.read_text(encoding="utf-8")


def test_reload_sees_the_new_values(tmp_path):
    """存完之后 load_config 必须读到新值，否则等于没存。"""
    cfg_path = write_cfg(tmp_path)
    apply_changes(cfg_path, tmp_path / ".env", {"guard": {"daily_limit": 8}})
    assert load_config(cfg_path).guard.daily_limit == 8


# ---------------------------------------------------------------- 安全


def test_save_rejects_foreign_origin(tmp_path):
    """跨站表单 POST 是真实威胁：随便打开一个网页就能改你的配置。"""
    app, cfg_path, console = make_app(tmp_path)
    status, body = call(
        app,
        "post",
        "/console/api/save",
        {"values": {"guard": {"daily_limit": 999}}},
        token=console.token,
        headers={"origin": "https://evil.example"},
    )
    assert status == 403
    assert "来源" in body["error"]
    assert "daily_limit: 999" not in cfg_path.read_text(encoding="utf-8")


def test_save_requires_cookie(tmp_path):
    app, _, _ = make_app(tmp_path)
    status, _ = call(app, "post", "/console/api/save", {"values": {"guard": {"daily_limit": 999}}})
    assert status == 401


def test_state_never_returns_secrets(tmp_path):
    # .env 要在建 app 之前写：配置是在启动时读进内存的
    (tmp_path / ".env").write_text("BASE_API_KEY=sk-very-secret\n", encoding="utf-8")
    app, _, console = make_app(tmp_path)
    status, body = call(app, "get", "/console/api/state", token=console.token)
    assert status == 200
    raw = json.dumps(body, ensure_ascii=False)
    assert "sk-very-secret" not in raw
    assert body["data"]["slots"]["base"]["has_key"] is True


def test_console_does_not_install_without_config_path(tmp_path):
    """没有配置文件可写时不该开这个入口。"""
    cfg = load_config(write_cfg(tmp_path))
    app = create_app(cfg)
    assert not hasattr(app.state, "console")


# ---------------------------------------------------------------- key 管理


def test_keys_list_new_revoke(tmp_path):
    app, _, console = make_app(tmp_path)

    status, body = call(app, "post", "/console/api/keys/new", {"label": "laptop"}, token=console.token)
    assert status == 200
    token = body["data"]["token"]
    key_id = body["data"]["id"]
    assert token.startswith("st-")

    status, body = call(app, "get", "/console/api/keys", token=console.token)
    assert status == 200
    labels = [k["label"] for k in body["data"]]
    assert "laptop" in labels

    status, body = call(app, "post", "/console/api/keys/revoke", {"id": key_id}, token=console.token)
    assert status == 200 and body["ok"]

    store = KeyStore(tmp_path / "state" / "keys.json")
    assert store.verify(token) is None, "吊销后还能用，等于没吊销"


def test_keys_cannot_be_issued_by_foreign_origin(tmp_path):
    app, _, console = make_app(tmp_path)
    status, _ = call(
        app,
        "post",
        "/console/api/keys/new",
        {"label": "x"},
        token=console.token,
        headers={"origin": "https://evil.example"},
    )
    assert status == 403


# ---------------------------------------------------------------- 探测


def test_test_endpoint_reports_unreachable(tmp_path):
    """填错地址时要说"连不上"，而不是抛 500。"""
    app, _, console = make_app(tmp_path)
    status, body = call(
        app,
        "post",
        "/console/api/test",
        {"slot": "base", "values": {"base_url": "http://127.0.0.1:9/v1", "model": "m"}},
        token=console.token,
    )
    assert status == 200
    assert body["data"]["reachable"] is False


def test_test_endpoint_rejects_unknown_slot(tmp_path):
    app, _, console = make_app(tmp_path)
    status, body = call(
        app, "post", "/console/api/test", {"slot": "nope", "values": {}}, token=console.token
    )
    assert status == 400
    assert "槽位" in body["error"]


def test_has_key_reflects_freshly_saved_key(tmp_path):
    """刚存进去的 key 还没重启，界面也必须显示「已设置」，否则用户会存第二遍。"""
    app, _, console = make_app(tmp_path)
    call(app, "post", "/console/api/save", {"values": {"base": {"api_key": "sk-now"}}}, token=console.token)
    status, body = call(app, "get", "/console/api/state", token=console.token)
    assert status == 200
    assert body["data"]["slots"]["base"]["has_key"] is True
    assert "sk-now" not in json.dumps(body, ensure_ascii=False)


def test_state_reads_from_disk_not_memory(tmp_path):
    """存完立刻读回来要看到新值：界面回显旧值会让人重复保存。"""
    app, cfg_path, console = make_app(tmp_path)
    call(
        app,
        "post",
        "/console/api/save",
        {"values": {"advanced": {"base_url": "https://api.example.com/v1", "model": "m"}}},
        token=console.token,
    )
    status, body = call(app, "get", "/console/api/state", token=console.token)
    assert status == 200
    assert body["data"]["slots"]["advanced"]["base_url"] == "https://api.example.com/v1"
    assert body["data"]["slots"]["advanced"]["configured"] is True


# ---------------------------------------------------------------- 认证方式
#
# 这些用例的由来：预览面板用的是沙箱 iframe，Origin 是 null，且存不了 cookie。
# 原实现只认 cookie + 严格同源，结果在那种环境里登录能成功、之后每个请求 401。
# 所以认证改成「请求头带口令」，来源判定放宽到同源 / 本机 / 不透明来源。


def test_login_works_from_sandboxed_iframe(tmp_path):
    """沙箱 iframe 的 Origin 是 null，不能把它当跨站拒绝。"""
    app, _, console = make_app(tmp_path)
    status, body = call(app, "post", "/console/api/login", {"token": console.token}, headers={"origin": "null"})
    assert status == 200, body


def test_login_works_through_local_proxy(tmp_path):
    """经本地代理访问时 Origin 的主机名和 Host 头对不上，也得放行。"""
    app, _, console = make_app(tmp_path)
    status, body = call(
        app,
        "post",
        "/console/api/login",
        {"token": console.token},
        headers={"origin": "http://localhost:27418", "host": "127.0.0.1:27418"},
    )
    assert status == 200, body


def test_header_token_authenticates_without_cookie(tmp_path):
    """请求头带口令就能访问，不依赖 cookie —— 沙箱里没有 cookie。"""
    app, _, console = make_app(tmp_path)
    status, body = call(app, "get", "/console/api/state", token=console.token, use_header=True)
    assert status == 200
    assert body["ok"]


def test_header_token_can_write(tmp_path):
    app, cfg_path, console = make_app(tmp_path)
    status, body = call(
        app,
        "post",
        "/console/api/save",
        {"values": {"guard": {"daily_limit": 6}}},
        token=console.token,
        use_header=True,
    )
    assert status == 200, body
    assert "daily_limit: 6" in cfg_path.read_text(encoding="utf-8")


def test_wrong_header_token_is_rejected(tmp_path):
    app, _, _ = make_app(tmp_path)
    status, body = call(app, "get", "/console/api/state", token="wrong-token", use_header=True)
    assert status == 401


def test_foreign_origin_is_rejected_even_with_valid_token(tmp_path):
    """外站即使知道口令也发不动：Origin 对不上，且自定义头跨域要过预检。"""
    app, _, console = make_app(tmp_path)
    status, body = call(
        app,
        "post",
        "/console/api/save",
        {"values": {"guard": {"daily_limit": 999}}},
        token=console.token,
        use_header=True,
        headers={"origin": "https://evil.example"},
    )
    assert status == 403
    assert "来源" in body["error"]


# ---------------------------------------------------------------- 超时与槽位提示


def test_timeout_is_editable_from_the_console(tmp_path):
    """超时必须能在界面上改 —— 用户最怕的就是「不知道会不会超时」。"""
    app, cfg_path, console = make_app(tmp_path)
    status, body = call(
        app,
        "post",
        "/console/api/save",
        {"values": {"advanced": {"timeout_s": 300}}},
        token=console.token,
        use_header=True,
    )
    assert status == 200, body
    assert "timeout_s: 300" in cfg_path.read_text(encoding="utf-8")


def test_state_reports_timeouts(tmp_path):
    app, _, console = make_app(tmp_path)
    status, body = call(app, "get", "/console/api/state", token=console.token, use_header=True)
    assert status == 200
    assert body["data"]["slots"]["advanced"]["timeout_s"] > 0


def test_identical_slots_are_flagged(tmp_path):
    """两个槽位填成一样，多半是复制粘贴 —— 得说一声。"""
    from steward.config import config_from_dict

    cfg = config_from_dict({
        "base": {"base_url": "http://x/v1", "model": "m"},
        "advanced": {"base_url": "http://x/v1", "model": "m"},
    })
    assert any("同一个模型" in w for w in cfg.warnings())


def test_short_timeout_is_flagged(tmp_path):
    from steward.config import config_from_dict

    cfg = config_from_dict({
        "base": {"base_url": "http://x/v1", "model": "m", "timeout_s": 20},
        "advanced": {"base_url": "http://y/v1", "model": "n"},
    })
    assert any("timeout_s" in w for w in cfg.warnings())


def test_different_slots_are_not_flagged(tmp_path):
    from steward.config import config_from_dict

    cfg = config_from_dict({
        "base": {"base_url": "http://x/v1", "model": "m"},
        "advanced": {"base_url": "http://y/v1", "model": "n"},
    })
    assert not any("同一个模型" in w for w in cfg.warnings())


# ---------------------------------------------------------------- 一键重启


def test_restart_without_uvicorn_refuses(tmp_path):
    """测试环境里没有 uvicorn server 对象，要明确说不能重启，而不是假装成功。"""
    app, _, console = make_app(tmp_path)
    status, body = call(app, "post", "/console/api/restart", {}, token=console.token, use_header=True)
    assert status == 409
    assert "重启" in body["error"]


def test_restart_sets_the_flag(tmp_path):
    """重启请求要真的把标志置上，serve 循环才知道要重来一遍。"""
    from steward.server import RESTART, take_restart

    class FakeServer:
        should_exit = False

    app, _, console = make_app(tmp_path)
    app.state.uvicorn_server = FakeServer()
    RESTART["want"] = False
    status, body = call(app, "post", "/console/api/restart", {}, token=console.token, use_header=True)
    assert status == 200
    assert body["data"]["restarting"] is True
    assert RESTART["want"] is True
    assert take_restart() is True
    assert take_restart() is False  # 取一次就清掉


def test_restart_requires_login(tmp_path):
    app, _, _ = make_app(tmp_path)
    status, _ = call(app, "post", "/console/api/restart", {})
    assert status == 401


def test_restart_rejects_foreign_origin(tmp_path):
    app, _, console = make_app(tmp_path)
    status, _ = call(
        app,
        "post",
        "/console/api/restart",
        {},
        token=console.token,
        use_header=True,
        headers={"origin": "https://evil.example"},
    )
    assert status == 403


def test_env_overrides_are_cleared_before_reload():
    """重启时改了 .env 要能读到新值。

    `load_env_file` 的规则是「已存在的环境变量优先」，所以第一次启动读进
    os.environ 的旧值会一直压着 .env。
    """
    import os

    from steward.__main__ import _clear_env_overrides

    os.environ["BASE_API_KEY"] = "old"
    os.environ["STEWARD_PORT"] = "1234"
    os.environ["SOMETHING_ELSE"] = "keep"
    _clear_env_overrides()
    assert "BASE_API_KEY" not in os.environ
    assert "STEWARD_PORT" not in os.environ
    assert os.environ.get("SOMETHING_ELSE") == "keep"
