# SPDX-License-Identifier: Apache-2.0
"""python -m steward [serve|init|test|key|ctx]

serve  启动网关（默认）
init   配两个模型槽位，签发项目自己的 key
test   验两个槽位的连通性（探模型列表免费；进阶槽位默认不发请求）
key    签发 / 查看 / 吊销项目 key
ctx    看本地模型的真实窗口和输入预算（不产生推理开销）
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import uvicorn

from . import __version__
from .envfile import write_env
from .config import (
    COMMON_PORTS,
    DEFAULT_PORT,
    EndpointCfg,
    find_config,
    load_config,
    looks_sequential,
)
from .artifacts import Store
from .context import Estimator, Measurer, Prober
from .keys import KeyStore
from .refs import DEFAULT_DENY, Resolver
from .responses import chat_to_responses, responses_to_chat
from .server import create_app, take_restart
from .upstream import probe

COMMANDS = {"serve", "init", "test", "key", "ctx", "refs", "artifacts"}


# ---------------------------------------------------------------- 工具


def _ask(label: str, default: str = "", secret: bool = False) -> str:
    hint = f" [{default}]" if default else ""
    try:
        # 非 TTY（管道、CI）时 getpass 会挂住，退回普通读取
        if secret and sys.stdin.isatty():
            value = getpass.getpass(f"{label}{hint}: ")
        else:
            value = input(f"{label}{hint}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)
    return value.strip() or default


def _ask_int(label: str, default: int) -> int:
    raw = _ask(label, str(default))
    try:
        return int(raw)
    except ValueError:
        print(f"  不是整数，用默认值 {default}")
        return default


def port_hint(port: int) -> str:
    """选端口的建议。返回空串表示这个端口没什么好说的。

    端口冲突是最难查的一类故障：报错只有一句“address in use”，
    不告诉你是谁占的，也不告诉你当初为什么选它。所以这里把理由说全，
    而且允许同时命中多条（80 既常见、又需要 root）。
    """
    reasons: list[str] = []
    if port in COMMON_PORTS:
        reasons.append(f"{port} 是常见端口，本机上很容易被别的服务先占")
    if looks_sequential(port):
        reasons.append(f"{port} 是连续数字，撞车时不好辨认是谁占的")
    if port < 1024:
        reasons.append(f"{port} 低于 1024，非 root 用户通常绑不上")
    elif port >= 49152:
        reasons.append(f"{port} 落在 Windows 临时端口段（49152+），可能被随机连接临时占走")
    if not reasons:
        return ""
    return "；".join(reasons) + f"；建议 {DEFAULT_PORT}"


def _port_owner(host: str, port: int) -> str:
    """端口被占时返回占用者描述，空闲时返回空串。

    试绑用的是临时 socket，和 uvicorn 用的是同一套语义：能绑上就是空的。
    Windows 上不能靠 SO_REUSEADDR 判断占用（它会让孩子抢同一端口也成功），
    所以这里用裸 bind 试一次。

    注意「占用了」和「查出是谁」是两件事：查不到占用者必须仍然算占用，
    返回空串会被调用方当成端口空闲，那就白查了。
    """
    bind_host = "0.0.0.0" if host in ("0.0.0.0", "::") else host
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((bind_host, port))
    except OSError:
        return _describe_holder(port) or "（占用者未知）"
    finally:
        probe.close()
    return ""


def _describe_holder(port: int) -> str:
    """尽力找出是谁占着这个端口。查不到返回空串，不影响「已被占用」的结论。"""
    try:
        if os.name == "nt":
            raw = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=6).stdout
            # 中文 Windows 的 netstat 输出是 GBK，直接按 utf-8 解会抛异常，
            # 异常发生在 reader 线程里，看起来像"命令没输出"。
            out = raw.decode("mbcs", "replace")
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                    return f'（PID {parts[4]}，tasklist /FI "PID eq {parts[4]}" 可查进程名）'
        else:
            raw = subprocess.run(["ss", "-ltnp"], capture_output=True, timeout=6).stdout
            for line in raw.decode("utf-8", "replace").splitlines():
                if f":{port} " in line and "pid=" in line:
                    return f"（PID {line.split('pid=')[1].split(',')[0]}）"
    except Exception:  # noqa: BLE001 - 查占用者只是锦上添花，查不到不该影响报错
        return ""
    return ""


def _ping(ep: EndpointCfg, timeout: float = 60.0) -> tuple[bool, str, int]:
    """发一个极短请求验连通性。基础槽位不花钱，进阶槽位只花极少的钱。"""
    base = ep.base_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    headers.update({k: v for k, v in (ep.extra_headers or {}).items() if v})

    payload = {
        "model": ep.model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 16,
        "stream": False,
    }
    if ep.protocol == "responses":
        url, body = f"{base}/responses", chat_to_responses(payload)
    else:
        url, body = f"{base}/chat/completions", payload

    started = time.perf_counter()
    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}", int((time.perf_counter() - started) * 1000)

    ms = int((time.perf_counter() - started) * 1000)
    if resp.status_code >= 400:
        detail = resp.text[:200].replace("\n", " ")
        if resp.status_code in (401, 403):
            detail = f"HTTP {resp.status_code} 上游拒绝了这把 key: {detail}"
        else:
            detail = f"HTTP {resp.status_code}: {detail}"
        return False, detail, ms
    try:
        data = resp.json()
    except ValueError:
        return False, "返回不是 JSON", ms

    if ep.protocol == "responses":
        try:
            data = responses_to_chat(data, ep.model)
        except ValueError as exc:
            return False, str(exc), ms
    if not (data.get("choices") or []):
        return False, "返回里没有 choices", ms
    return True, "ok", ms


def _detect_protocol(ep: EndpointCfg) -> tuple[str, str]:
    """先试 chat 再试 responses，选能通的那个。"""
    detail = ""
    for proto in ("chat", "responses"):
        trial = EndpointCfg(
            base_url=ep.base_url,
            api_key=ep.api_key,
            model=ep.model,
            protocol=proto,
            timeout_s=60,
            extra_headers=dict(ep.extra_headers),
        )
        ok, detail, ms = _ping(trial)
        if ok:
            return proto, f"{proto} 协议可用（{ms}ms）"
    return "chat", f"两种协议都没通：{detail}"


def _lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


CONFIG_TEMPLATE = """\
# steward 配置（由 init 生成）

server:
  host: {host}
  port: {port}
  api_key: ""              # 留空。下游鉴权用 auth 签发的项目 key
  log_level: info

auth:
  mode: required           # required=必须带项目 key；off=关闭鉴权（仅本机调试）
  keys_file: state/keys.json
  auto_issue: true         # 没有任何 key 时，启动自动签发一把并打印一次

# 槽位一：基础模型
# 日常主力，绝大多数请求由它完成。通常填本地部署的模型
# （llama.cpp / LM Studio / vLLM / Ollama 都行，只要 OpenAI 兼容）。
base:
  base_url: {base_url}
  api_key: ${{BASE_API_KEY}}      # 上游访问令牌。可留空，但字段要保留：
                                  # 局域网里给多台设备用时，这里往往是唯一的门外锁
  model: {base_model}
  protocol: {base_protocol}       # chat | responses（前者通用，后者是 OpenAI Responses 协议）
  thinking: "off"                 # 推理型模型建议 off：路由判断不需要思维链，开着会烧光预算
  timeout_s: 600
  min_max_tokens: 1024            # 客户端 max_tokens 小于此值时抬上去

# 槽位二：进阶模型
# 只在复杂任务时被调用，按次计费，受下方配额硬限制。通常填云端 API。
advanced:
  base_url: {adv_url}
  api_key: ${{ADVANCED_API_KEY}}
  model: {adv_model}
  protocol: chat
  timeout_s: 120
  max_tokens: 4096

guard:
  max_calls_per_turn: 2    # 单次请求内最多问进阶模型几次
  max_tool_rounds: 4       # 基础模型最多循环几轮，超过强制收尾
  min_interval_s: 30       # 两次进阶调用最小间隔（秒）
  daily_limit: {daily_limit:<2}       # 每日总次数，-1 不限
  dedupe: true
  similarity: 0.92
  max_brief_chars: 6000
  max_result_chars: 16000
  state_file: state/usage.json

# 上下文治理：本地窗口远小于云端，网关负责记账与裁剪。
# 两个窗口差 4–16 倍，不记账的话长任务必然在本地这一层爆掉。
context:
  enabled: true
  ctx_limit: 0             # 0=自动探测；探不到时会保守按 32K 处理
  usable_ratio: 0.85
  target_ratio: 0.65
  min_clear_tokens: 8000
  reserve_output: true
  keep_recent: 4
  overflow_retry: true
  state_file: state/ctx.json
  spine: true               # 裁完把丢掉的那片提炼成六行，避免重试已否决的方案
  spine_min_dropped: 4000   # 丢得少就不值得为它调模型
  spine_state_file: state/spine.json

# 交付物外置：长方案落盘，上下文只留摘要 + 路径
artifacts:
  enabled: true
  dir: .steward/artifacts
  inline_chars: 800
  min_store_chars: 1500
  max_age_days: 30          # 按目录名里的日期清；0 = 不按时间清
  max_total_mb: 512         # 超了从最旧的日目录开始删；0 = 不按容量清
  index_max_lines: 5000

public_model: auto         # 客户端看到的模型名，网关不按它路由
model_aliases: [local, steward]

prompt:
  system_text: ""          # 留空用内置规则
  tool_description: ""
  force_finish_text: ""
"""


# ---------------------------------------------------------------- 命令


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port

    logging.basicConfig(
        level=getattr(logging, cfg.server.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    problems = cfg.validate()
    if problems:
        for item in problems:
            print(f"[配置] 错误  {item}", file=sys.stderr)
        print("[配置] 以上必须修掉才能启动：基础模型缺失时网关没有任何可用模型。", file=sys.stderr)
        return 1

    # 空槽位、冲突端口这类只提示不拦：它们不影响「能不能跑」，只影响「好不好用」。
    for item in cfg.warnings():
        print(f"[配置] 注意  {item}", file=sys.stderr)

    if args.check:
        store = KeyStore(cfg.auth.keys_file)
        adv = (
            f"{cfg.advanced.base_url} model={cfg.advanced.model} protocol={cfg.advanced.protocol}"
            if cfg.advanced.base_url and cfg.advanced.model
            else "空槽位（不注入 consult_cloud）"
        )
        print(f"[配置] OK  基础={cfg.base.base_url} model={cfg.base.model} protocol={cfg.base.protocol}")
        print(f"[配置]     进阶={adv}")
        print(f"[配置]     监听={cfg.server.host}:{cfg.server.port}")
        print(f"[配置]     鉴权={cfg.auth.mode}  有效 key={store.summary()['active']}")
        print(f"[配置]     单请求上限={cfg.guard.max_calls_per_turn} 每日={cfg.guard.daily_limit} 间隔={cfg.guard.min_interval_s}s")
        return 0

    # 控制台要写回配置文件，所以得知道它在哪。load_config 里已经解析过一次，
    # 这里按同样的规则再算一遍，避免两处口径不同。
    cfg_path = Path(args.config).expanduser().resolve() if args.config else find_config()
    return asyncio.run(_serve_loop(cfg_path, args))


def _clear_env_overrides() -> None:
    """清掉会被配置覆盖的环境变量。

    `load_env_file` 的规则是「已存在的环境变量优先」，所以第一次启动读进
    os.environ 的旧值会一直压着 .env —— 重启时改了 .env 也不会生效。
    重启前把这几类变量清掉，才能读到新的。
    """
    prefixes = ("STEWARD_", "BASE_", "ADVANCED_", "LOCAL_", "CLOUD_")
    for name in [k for k in os.environ if k.startswith(prefixes)]:
        os.environ.pop(name, None)


def _banner(cfg, app) -> None:
    """启动横幅。flush=True 是必须的：

    重定向到日志文件时 stdout 是全缓冲的，横幅要等进程退出才出现，
    而口令只显示这一次，晚出现的口令等于没有。
    """
    host = cfg.server.host
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"\n  端点 http://{shown}:{cfg.server.port}/v1", flush=True)
    if host in ("0.0.0.0", "::"):
        for ip in _lan_ips():
            print(f"  局域网 http://{ip}:{cfg.server.port}/v1", flush=True)
        print("  每台设备单独签发：python -m steward key new --label 设备名", flush=True)
    print(f"  key  {cfg.auth.keys_file}", flush=True)
    console = getattr(app.state, "console", None)
    if console is not None:
        print(f"\n  控制台 http://{shown}:{cfg.server.port}/  （填槽位、改配额、签发 key）", flush=True)
        print(f"  口令   {console.token}", flush=True)
        # Windows 上 chmod 0600 是空操作，别在这里承诺一个不成立的权限
        perm = "权限 0600" if os.name != "nt" else "Windows 上靠目录 ACL，chmod 不生效"
        print(f"        也存在 {console.path}（{perm}，别提交到仓库）", flush=True)
    print(flush=True)


async def _serve_loop(cfg_path: Path, args: argparse.Namespace) -> int:
    """起服务；控制台请求重启时，重新读配置再起一次。

    **同进程重启**，不派生新进程：uvicorn 退出时就把端口放了，新一轮直接
    绑上去，不会和「还没退干净的老进程」抢同一个 socket。日志也还在用户
    原来那个终端里 —— 派生进程的话日志会跑到没人看的地方去。
    """
    first = True
    while True:
        if not first:
            _clear_env_overrides()
        cfg = load_config(cfg_path)
        host, port = cfg.server.host, cfg.server.port
        shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host

        # 先自己试绑一次。uvicorn 撞端口时抛的是 WinError 10048 / EADDRINUSE，
        # 混在启动日志里一闪而过，用户只看到「shutdown complete」，
        # 会以为是配置写错了。端口冲突要一句话说清是谁的问题。
        taken = _port_owner(host, port)
        if taken:
            print(f"[端口] {shown}:{port} 已被占用{taken}。", file=sys.stderr)
            print(
                f"[端口] 停掉它，或用 --port 换一个（默认 {DEFAULT_PORT}，也可以改 config.yaml 的 server.port）。",
                file=sys.stderr,
            )
            return 1

        app = create_app(cfg, config_path=cfg_path)
        _banner(cfg, app)

        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level=cfg.server.log_level)
        )
        # 控制台要拿到这个对象才能触发优雅退出
        app.state.uvicorn_server = server

        await server.serve()

        if not take_restart():
            return 0
        first = False
        print("\n[重启] 重新读取配置…", flush=True)


def cmd_init(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).expanduser().resolve() if args.config else Path.cwd() / "config.yaml"
    if cfg_path.exists() and not args.force:
        print(f"[init] {cfg_path} 已存在。要覆盖加 --force。", file=sys.stderr)
        return 1

    print("\nsteward 初始化：先配两个模型槽位，再签发一把项目 key\n")
    print("  基础模型   日常主力，绝大多数请求由它完成。通常填本地部署的模型")
    print("  进阶模型   只在复杂任务时调用，按次计费，受每日配额硬限制\n")

    if args.no_input:
        values = {
            "base_url": args.base_url,
            "base_model": args.base_model,
            "base_key": args.base_key,
            "adv_url": args.adv_url,
            "adv_model": args.adv_model,
            "adv_key": args.adv_key,
        }
        port = args.port or DEFAULT_PORT
        hint = port_hint(port)
        if hint:
            print(f"[端口] {hint}")
        daily_limit = args.daily_limit if args.daily_limit is not None else 5
        protocol = args.base_protocol or "chat"
    else:
        port = _ask_int("监听端口", args.port or DEFAULT_PORT)
        hint = port_hint(port)
        if hint:
            print(f"  注意：{hint}")
        print("\n-- 基础模型 --")
        values = {
            "base_url": _ask("HTTP 地址", args.base_url or "http://127.0.0.1:11434/v1"),
            "base_model": _ask("模型标识（llama.cpp 一般填 GGUF 路径）", args.base_model),
            "base_key": _ask(
                "访问令牌（上游不校验就留空；llama.cpp --api-key / LM Studio / vLLM 都要填）",
                args.base_key or os.environ.get("BASE_API_KEY", ""),
                secret=True,
            ),
        }
        print("\n-- 进阶模型 --")
        print("   使用次数受限，按次计费。云端 API 就填这里。")
        values.update(
            {
                "adv_url": _ask("HTTP 地址", args.adv_url),
                "adv_model": _ask("模型标识", args.adv_model),
                "adv_key": _ask("API key", args.adv_key, secret=True),
            }
        )
        daily_limit = _ask_int(
            "每日最多调用次数", args.daily_limit if args.daily_limit is not None else 5
        )

        print("\n正在探测基础模型协议 ...")
        trial = EndpointCfg(
            base_url=values["base_url"] or "http://127.0.0.1:11434/v1",
            model=values["base_model"] or "your-base-model",
            api_key=values["base_key"],
        )
        protocol, note = _detect_protocol(trial)
        print(f"  {note}\n")

    host = args.host or "127.0.0.1"

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        CONFIG_TEMPLATE.format(
            host=host,
            port=port,
            base_url=values["base_url"] or "http://127.0.0.1:11434/v1",
            base_model=values["base_model"] or "your-base-model",
            base_protocol=protocol,
            adv_url=values["adv_url"] or "https://your-provider.example.com/v1",
            adv_model=values["adv_model"] or "your-advanced-model",
            daily_limit=daily_limit,
        ),
        encoding="utf-8",
    )

    env_path = cfg_path.parent / ".env"
    if values["base_key"] or values["adv_key"]:
        write_env(
            env_path,
            {"BASE_API_KEY": values["base_key"], "ADVANCED_API_KEY": values["adv_key"]},
        )

    store = KeyStore((cfg_path.parent / "state" / "keys.json").resolve())
    token, _ = store.issue(args.key_label or "default")

    print(f"[1/3] 配置   {cfg_path}")
    print(f"[2/3] 凭据   {env_path if values['base_key'] or values['adv_key'] else '（未填 API key，跳过）'}")
    print("[3/3] 项目 key（只显示这一次）\n")
    print(f"      {token}\n")
    print("智能体里这样填：")
    print(f"  base_url = http://127.0.0.1:{port}/v1")
    print(f"  api_key  = {token}\n")

    if not args.no_input:
        print("正在测基础模型连通性 ...")
        ok, detail, ms = _ping(
            EndpointCfg(
                base_url=values["base_url"] or "http://127.0.0.1:11434/v1",
                model=values["base_model"] or "your-base-model",
                api_key=values["base_key"],
                protocol=protocol,
            )
        )
        if ok:
            print(f"  基础模型 OK（{ms}ms）\n")
        else:
            print(f"  基础模型未连通：{detail}")
            print("  先确认本地模型服务在跑，再改 config.yaml。\n")

    print("启动：python -m steward serve")
    print("自检：python -m steward test\n")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    # 基础槽位必需，进阶槽位可选：空着就是「纯本地模式」，不是配置错误。
    rows = [("基础模型", cfg.base, True, True), ("进阶模型", cfg.advanced, bool(args.all), False)]

    failed = 0
    for name, ep, enabled, required in rows:
        if not ep.base_url or not ep.model:
            if required:
                print(f"{name}   未配置")
                failed += 1
            else:
                print(f"{name}   空槽位（可选）：consult_cloud 不注入，纯本地运行")
            continue

        info = probe(ep)
        if enabled:
            ok, detail, ms = _ping(ep)
            mark = "OK" if ok else "失败"
            print(f"{name}   {ep.model}")
            print(f"{'':<10} 调用 {mark} {ms}ms  {'' if ok else detail}")
            if not ok:
                failed += 1
        else:
            print(f"{name}   {ep.model}")
            print(f"{'':<10} 调用 跳过（探模型列表不花钱；加 --all 才真发请求）")

        if info["ok"]:
            bits = [f"protocol={ep.protocol}"]
            if info["owned_by"]:
                bits.append(f"上游={info['owned_by']}")
            if info["context"]:
                bits.append(f"上下文={info['context']}")
            if info["slots"]:
                bits.append(f"槽位={info['slots']}")
            bits.append(f"模型数={len(info['models'])}")
            print(f"{'':<10} 探测 {' · '.join(bits)}")
            if info["models"] and ep.model not in info["models"]:
                print(f"{'':<10} 注意 {ep.model} 不在模型列表里（llama.cpp 通常仍能用）")
        else:
            status = info.get("status")
            if status in (401, 403):
                print(f"{'':<10} 探测 上游拒绝了 key，检查 api_key")
            elif info.get("detail"):
                print(f"{'':<10} 探测 {info['detail']}")
            if enabled:
                failed += 1

    return 0 if failed == 0 else 1


def cmd_key(args: argparse.Namespace) -> int:
    if not getattr(args, "key_cmd", None):
        print("用法：python -m steward key {new|list|revoke}", file=sys.stderr)
        return 2

    cfg = load_config(getattr(args, "config", None))
    store = KeyStore(cfg.auth.keys_file)

    if args.key_cmd == "new":
        token, rec = store.issue(args.label or "default")
        print(f"id={rec.id} label={rec.label}")
        print(token)
        return 0

    if args.key_cmd == "list":
        items = store.all()
        if not items:
            print(f"没有 key（{cfg.auth.keys_file}）。用 key new 签发一把。")
            return 0
        print(f"{'ID':<10}{'标签':<20}{'状态':<8}{'调用':<8}最后使用")
        for k in items:
            state = "有效" if k.enabled else "已吊销"
            print(f"{k.id:<10}{k.label[:18]:<20}{state:<8}{k.calls:<8}{k.last_used_at or '-'}")
        return 0

    if args.key_cmd == "revoke":
        ok = store.revoke(args.id)
        print("已吊销" if ok else "没找到该 id，或已经是吊销状态")
        return 0 if ok else 1

    print("用法：python -m steward key {new|list|revoke}", file=sys.stderr)
    return 2


def cmd_ctx(args: argparse.Namespace) -> int:
    """看本地模型的真实窗口、输入预算，以及计数方式。纯探测，不产生推理。"""
    cfg = load_config(args.config)
    ep = cfg.base
    if not ep.base_url:
        print("base.base_url 未配置", file=sys.stderr)
        return 1

    estimator = Estimator(state_path=cfg.context.state_file)
    limits = asyncio.run(Prober(cfg.context, ep).probe())
    limits.finalize(cfg.context, cfg.base.max_tokens, estimator.budget_scale)

    print(f"\n基础模型  {ep.model}")
    print(f"  地址        {ep.base_url}")
    print(f"  真实窗口    {limits.per_slot} tokens（来源 {limits.source}，槽位 {limits.slots}）")
    if limits.n_ctx_train:
        print(f"  训练长度    {limits.n_ctx_train}")
    print(f"  可用窗口    {limits.usable}（usable_ratio {cfg.context.usable_ratio}）")
    print(f"  输入预算    {limits.budget}（已扣输出预留 {cfg.base.max_tokens} 与安全余量）")
    print(f"  裁剪目标    {int(limits.budget * cfg.context.target_ratio)}（target_ratio {cfg.context.target_ratio}）")

    async def check_tokenize() -> bool:
        measurer = Measurer(ep.base_url, ep.api_key, ep.extra_headers)
        try:
            return await measurer.probe()
        finally:
            await measurer.aclose()

    exact = asyncio.run(check_tokenize())
    print(f"  计数方式    {'/tokenize 精确计数' if exact else '字符类估算 + 自校准'}")
    print(f"  估算系数    {estimator.factor:.3f}   预算收缩 {estimator.budget_scale:.2f}")

    if limits.source in ("default", "config") and not cfg.context.ctx_limit:
        print(
            "\n  注意：没能探到上游真实窗口，按保守值处理。"
            "如果本地实际更小，请写 context.ctx_limit 显式指定。"
        )
    print()
    return 0


def cmd_refs(args: argparse.Namespace) -> int:
    """逐个路径给出「会不会被发出去」的判决。白名单要能被一句话问清楚。"""
    cfg = load_config(args.config)
    resolver = Resolver(cfg.refs)
    print()
    if not cfg.refs.enabled:
        print("  refs 未启用（refs.enabled=false）。下面的判决仅供参考，实际不会发生。")
    print(f"  白名单  {len(cfg.refs.roots)} 个根目录")
    for root in cfg.refs.roots:
        print(f"          {root}")
    if cfg.refs.deny:
        print(f"  黑名单  {len(cfg.refs.deny)} 条（配置覆盖）")
    else:
        print(f"  黑名单  内置 {len(DEFAULT_DENY)} 条")

    if not args.paths:
        print("\n  用法：python -m steward refs <路径>...    加路径才做判决\n")
        return 0

    print()
    denied = 0
    for raw in args.paths:
        try:
            target = resolver._safe(raw)  # noqa: SLF001 同一个包的诊断入口，故意的
        except Exception as exc:  # _Reject 是内部异常，这里只关心给人看的话
            denied += 1
            print(f"  拒绝  {raw}")
            print(f"        {exc}")
            continue
        size = target.stat().st_size
        verdict = "允许" if size <= cfg.refs.max_file_bytes else "允许（超上限，读前缀）"
        print(f"  {verdict}  {raw}")
        print(f"        → {target}  {size:,} 字节")

    print(f"\n  合计 {len(args.paths)} 个，拒绝 {denied} 个\n")
    return 0 if denied == 0 else 1


def cmd_artifacts(args: argparse.Namespace) -> int:
    """看存档占用，按策略回收。默认只看不动，--clean 才真删。"""
    cfg = load_config(args.config)
    store = Store(cfg.artifacts)
    stats = store.stats()

    print(f"\n存档目录  {stats['root']}")
    print(f"  合计      {stats['files']} 个文件 / {stats['bytes'] / 1048576:.2f} MB")
    print(f"  策略      保留 {cfg.artifacts.max_age_days or '不限'} 天 / "
          f"上限 {cfg.artifacts.max_total_mb or '不限'} MB")

    if not stats["days"]:
        print("  还没有任何存档。\n")
        return 0

    print("\n  按天：")
    for day, info in list(stats["days"].items())[-14:]:
        label = f"{day[:4]}-{day[4:6]}-{day[6:]}"
        print(f"    {label}  {info['files']:>5} 个  {info['bytes'] / 1048576:>8.2f} MB")

    if not args.clean:
        print("\n  加 --clean 执行回收（先加 --dry-run 看会删什么）。\n")
        return 0

    result = store.maintain(force=True, dry_run=args.dry_run)
    verb = "将删除" if args.dry_run else "已删除"
    if not result["removed_days"]:
        print(f"\n  没有需要回收的内容（{verb} 0 个日目录）。\n")
        return 0

    print(f"\n  {verb} {len(result['removed_days'])} 个日目录 / "
          f"{result['removed_files']} 个文件 / {result['freed_bytes'] / 1048576:.2f} MB")
    print(f"    {', '.join(result['removed_days'])}")
    if result["index_trimmed"]:
        print(f"    index.jsonl 裁掉 {result['index_trimmed']} 行")
    if args.dry_run:
        print("    这是预演，什么都没删。")
    print()
    return 0


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="steward",
        description="本地模型管家：两个模型槽位，对外一把 key，管账 / 管窗 / 管档案",
    )
    parser.add_argument("--version", action="version", version=f"steward {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动网关")
    serve.add_argument("-c", "--config", help="配置文件路径，默认 ./config.yaml")
    serve.add_argument("--host", help="覆盖监听地址（局域网用 0.0.0.0）")
    serve.add_argument("--port", type=int, help="覆盖监听端口")
    serve.add_argument("--check", action="store_true", help="只检查配置，不启动")
    serve.set_defaults(func=cmd_serve)

    init = sub.add_parser("init", help="配两个模型槽位并签发项目 key")
    init.add_argument("-c", "--config", help="写入位置，默认 ./config.yaml")
    init.add_argument("--force", action="store_true", help="覆盖已有配置")
    init.add_argument("--host", help="监听地址，局域网用 0.0.0.0")
    init.add_argument("--port", type=int, help="监听端口")
    init.add_argument("--base-url", help="基础模型 HTTP 地址")
    init.add_argument("--base-model", help="基础模型标识")
    init.add_argument("--base-key", help="基础模型访问令牌（可留空）")
    init.add_argument("--base-protocol", choices=["chat", "responses"], help="基础模型协议，默认自动探测")
    init.add_argument("--adv-url", help="进阶模型 HTTP 地址")
    init.add_argument("--adv-model", help="进阶模型标识")
    init.add_argument("--adv-key", help="进阶模型 API key")
    init.add_argument("--daily-limit", type=int, help="进阶模型每日调用上限")
    init.add_argument("--key-label", help="项目 key 的标签")
    init.add_argument("--no-input", action="store_true", help="不交互，全用参数")
    init.set_defaults(func=cmd_init)

    test = sub.add_parser("test", help="测两个槽位的连通性")
    test.add_argument("-c", "--config", help="配置文件路径")
    test.add_argument("--all", action="store_true", help="连进阶模型一起测（会产生 1 次调用）")
    test.set_defaults(func=cmd_test)

    key = sub.add_parser("key", help="管理项目 key")
    ksub = key.add_subparsers(dest="key_cmd")

    knew = ksub.add_parser("new", help="签发一把新 key")
    knew.add_argument("-c", "--config", help="配置文件路径")
    knew.add_argument("--label", help="标签，比如 workbuddy / cline")
    knew.set_defaults(func=cmd_key)

    klist = ksub.add_parser("list", help="列出所有 key")
    klist.add_argument("-c", "--config", help="配置文件路径")
    klist.set_defaults(func=cmd_key)

    krev = ksub.add_parser("revoke", help="吊销一把 key")
    krev.add_argument("-c", "--config", help="配置文件路径")
    krev.add_argument("id")
    krev.set_defaults(func=cmd_key)

    key.set_defaults(func=cmd_key)

    ctx = sub.add_parser("ctx", help="看本地模型的真实窗口与输入预算")
    ctx.add_argument("-c", "--config", help="配置文件路径")
    ctx.set_defaults(func=cmd_ctx)

    refs = sub.add_parser("refs", help="检查某个本地路径会不会被 refs 放行")
    refs.add_argument("paths", nargs="*", help="要检查的文件路径（可多个）")
    refs.add_argument("-c", "--config", help="配置文件路径")
    refs.set_defaults(func=cmd_refs)

    art = sub.add_parser("artifacts", help="看交付物存档占用，按策略回收")
    art.add_argument("-c", "--config", help="配置文件路径")
    art.add_argument("--clean", action="store_true", help="执行回收（默认只看不动）")
    art.add_argument("--dry-run", action="store_true", help="配合 --clean，只报告不删除")
    art.set_defaults(func=cmd_artifacts)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["serve"]
    elif argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version"):
        # 不带子命令时默认 serve，但 -h / --version 是顶层参数，不能被塞进子命令里
        argv = ["serve"] + argv

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
