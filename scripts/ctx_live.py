# SPDX-License-Identifier: Apache-2.0
"""拿真实本地端点验证上下文治理层，不产生云端费用。

  python scripts/ctx_live.py                 探测窗口 + 小样本估算校准（几秒）
  python scripts/ctx_live.py --fit 120000    造 12 万 token 历史，裁剪后真发一次（约 1–2 分钟）
  python scripts/ctx_live.py --fit 150000 --stress  故意超窗，看上游是报错还是静默截断

--stress 会把超过窗口的原始历史直接发给上游，用来确认它到底走哪条路：
返回 400 说明 context-shift 关着（好事，失败是响的）；
返回 200 但 prompt_tokens 明显小于估算，说明它在静默截断（要靠网关兜）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from steward.config import ContextCfg, EndpointCfg, AppCfg  # noqa: E402
from steward.context import Estimator, Measurer, Prober, Trimmer, count_tokens  # noqa: E402

DEFAULT_URL = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL = "/models/your-model.gguf"

SAMPLES = {
    "中文": "上下文治理要解决的是记账不对称：客户端以为窗口很大，本地模型其实装不下。" * 8,
    "英文": "The gateway is the only party that knows both the client's intent and the "
    "model's real window, so it must own the accounting. " * 8,
    "代码": "\n".join(
        f"def handler_{i}(request):\n    body = await request.json()\n    return {{'i': {i}}}"
        for i in range(12)
    ),
    "JSON": "[" + ",".join(f'{{"id":{i},"name":"item-{i}"}}' for i in range(30)) + "]",
}


def chat(ep: EndpointCfg, messages: list[dict], max_tokens: int = 8) -> dict:
    headers = {"Content-Type": "application/json"}
    if ep.api_key:
        headers["Authorization"] = f"Bearer {ep.api_key}"
    body = {"model": ep.model, "messages": messages, "max_tokens": max_tokens, "stream": False}
    started = time.perf_counter()
    resp = httpx.post(f"{ep.base_url.rstrip('/')}/chat/completions", json=body, headers=headers, timeout=400)
    elapsed = int((time.perf_counter() - started) * 1000)
    if resp.status_code >= 400:
        return {"error": resp.status_code, "detail": resp.text[:300], "ms": elapsed}
    data = resp.json()
    data["_ms"] = elapsed
    return data


def make_history(est: Estimator, target_tokens: int) -> list[dict]:
    """凑一份指定规模的历史：文件读取 + 测试日志 + 对话，接近真实 agent 负载。"""
    msgs = [{"role": "system", "content": "你在一个仓库里干活，可以用工具读文件。"}]
    msgs.append({"role": "user", "content": "把这套服务的配额从按次改成按 token 计量，并补测试。"})
    i = 0
    while est.messages(msgs) < target_tokens:
        i += 1
        msgs.append(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "先看 guard.py 的实现，再决定改哪里。" * 6,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": f'{{"path":"src/mod_{i}.py"}}'},
                    }
                ],
            }
        )
        body = "\n".join(
            f"def fn_{j}(x):\n    return x * {j}  # 处理分支 {j}" for j in range(40)
        )
        msgs.append({"role": "tool", "tool_call_id": f"call_{i}", "content": f"# src/mod_{i}.py\n{body}"})
        msgs.append({"role": "assistant", "content": f"看完 mod_{i}.py，继续下一个。" * 4})
        if i % 5 == 0:
            log = "\n".join(
                ["PASSED test_ok"] * 180
                + ["FAILED tests/test_guard.py::test_refund - AssertionError: 配额没退回"]
                + ["PASSED test_ok"] * 120
            )
            msgs.append({"role": "user", "content": f"跑了一遍测试（第 {i} 批）：\n{log}"})
    return msgs


def run_measure(base_url: str, key: str, msgs: list[dict], tools: int = 0):
    """一次性建/关客户端，避免同一个 AsyncClient 跨事件循环复用。"""

    async def go():
        m = Measurer(base_url, key)
        try:
            if not await m.probe():
                return None, False
            return await m.measure(msgs, tools), True
        finally:
            await m.aclose()

    return asyncio.run(go())


def first_user(messages: list[dict]) -> str:
    """裁剪后仍然健在的任务目标——骨架的 goal 必须对齐它。"""
    for msg in messages:
        if msg.get("role") == "user" and isinstance(msg.get("content"), str) and msg["content"].strip():
            return msg["content"].strip()[:300]
    return ""


def run_spine(cfg: ContextCfg, ep: EndpointCfg, dropped: list[dict], anchor: str):
    """真跑一次骨架提炼。同样是「一个 asyncio.run 一个连接」。"""
    from steward.spine import SpineBuilder
    from steward.upstream import BaseClient

    async def go():
        client = BaseClient(ep)
        try:
            builder = SpineBuilder(cfg, lambda: client, state_path=None)
            started = time.time()
            spine = await builder.build(dropped, None, anchor_goal=anchor)
            print(f"  耗时 {time.time() - started:.1f}s")
            return spine
        finally:
            await client.aclose()

    return asyncio.run(go())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=DEFAULT_URL)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--key", default="")
    ap.add_argument("--fit", type=int, default=0, help="造多少 token 的历史再裁剪（0 = 跳过）")
    ap.add_argument("--stress", action="store_true", help="配合 --fit，先把裁剪前的原始历史发一次")
    ap.add_argument("--out", default="", help="把裁剪前的历史存成 json，便于复现")
    ap.add_argument("--spine", action="store_true", help="配合 --fit，裁剪后真跑一次骨架提炼")
    args = ap.parse_args()

    cfg = AppCfg()
    ep = EndpointCfg(base_url=args.base_url, model=args.model, api_key=args.key, timeout_s=600)
    ctx_cfg = ContextCfg()

    print(f"\n端点 {args.base_url}")
    probe = Prober(ctx_cfg, ep)
    limits = asyncio.run(probe.probe())
    limits.finalize(ctx_cfg, out_reserve=4096)
    print(f"探测  窗口={limits.per_slot} 槽位={limits.slots} 来源={limits.source} 输入预算={limits.budget}")

    est = Estimator()

    print("\n-- 估算精度（小样本） --")
    rows = []
    for name, text in SAMPLES.items():
        raw = count_tokens(text)
        data = chat(ep, [{"role": "user", "content": text}], max_tokens=4)
        if "error" in data:
            print(f"  {name:<4} 调用失败 {data['error']}: {data['detail'][:120]}")
            continue
        actual = int((data.get("usage") or {}).get("prompt_tokens") or 0)
        rows.append((name, raw, actual))
        print(f"  {name:<4} 估算 {raw:>5}  上游 {actual:>5}  偏差 {raw / actual:.2f}x")

    if rows:
        ratios = [e / a for _, e, a in rows if a]
        print(f"  平均偏差 {sum(ratios) / len(ratios):.2f}x（1.0 完全吻合；>1 是估高了，安全方向）")
        # 走生产的校准路径，顺便验证 EWMA 会不会收敛
        for _, raw, actual in rows:
            est.observe(raw, actual)
        after = est.messages([{"role": "user", "content": SAMPLES["JSON"]}])
        print(
            f"  校准后系数 {est.factor:.3f}，同一段 JSON 重估 {after} "
            f"（上游实际 {dict((n, a) for n, _, a in rows).get('JSON', 0)}）"
        )

    if not args.fit:
        print("\n想验证裁剪：加 --fit 120000\n")
        return 0

    print(f"\n-- 造 {args.fit} token 的历史 --")
    msgs = make_history(est, args.fit)
    raw_total = est.messages(msgs)
    print(f"  {len(msgs)} 条消息，估算 {raw_total} tokens")

    measurer = None
    exact, supported = run_measure(ep.base_url, ep.api_key, msgs)
    print(f"  /tokenize {'可用' if supported else '不可用'}")
    if exact:
        scale = min(2.0, max(0.5, exact / raw_total))
        est.factor = min(6.0, max(0.3, est.factor * scale))
        print(f"  精确 {exact} tokens，估算 {raw_total}，系数校正到 {est.factor:.3f}")

    if args.stress:
        print("  先把原始历史直接发一次，看上游怎么处理 ...")
        data = chat(ep, msgs, max_tokens=4)
        if "error" in data:
            print(f"  → 上游拒绝 {data['error']}（响亮失败，符合预期）：{data['detail'][:160]}")
        else:
            actual = int((data.get("usage") or {}).get("prompt_tokens") or 0)
            verdict = "静默截断" if actual < raw_total * 0.9 else "照单全收"
            print(f"  → 上游返回 200，prompt_tokens={actual}（估算 {raw_total}）＝ {verdict}")

    trimmer = Trimmer(ctx_cfg, est)
    trimmed, report = trimmer.fit(
        msgs,
        budget=limits.budget,
        target=int(limits.budget * ctx_cfg.target_ratio),
        tools_tokens=0,
    )
    print(f"  裁剪 {report.before} -> {report.after} tokens（省 {report.saved}）")
    print(
        f"  思维链 {report.stripped_reasoning} / 摘要 {report.digested} / "
        f"丢对 {report.dropped} / 硬截 {report.hard_cut} / 消息 {len(msgs)} -> {len(trimmed)}"
    )

    if args.out:
        Path(args.out).write_text(
            __import__("json").dumps(msgs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  原始历史已存 {args.out}")

    print("  真发一次裁剪后的请求 ...")
    exact_trimmed = run_measure(ep.base_url, ep.api_key, trimmed)[0] if supported else None
    data = chat(ep, trimmed, max_tokens=16)
    if "error" in data:
        print(f"  → 失败 {data['error']}: {data['detail'][:200]}")
        return 1
    actual = int((data.get("usage") or {}).get("prompt_tokens") or 0)
    timings = data.get("timings") or {}
    print(
        f"  → 成功，prompt_tokens={actual}（估算 {report.after}"
        + (f"，精确 {exact_trimmed}" if exact_trimmed else "")
        + f"），prefill {timings.get('prompt_ms', 0) / 1000:.1f}s"
    )
    if exact_trimmed:
        print(f"  精确计数与上游偏差 {exact_trimmed / actual:.3f}x")
    print(f"  窗口占用 {actual / limits.per_slot * 100:.0f}%\n")

    if args.spine:
        print("-- 骨架提炼（真模型） --")
        if not report.dropped_messages:
            print("  这次裁剪没丢消息，没有可提炼的区域\n")
            return 0
        spine = run_spine(ctx_cfg, ep, report.dropped_messages, first_user(trimmed))
        if spine is None:
            print("  提炼失败\n")
            return 1
        used = est.text(spine.as_text())
        print(f"  输入 {min(len(report.dropped_messages), 10**9)} 条丢弃消息 / 骨架 {used} tokens / 来源 {spine.source}")
        print("  " + "-" * 56)
        for line in spine.as_text().splitlines():
            print("  " + line)
        print("  " + "-" * 56)
        if spine.goal != first_user(trimmed):
            print(f"  !! goal 没有对齐用户原话：{spine.goal!r}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
