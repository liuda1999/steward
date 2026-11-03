"""真实进程冒烟：起 uvicorn，打 /health 和 /v1/models，然后退出。

用法：python scripts/smoke.py [端口]
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from steward.config import load_config  # noqa: E402
from steward.server import create_app  # noqa: E402


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18010
    cfg = load_config(ROOT / "steward" / "config.yaml")
    cfg.base.model = cfg.base.model or "smoke-base"
    cfg.advanced.model = cfg.advanced.model or "smoke-advanced"
    cfg.advanced.base_url = cfg.advanced.base_url or "http://127.0.0.1:1/v1"
    # 冒烟不污染真实 key 文件
    cfg.auth.keys_file = str(Path(tempfile.mkdtemp(prefix="st-smoke-")) / "keys.json")

    app = create_app(cfg)
    token = app.state.bootstrap_key
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    last_error = ""
    for _ in range(80):
        try:
            health = httpx.get(f"{base}/health", timeout=2.0)
            break
        except httpx.HTTPError as exc:
            last_error = str(exc)
            time.sleep(0.1)
    else:
        print(f"FAIL 服务未起来: {last_error}")
        return 1

    models = httpx.get(f"{base}/v1/models", headers=headers, timeout=5.0)
    anonymous = httpx.get(f"{base}/v1/models", timeout=5.0)

    print(f"GET /health      -> {health.status_code}")
    print(f"   quota         -> {health.json()['quota']['used_today']}/{health.json()['quota']['daily_limit']}")
    print(f"   per-turn      -> {health.json()['quota']['max_calls_per_turn']}")
    print(f"   auth          -> {health.json()['auth']['mode']} active={health.json()['auth']['keys_active']}")
    print(f"GET /v1/models   -> {models.status_code} {[m['id'] for m in models.json()['data']]}")
    print(f"GET 无 key       -> {anonymous.status_code}（应为 401）")

    server.should_exit = True
    thread.join(timeout=8)
    ok = health.status_code == 200 and models.status_code == 200 and anonymous.status_code == 401
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
