# SPDX-License-Identifier: Apache-2.0
"""网络策略：本机与内网地址必须直连。

这个 bug 的症状极具误导性：环境里设了代理时，本机「没人听的端口」不再
立刻报 connection refused，而是被送去代理干等，最后报成 timeout ——
用户看到的是「模型太慢」，实际是「模型根本没启动」。
"""

from __future__ import annotations

import pytest

from steward.netpolicy import client_kwargs, host_of, is_direct


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.53",
        "0.0.0.0",
        "::1",
        "192.168.1.50",
        "10.0.0.5",
        "172.16.3.4",
        "169.254.1.1",
        "localhost",
        "my-model.local",
        "my-model.lan.localdomain",
    ],
)
def test_local_addresses_are_direct(host):
    assert is_direct(host) is True


@pytest.mark.parametrize("host", ["api.openai.com", "8.8.8.8", "example.com", "172.32.0.1"])
def test_public_addresses_may_use_proxy(host):
    """172.16–172.31 才是私网；172.32 是公网段。"""
    assert is_direct(host) is False


def test_client_kwargs_bypasses_proxy_for_local():
    assert client_kwargs("http://127.0.0.1:11434/v1")["trust_env"] is False
    assert client_kwargs("http://192.168.1.50:8080/v1")["trust_env"] is False


def test_client_kwargs_keeps_proxy_for_public():
    """公网地址照常读环境代理，企业网关与 CA 都不受影响。"""
    assert client_kwargs("https://api.openai.com/v1")["trust_env"] is True


def test_host_of():
    assert host_of("http://127.0.0.1:11434/v1") == "127.0.0.1"
    assert host_of("https://API.Example.COM/v1") == "api.example.com"
    assert host_of("not a url") == ""


def test_upstream_client_does_not_use_env_proxy(tmp_path, monkeypatch):
    """本机上游的客户端不能带 trust_env=True。"""
    import httpx

    from steward.config import EndpointCfg
    from steward.upstream import BaseClient

    client = BaseClient(EndpointCfg(base_url="http://127.0.0.1:11434/v1", model="m"))
    try:
        assert client._client.trust_env is False
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_cloud_client_does_not_use_env_proxy():
    import asyncio

    from steward.cloud import AdvancedClient
    from steward.config import EndpointCfg

    client = AdvancedClient(EndpointCfg(base_url="http://127.0.0.1:11434/v1", model="m"))
    try:
        assert client._client.trust_env is False
    finally:
        asyncio.run(client.aclose())
