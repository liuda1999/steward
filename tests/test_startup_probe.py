# SPDX-License-Identifier: Apache-2.0
"""首次启动不应该去探测上游窗口。

真实反馈：第一次跑 `scripts\\start.bat`，槽位还是模板里的 `your-base-model`，
日志却冒出一句「没能探到上游真实窗口，按 32768 保守估计」——读者以为是报错。
正确的顺序是：先起服务 → 进控制台填上游和 key → 保存并重启 → 这时才探测。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from steward.config import (
    PLACEHOLDER_MODEL,
    AppCfg,
    AuthCfg,
    ContextCfg,
    EndpointCfg,
    GuardCfg,
)
from steward.server import create_app

ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path: Path, model: str) -> AppCfg:
    return AppCfg(
        auth=AuthCfg(mode="off", keys_file=str(tmp_path / "keys.json")),
        base=EndpointCfg(base_url="http://local.test/v1", model=model, timeout_s=5, max_tokens=512),
        guard=GuardCfg(
            state_file=str(tmp_path / "usage.json"),
            max_calls_per_turn=2,
            max_tool_rounds=2,
            min_interval_s=0,
            daily_limit=10,
        ),
        context=ContextCfg(
            state_file=str(tmp_path / "ctx.json"),
            spine_state_file=str(tmp_path / "spine.json"),
            spine=False,
        ),
    )


class _Recorder(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return httpx.Response(404, json={"error": "not found"})


def _requests_probe(urls: list[str]) -> bool:
    return any(("/props" in u) or ("/models" in u) or ("/tokenize" in u) for u in urls)


def test_first_run_skips_upstream_probe(tmp_path, caplog):
    """占位模型时一个请求都不该发，也不该出现「探不到窗口」的告警。"""
    rec = _Recorder()
    app = create_app(_cfg(tmp_path, PLACEHOLDER_MODEL), transport=rec)

    with caplog.at_level(logging.INFO, logger="steward"):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    assert not _requests_probe(rec.urls), f"首次启动不该探测上游，却发了 {rec.urls}"
    assert "跳过窗口探测" in caplog.text
    assert "没能探到上游真实窗口" not in caplog.text
    assert "上游没返回窗口大小" not in caplog.text


def test_configured_slot_is_probed(tmp_path, caplog):
    """填了真实模型名，就该探测；探不到时说清楚是按估计值先跑。"""
    rec = _Recorder()
    app = create_app(_cfg(tmp_path, "real-model"), transport=rec)

    with caplog.at_level(logging.INFO, logger="steward"):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200

    assert _requests_probe(rec.urls), f"配好的槽位应该探测，实际一个请求都没发：{rec.urls}"
    assert "上游没返回窗口大小" in caplog.text
    assert "跳过窗口探测" not in caplog.text


def test_banner_paths_are_relative_inside_the_project(monkeypatch):
    """横幅里的路径应该是相对的，别一上来甩一长串绝对路径。"""
    from steward.__main__ import _display_path

    monkeypatch.chdir(ROOT)
    shown = _display_path(ROOT / "state" / "keys.json")
    assert shown == str(Path("state") / "keys.json")
