"""真实链路联调：假本地模型 + 假云端 + steward，全部走真实 HTTP。

用法：python scripts/demo.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from steward.config import AppCfg, AuthCfg, EndpointCfg, GuardCfg  # noqa: E402
from steward.server import create_app  # noqa: E402

LOCAL_PORT = 18101
CLOUD_PORT = 18102
GW_PORT = 18100


def build_fake_local() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        msgs = body.get("messages") or []
        seen_tools = [m for m in msgs if m.get("role") == "tool"]

        if not seen_tools:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_demo_1",
                        "type": "function",
                        "function": {
                            "name": "consult_cloud",
                            "arguments": json.dumps(
                                {
                                    "kind": "design",
                                    "ask": "两级缓存怎么划分边界",
                                    "brief": "goal: 读性能提升 3 倍\ngiven: 单机 QPS 2k，Redis 已有\ntried: 加过索引，无效\nblocked: 不知道 L1 放什么\nconstraints: Python 3.13",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
            return {
                "id": "l1",
                "object": "chat.completion",
                "created": 0,
                "model": "fake-local",
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 20},
            }

        return {
            "id": "l2",
            "object": "chat.completion",
            "created": 0,
            "model": "fake-local",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "已按交付物实现两级缓存。"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }

    return app


def build_fake_cloud() -> FastAPI:
    app = FastAPI()
    state = {"calls": 0, "last": None}

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        state["calls"] += 1
        state["last"] = body
        doc = (
            "## 1. 结论\nL1 放热点对象，L2 放整表查询结果。\n\n"
            "## 2. 执行步骤\n1. 动作：在 ORM 层加 L1\n   输入：热点 key 白名单\n   输出：命中率指标\n   失败判据：命中率 < 60% 或内存 > 200MB\n\n"
            "## 3. 风险与不确定项\n- 写后读不一致，触发条件是同一请求内先写后读\n\n"
            "## 4. 待本地补充\n- 现有 Redis 内存上限"
        )
        return {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "fake-cloud",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": doc}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 200},
        }

    @app.get("/stats")
    async def stats():
        return {"calls": state["calls"]}

    return app


def serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return server


def wait_ready(url: str, tries: int = 80) -> bool:
    for _ in range(tries):
        try:
            httpx.get(url, timeout=1.0)
            return True
        except httpx.HTTPError:
            time.sleep(0.1)
    return False


def main() -> int:
    local_server = serve(build_fake_local(), LOCAL_PORT)
    cloud_server = serve(build_fake_cloud(), CLOUD_PORT)

    cfg = AppCfg(
        auth=AuthCfg(mode="required", keys_file=str(ROOT / "state" / "demo-keys.json")),
        base=EndpointCfg(base_url=f"http://127.0.0.1:{LOCAL_PORT}/v1", model="fake-local", api_key="x"),
        advanced=EndpointCfg(base_url=f"http://127.0.0.1:{CLOUD_PORT}/v1", model="fake-cloud", api_key="x"),
        guard=GuardCfg(
            state_file=str(ROOT / "state" / "demo-usage.json"),
            max_calls_per_turn=2,
            daily_limit=5,
            min_interval_s=0,
            max_tool_rounds=3,
        ),
    )
    Path(cfg.guard.state_file).unlink(missing_ok=True)
    Path(cfg.auth.keys_file).unlink(missing_ok=True)
    app = create_app(cfg)
    gw_server = serve(app, GW_PORT)

    if not wait_ready(f"http://127.0.0.1:{GW_PORT}/health"):
        print("FAIL 网关未启动")
        return 1

    # 项目自己签发的 key，模拟"填到智能体里"的那一把
    token = app.state.bootstrap_key
    resp = httpx.post(
        f"http://127.0.0.1:{GW_PORT}/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "local", "messages": [{"role": "user", "content": "设计一个两级缓存"}]},
        timeout=30,
    )
    anon = httpx.post(
        f"http://127.0.0.1:{GW_PORT}/v1/chat/completions",
        json={"model": "local", "messages": [{"role": "user", "content": "无 key"}]},
        timeout=10,
    )
    data = resp.json()

    print(f"项目 key          -> {token[:11]}-****")
    print(f"无 key 请求       -> {anon.status_code}（应为 401）")
    print(f"status            -> {resp.status_code}")
    print(f"final content     -> {data['choices'][0]['message']['content']}")
    print(f"cloud_calls       -> {data['x_steward']['cloud_calls']}")
    print(f"remaining_today   -> {data['x_steward']['remaining_today']}")
    print(f"responded headers -> calls={resp.headers.get('x-steward-calls')}")
    print(f"fake cloud hits   -> {httpx.get(f'http://127.0.0.1:{CLOUD_PORT}/stats').json()['calls']}")

    gw_server.should_exit = True
    local_server.should_exit = True
    cloud_server.should_exit = True
    time.sleep(0.3)

    ok = (
        resp.status_code == 200
        and data["x_steward"]["cloud_calls"] == 1
        and anon.status_code == 401
    )
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
