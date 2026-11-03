"""拿真实本地模型跑一遍完整链路，不产生云端费用。

用法：
  python scripts/live_test.py
  python scripts/live_test.py --protocol responses
  python scripts/live_test.py --base-url http://127.0.0.1:8080/v1 \
      --model /models/your-model.gguf --key sk-xxx

进阶槽位也指向同一个本地端点，只验证"咨询 → 回灌 → 继续"这条链路。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from steward.config import AppCfg, AuthCfg, EndpointCfg, GuardCfg, ServerCfg  # noqa: E402
from steward.server import create_app  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL = "/models/your-model.gguf"

SIMPLE = "计算 37 乘 43，只回数字。"
COMPLEX = (
    "设计一个跨机房双活的订单系统：要求 RPO=0、RTO 小于 30 秒。"
    "说明脑裂怎么处理、数据一致性怎么保证、以及分阶段落地步骤。"
)


def build_cfg(args) -> AppCfg:
    tmp = Path(tempfile.mkdtemp(prefix="st-live-"))
    slot = dict(
        base_url=args.base_url,
        model=args.model,
        api_key=args.key,
        protocol=args.protocol,
        timeout_s=600,
        extra_headers={},
    )
    return AppCfg(
        server=ServerCfg(host="127.0.0.1", port=args.port, log_level="warning"),
        auth=AuthCfg(mode="required", keys_file=str(tmp / "keys.json")),
        base=EndpointCfg(min_max_tokens=1024, thinking=args.thinking, **slot),
        # 测试里进阶槽位也指向同一个本地模型，同样关掉思考，免得 700 token 全花在思维链上
        advanced=EndpointCfg(max_tokens=700, thinking="off", **slot),
        guard=GuardCfg(
            max_calls_per_turn=2,
            max_tool_rounds=4,
            min_interval_s=0,
            daily_limit=5,
            state_file=str(tmp / "usage.json"),
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--key", default="")
    parser.add_argument("--protocol", choices=["chat", "responses"], default="chat")
    parser.add_argument("--thinking", choices=["auto", "off", "on"], default="off")
    parser.add_argument("--port", type=int, default=27420)
    args = parser.parse_args()

    cfg = build_cfg(args)
    app = create_app(cfg)
    token = app.state.bootstrap_key
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    root = f"http://127.0.0.1:{args.port}"
    for _ in range(100):
        try:
            if httpx.get(f"{root}/health", timeout=2.0).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        print("FAIL 网关没起来")
        return 1

    auth = {"Authorization": f"Bearer {token}"}
    results: list[tuple[str, bool, str]] = []

    print(f"\n上游 {args.base_url}  协议={args.protocol}")
    print(f"模型 {args.model}\n")

    anon = httpx.post(
        f"{root}/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        timeout=10,
    )
    results.append(("无 key 应被拒", anon.status_code == 401, f"HTTP {anon.status_code}"))

    for label, prompt, expect_cloud in (
        ("简单任务本地闭环", SIMPLE, False),
        ("复杂任务触发外援", COMPLEX, True),
    ):
        started = time.perf_counter()
        try:
            resp = httpx.post(
                f"{root}/v1/chat/completions",
                headers=auth,
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1500,
                },
                timeout=600,
            )
        except httpx.HTTPError as exc:
            results.append((label, False, f"请求失败 {exc}"))
            continue
        elapsed = time.perf_counter() - started

        if resp.status_code != 200:
            results.append((label, False, f"HTTP {resp.status_code} {resp.text[:160]}"))
            continue

        data = resp.json()
        meta = data.get("x_steward") or {}
        content = (data["choices"][0]["message"].get("content") or "").strip()
        calls = int(meta.get("cloud_calls") or 0)
        ok = bool(content) and ((calls >= 1) == expect_cloud)

        detail = f"{elapsed:.1f}s · 外援 {calls} 次 · 轮次 {meta.get('rounds')} · {len(content)} 字"
        if calls:
            detail += f" · 剩余 {meta.get('remaining_today')}"
        results.append((label, ok, detail))
        print(f"--- {label} ---")
        print(content[:400].replace("\n", " ") + ("..." if len(content) > 400 else ""))
        print()

    print("=" * 60)
    bad = 0
    for label, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label:<16} {detail}")
        bad += 0 if ok else 1

    server.should_exit = True
    thread.join(timeout=8)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
