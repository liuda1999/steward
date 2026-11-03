# SPDX-License-Identifier: Apache-2.0
"""上游访问的网络策略。

一个容易踩、而且症状极具误导性的坑：**环境里的代理会把本机请求也劫持。**

`httpx` 默认 `trust_env=True`，也就是照着 `HTTP_PROXY` / `HTTPS_PROXY` 发请求。
如果机器上设了代理（公司网关、加速器、某些开发工具自带出网代理），那么
`http://127.0.0.1:11434` 这种本机地址也会被送给代理：

- 代理转发得动 → 看起来正常，但多了一跳，也把本地流量交给了第三方；
- 代理转发不动（端口没人听时它不立刻拒绝，而是干等） → **连接失败被伪装成超时**。

第二种最坑：你以为「模型启动慢」，其实是「模型根本没启动」。实测一个没人
听的端口，从「立刻 refused」变成「等满超时」。

所以规则很简单：

- **本机与内网地址（回环、RFC1918）直连**，不经过任何代理；
- **公网地址照常读环境变量**，企业代理与企业 CA 都不受影响。
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

#: 读环境变量里的 CA 时按这个顺序找
CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").strip().lower()
    except ValueError:
        return ""


def is_direct(host: str) -> bool:
    """这个地址应该直连吗（不经代理）？

    回环、局域网、以及常见的本地主机名都算「只能直接连」——
    代理既到不了，也不该经手。
    """
    host = (host or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"):
        return True
    # .local（mDNS）与 .localdomain（Debian/RedHat 的 mDNS 域）都在本机解析
    if host.endswith((".local", ".localhost", ".localdomain")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def client_kwargs(url: str) -> dict:
    """给 httpx 客户端的构造参数。

    直连地址：`trust_env=False` —— 不走代理、不读环境 CA。
    公网地址：`trust_env=True` —— 代理照用，企业 CA 也照用。
    """
    if is_direct(host_of(url)):
        return {"trust_env": False}
    return {"trust_env": True}


def env_ca() -> str:
    """环境里声明的 CA 包（没有就空字符串）。"""
    for name in CA_ENV_VARS:
        path = os.environ.get(name)
        if path and os.path.isfile(path):
            return path
    return ""


def mentions_unknown_param(text: str, param: str = "chat_template_kwargs") -> bool:
    """上游是不是因为「不认识这个参数」而报的错？

    `chat_template_kwargs` 是 llama.cpp / vLLM / Qwen 这类本地服务认识的
    扩展开关，严格的云端 API（OpenAI、Azure）会直接 400：

        Unrecognized request argument supplied: chat_template_kwargs

    认出这个形状，才能判断「摘掉它重发一次就行」。
    """
    t = (text or "").lower()
    if param.lower() not in t:
        return False
    return any(
        word in t
        for word in ("unrecognized", "unknown", "unsupported", "not permitted", "invalid parameter", "extra")
    )
