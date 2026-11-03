"""OpenAI 兼容的本地模型网关。

对外只有一个端口、一个端点。客户端（任意智能体）把 base_url 指过来即可，
它不知道云端存在。

请求路径：
  client -> /v1/chat/completions
         -> 注入 system 规则 + consult_cloud 工具
         -> 本地模型
         -> 若模型调用 consult_cloud：护栏判定 -> 调云端 -> 以 role=tool 回灌
         -> 本地模型继续，直到不再调用工具或轮次用尽
         -> 返回（可选 SSE 流式）
"""

from __future__ import annotations

import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .artifacts import Store
from .cloud import AdvancedClient, flatten_content
from .config import AppCfg
from .context import Estimator, Limits, Measurer, Prober, Trimmer, TrimReport
from .guard import Guard, TurnState
from .keys import KeyStore
from .prompts import (
    CONSULT_NAME,
    DEFAULT_FORCE_FINISH_TEXT,
    DEFAULT_SYSTEM_TEXT,
    DEFAULT_TOOL_DESCRIPTION,
    build_consult_tool,
    precheck,
    render_quota_note,
)
from .refs import Resolver
from .spine import Spine, SpineBuilder, inject_spine
from .upstream import BaseClient

logger = logging.getLogger("steward")

PASSTHROUGH = (
    "temperature",
    "top_p",
    "max_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
    "chat_template_kwargs",
)

SSE_CHUNK = 60

# 上游说这些词，就当成「装不下了」而不是「请求非法」
_OVERFLOW_HINTS = (
    "context",
    "exceed",
    "too long",
    "maximum length",
    "max_model_len",
    "n_ctx",
    "reduce the length",
    "prompt is too long",
)


def is_overflow(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code not in (400, 413, 422, 500):
        return False
    body = (exc.response.text or "").lower()
    return any(hint in body for hint in _OVERFLOW_HINTS)


def inject_rules(messages: list[dict], text: str) -> list[dict]:
    out = [dict(m) for m in messages]
    if not text:
        return out
    for m in out:
        if m.get("role") == "system":
            content = m.get("content")
            if isinstance(content, str):
                m["content"] = (content.rstrip() + "\n\n" + text).strip()
            elif isinstance(content, list):
                m["content"] = content + [{"type": "text", "text": text}]
            else:
                m["content"] = text
            return out
    out.insert(0, {"role": "system", "content": text})
    return out


def _first_user_text(messages: list[dict], limit: int = 300) -> str:
    """取裁剪后仍然健在的第一句用户目标，给机械骨架兜底用。"""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:limit]
    return ""


def merge_tools(client_tools: Any, tool_def: dict) -> tuple[list[dict], bool]:
    tools = [t for t in (client_tools or []) if isinstance(t, dict)]
    names = {(t.get("function") or {}).get("name") for t in tools}
    if CONSULT_NAME in names:
        logger.warning("客户端已自带同名工具 %s，跳过注入", CONSULT_NAME)
        return tools, False
    return tools + [tool_def], True


def parse_consult_args(tool_call: dict) -> tuple[str, str, str]:
    fn = tool_call.get("function") or {}
    raw = fn.get("arguments") or "{}"
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    kind = str(data.get("kind") or "design")
    if kind not in ("design", "diagnosis", "review", "decision"):
        kind = "design"
    return kind, str(data.get("ask") or "").strip(), str(data.get("brief") or "").strip()


def parse_refs(tool_call: dict) -> list:
    """单独取 refs，不动 parse_consult_args 的三元组契约。"""
    fn = tool_call.get("function") or {}
    raw = fn.get("arguments") or "{}"
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    refs = data.get("refs")
    return refs if isinstance(refs, list) else []


def tool_call_name(tc: dict) -> str:
    return str(((tc.get("function") or {}).get("name")) or "")


def serialize_tool_calls(tool_calls: list[dict]) -> list[dict]:
    out = []
    for i, tc in enumerate(tool_calls):
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {}, ensure_ascii=False)
        out.append(
            {
                "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "index": i,
                "function": {"name": fn.get("name") or "", "arguments": args},
            }
        )
    return out


def build_usage(resp: dict | None, turn: TurnState) -> dict:
    usage = (resp or {}).get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cloud": {
            "calls": turn.calls,
            "prompt_tokens": turn.prompt_tokens,
            "completion_tokens": turn.completion_tokens,
        },
    }


def chunk_text(text: str, size: int = SSE_CHUNK) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def extract_token(request: Request) -> str:
    """兼容三种常见写法：Bearer、x-api-key、api-key(Azure)。"""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    for name in ("x-api-key", "api-key"):
        value = request.headers.get(name, "")
        if value:
            return value.strip()
    return ""


def create_app(cfg: AppCfg, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    guard = Guard(cfg.guard)
    base = BaseClient(cfg.base, transport)
    advanced = AdvancedClient(cfg.advanced, transport)
    store = KeyStore(cfg.auth.keys_file)

    estimator = Estimator(state_path=cfg.context.state_file)
    artifacts = Store(cfg.artifacts) if cfg.artifacts.enabled else None
    trimmer = Trimmer(
        cfg.context,
        estimator,
        spill=artifacts.spill if artifacts else None,
        spill_batch=artifacts.spill_batch if artifacts else None,
    )
    prober = Prober(cfg.context, cfg.base, transport)
    measurer = Measurer(cfg.base.base_url, cfg.base.api_key, cfg.base.extra_headers, transport)
    # 骨架提炼走独立的客户端。共用 base 的话，提炼那次撞上 keep-alive 404
    # 自愈时会把整个连接池关掉重建，正在跑的主请求就被连坐（实测 502）。
    spine_client = BaseClient(cfg.base, transport)
    spine_builder = SpineBuilder(
        cfg.context,
        client_factory=lambda: spine_client,
        state_path=cfg.context.spine_state_file,
    )
    ctx: dict = {
        "limits": Limits(n_ctx=cfg.context.ctx_limit, per_slot=cfg.context.ctx_limit, source="pending"),
        "last": {},
        "compactions": 0,
        "truncated": 0,
        "spine_goal": "",
        "spine_source": "",
        "spine_built": 0,
    }

    def current_budget() -> int:
        """输入预算 = 可用窗口 - 输出预留 - 安全余量，再乘学习到的收缩系数。"""
        lim: Limits = ctx["limits"]
        usable = lim.usable or int((lim.per_slot or 32768) * cfg.context.usable_ratio)
        budget = usable
        if cfg.context.reserve_output:
            budget -= max(0, cfg.base.max_tokens)
        budget -= max(0, cfg.context.safety_tokens)
        return max(1024, int(budget * estimator.budget_scale))

    system_text = cfg.prompt.system_text or DEFAULT_SYSTEM_TEXT
    tool_def = build_consult_tool(
        cfg.prompt.tool_description or DEFAULT_TOOL_DESCRIPTION,
        refs_enabled=cfg.refs.enabled,
    )
    resolver = Resolver(cfg.refs, Path(cfg.refs.audit_file) if cfg.refs.audit_file else None)
    if cfg.refs.enabled:
        logger.warning(
            "本地文件引用已启用，白名单 %s（共 %d 个根目录）。这是一条把本地内容发出去的通道，确认这是你要的。",
            ", ".join(cfg.refs.roots) or "（空）",
            len(cfg.refs.roots),
        )
    force_finish = cfg.prompt.force_finish_text or DEFAULT_FORCE_FINISH_TEXT
    # 进阶槽位可以是本地端点（不需要 key），所以只要求地址和模型名
    advanced_ready = bool(cfg.advanced.base_url and cfg.advanced.model)

    bootstrap_key = ""
    if cfg.auth.mode == "required" and cfg.auth.auto_issue and store.empty and not cfg.server.api_key:
        bootstrap_key, _ = store.issue("bootstrap")
        logger.warning(
            "\n"
            "============================================================\n"
            "  已签发项目 API Key（只显示这一次，请立刻保存）\n"
            "  %s\n"
            "  填到智能体里：OPENAI_API_KEY / api_key = 上面这串\n"
            "  重新签发：python -m steward key new --label <名字>\n"
            "============================================================",
            bootstrap_key,
        )

    async def refresh_limits() -> Limits:
        limits = await prober.probe()
        limits.finalize(cfg.context, cfg.base.max_tokens, estimator.budget_scale)
        ctx["limits"] = limits
        return limits

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        snap = await guard.snapshot()
        logger.info(
            "启动：基础=%s 进阶=%s 今日 %s/%s 单请求上限=%s",
            cfg.base.model,
            cfg.advanced.model,
            snap["used_today"],
            snap["daily_limit"],
            snap["max_calls_per_turn"],
        )
        limits = await refresh_limits()
        logger.info(
            "上下文：窗口 %s tokens（每槽 %s / 槽位 %s / 来源 %s），输入预算 %s，估算系数 %.2f",
            limits.per_slot,
            limits.per_slot,
            limits.slots,
            limits.source,
            current_budget(),
            estimator.factor,
        )
        if limits.source in ("default", "config") and not cfg.context.ctx_limit:
            logger.warning(
                "没能探到上游真实窗口，按 %s 保守估计。若本地实际更小，"
                "请在 config.yaml 里写 context.ctx_limit 显式指定。",
                limits.per_slot,
            )
        if not advanced_ready:
            logger.warning("进阶模型配置不完整，consult_cloud 将一律拦截且不计数")
        if cfg.context.enabled:
            if await measurer.probe():
                logger.info("上游支持 /tokenize，上下文按精确 token 计数")
            else:
                logger.info("上游没有 /tokenize，按字符类估算 + 自校准")
        yield
        await base.aclose()
        await spine_client.aclose()
        await advanced.aclose()
        await measurer.aclose()

    app = FastAPI(title="steward", version="1.0.0", lifespan=lifespan)
    app.state.key_store = store
    app.state.bootstrap_key = bootstrap_key

    async def auth(request: Request) -> None:
        if cfg.auth.mode == "off":
            request.state.key_id = "off"
            return
        token = extract_token(request)
        if not token:
            raise HTTPException(
                status_code=401,
                detail="missing api key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        # 兼容旧配置里的静态 key
        if cfg.server.api_key and hmac.compare_digest(token, cfg.server.api_key):
            request.state.key_id = "static"
            return
        rec = store.verify(token)
        if rec is None:
            raise HTTPException(
                status_code=401,
                detail="invalid api key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        store.touch(rec.id)
        request.state.key_id = rec.id

    # ---------- 元信息 ----------

    @app.get("/health")
    async def health() -> JSONResponse:
        snap = await guard.snapshot()
        limits: Limits = ctx["limits"]
        return JSONResponse(
            {
                "status": "ok",
                "base": {"base_url": cfg.base.base_url, "model": cfg.base.model},
                "advanced": {
                    "base_url": cfg.advanced.base_url,
                    "model": cfg.advanced.model,
                    "protocol": cfg.advanced.protocol,
                    "configured": advanced_ready,
                },
                "context": {
                    **limits.as_dict(),
                    "budget": current_budget(),
                    "estimator": round(estimator.factor, 3),
                    "budget_scale": round(estimator.budget_scale, 3),
                },
                "quota": snap,
                "auth": {"mode": cfg.auth.mode, "keys_active": store.summary()["active"]},
            }
        )

    @app.get("/v1/models")
    async def models(_: None = Depends(auth)) -> JSONResponse:
        ids = [cfg.public_model] + [a for a in cfg.model_aliases if a and a != cfg.public_model]
        # 把真实窗口说出来，行为良好的客户端才能按正确阈值做自己的压缩。
        # 瞒着不说，客户端会按它以为的模型窗口决策，两头对不上。
        limits: Limits = ctx["limits"]
        window = limits.per_slot or cfg.context.ctx_limit or 32768
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": name,
                        "object": "model",
                        "created": 0,
                        "owned_by": "steward",
                        "context_length": window,
                        "max_output_tokens": cfg.base.max_tokens,
                    }
                    for name in ids
                ],
            }
        )

    @app.get("/admin/context")
    async def context_info(_: None = Depends(auth)) -> JSONResponse:
        limits: Limits = ctx["limits"]
        # 只在这里扫一次盘：stats() 是 rglob，别在热路径上反复调
        art_stats = artifacts.stats() if artifacts else {}
        return JSONResponse(
            {
                "limits": limits.as_dict(),
                "budget": current_budget(),
                "estimator": {
                    "factor": round(estimator.factor, 4),
                    "budget_scale": round(estimator.budget_scale, 4),
                    "last": {
                        "estimated": estimator.last.estimated,
                        "actual": estimator.last.actual,
                        "ratio": estimator.last.ratio,
                        "truncated": estimator.last.truncated,
                    },
                },
                "config": {
                    "usable_ratio": cfg.context.usable_ratio,
                    "target_ratio": cfg.context.target_ratio,
                    "min_clear_tokens": cfg.context.min_clear_tokens,
                    "keep_recent": cfg.context.keep_recent,
                    "reserve_output": cfg.context.reserve_output,
                    "max_tokens_out": cfg.base.max_tokens,
                },
                "tokenize": {
                    "supported": bool(measurer.supported),
                    "per_message_tokens": round(measurer.per_message, 1),
                },
                "last_trim": ctx["last"],
                "compactions": ctx["compactions"],
                "truncated_seen": ctx["truncated"],
                "spine": {
                    "enabled": cfg.context.spine,
                    "last_goal": ctx.get("spine_goal", ""),
                    "last_source": ctx.get("spine_source", ""),
                    "built": ctx.get("spine_built", 0),
                },
                "artifacts": {
                    "enabled": artifacts is not None,
                    "dir": str(artifacts.root) if artifacts else "",
                    "files": art_stats.get("files", 0),
                    "bytes": art_stats.get("bytes", 0),
                    "max_age_days": cfg.artifacts.max_age_days,
                    "max_total_mb": cfg.artifacts.max_total_mb,
                },
                "refs": {
                    "enabled": cfg.refs.enabled,
                    "roots": list(cfg.refs.roots),
                    "max_files": cfg.refs.max_files,
                    "max_total_chars": cfg.refs.max_total_chars,
                    "audit_file": cfg.refs.audit_file,
                },
            }
        )

    @app.post("/admin/context/probe")
    async def context_probe(_: None = Depends(auth)) -> JSONResponse:
        limits = await refresh_limits()
        return JSONResponse({"limits": limits.as_dict(), "budget": current_budget()})

    @app.get("/admin/usage")
    async def usage(_: None = Depends(auth)) -> JSONResponse:
        snap = await guard.snapshot()
        snap["keys"] = store.summary()
        return JSONResponse(snap)

    @app.post("/admin/reset-day")
    async def reset_day(_: None = Depends(auth)) -> JSONResponse:
        await guard.reset_day()
        return JSONResponse({"ok": True, "quota": await guard.snapshot()})

    # ---------- 主端点 ----------

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, _: None = Depends(auth)):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="body must be json")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="messages is required")

        want_stream = bool(body.get("stream"))
        client_model = str(body.get("model") or cfg.public_model)
        turn = guard.new_turn(getattr(request.state, "key_id", ""))

        tools, tool_injected = merge_tools(body.get("tools"), tool_def)

        # 配额每轮都在变，所以每轮现拼。模型知道自己还剩几次，才会掂量着用；
        # 只靠被拒后那条提示太晚，那一轮已经浪费了。
        snap0 = await guard.snapshot()
        quota_note = render_quota_note(snap0["remaining_today"], cfg.guard.max_calls_per_turn)
        if tool_injected:
            note = f"{system_text}\n\n{quota_note}" if quota_note else system_text
        else:
            # 客户端自带了同名工具，网关没有注入：此时再在 system 里说
            # 「把它当外部专家」、再报 consult_cloud 的配额，就是把模型指向
            # 一个不存在的描述。用户自定义的 system_text 仍然照发。
            note = cfg.prompt.system_text or ""
            if note:
                logger.warning("未注入 %s（客户端已自带同名工具），跳过默认 system 提示", CONSULT_NAME)
        merged_messages = inject_rules(messages, note)

        base_payload: dict = {"model": cfg.base.model, "stream": False}
        for key in PASSTHROUGH:
            if body.get(key) is not None:
                base_payload[key] = body[key]

        # 推理型模型（Qwen3 / DeepSeek-R1 一类）把思维链也算进 max_tokens。
        # 客户端给 200 时，模型还没开始写工具参数就被截断，看起来像"不会调用工具"。
        floor = cfg.base.min_max_tokens
        requested = base_payload.get("max_tokens")
        if floor and isinstance(requested, int) and requested < floor:
            logger.info("max_tokens=%s 太小，抬到 %s（推理 token 会吃预算）", requested, floor)
            base_payload["max_tokens"] = floor

        # 基础模型的活是"判断该不该找外援"，不是深度思考。
        # 推理型模型开着 thinking 会把预算烧在思维链上，工具调用迟迟不吐。
        if cfg.base.thinking in ("off", "on"):
            kwargs = dict(base_payload.get("chat_template_kwargs") or {})
            kwargs.setdefault("enable_thinking", cfg.base.thinking == "on")
            base_payload["chat_template_kwargs"] = kwargs

        rounds = 0
        escalated = False
        last_resp: dict | None = None
        final_message: dict = {"role": "assistant", "content": ""}
        pending_tools: list[dict] = []
        finish_reason = "stop"

        tools_tokens = estimator.tools_tokens(tools) if tools else 0
        budget = current_budget()
        last_report = TrimReport()
        compacted = False
        refs_files = 0
        truncated = False
        spine_state: Spine | None = None
        spine_model_calls = 0  # 一个请求最多为骨架花一次模型调用

        async def maybe_spine(
            trimmed: list[dict], rep: TrimReport, hard_budget: int, tools_tokens: int = 0
        ) -> list[dict]:
            """裁剪丢了大量内容时，把那片区域提炼成六行接回 system。

            不这么做的话，模型只看到一句「压缩了 N 条」，既不知道目标，
            也不知道哪些方案已经试过并被否决，于是会原地重试。
            """
            nonlocal merged_messages, spine_state, spine_model_calls
            if (
                not cfg.context.spine
                or not rep.dropped_messages
                or rep.dropped_tokens < cfg.context.spine_min_dropped
            ):
                return trimmed
            if rep.after + 1200 > hard_budget:
                return trimmed  # 装不下就别硬加，宁可只剩那行计数提示
            built = await spine_builder.build(
                rep.dropped_messages,
                spine_state,
                anchor_goal=_first_user_text(trimmed),
                allow_model=spine_model_calls < 1,
            )
            if built is None:
                return trimmed
            spine_state = built
            if built.source != "cache":
                spine_model_calls += 1
                ctx["spine_built"] = int(ctx.get("spine_built", 0)) + 1
                logger.info(
                    "生成骨架 turn=%s source=%s 丢弃 %s 条 / 本请求第 %s 次",
                    turn.turn_id,
                    built.source,
                    len(rep.dropped_messages),
                    spine_model_calls,
                )
            ctx["spine_goal"] = built.goal
            ctx["spine_source"] = built.source
            # merged_messages 也要写：下一轮如果没触发裁剪，模型读的就是它，
            # 骨架只挂在 trimmed 上会当场丢掉。
            merged_messages = inject_spine(merged_messages, built)
            out = inject_spine(trimmed, built)
            # 骨架加了 ~800 tokens，报告必须跟着改。不改的话 observe() 会拿
            # 「不含骨架的估算」去比「含骨架的实际值」，每次都判成低估，
            # 系数一路往上漂，裁剪点跟着漂 —— 骨架缓存就永远命中不了。
            rep.after = int(estimator.messages(out, tools_tokens))
            return out

        while True:
            rounds += 1
            payload = dict(base_payload)
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = body.get("tool_choice") or "auto"

            # 有能力就用上游 tokenizer 拿精确值，并据此校正本次请求的估算比例。
            # 字符启发式在代码/JSON 上会低估 25%–40%，而那正是 agent 上下文的主体。
            exact = await measurer.measure(merged_messages, tools_tokens)
            if exact:
                approx = estimator.messages(merged_messages, tools_tokens)
                if approx > 0:
                    scale = min(2.0, max(0.5, exact / approx))
                    estimator.factor = min(6.0, max(0.3, estimator.factor * scale))

            # 每一轮都过一遍预算：交付物和工具结果是在循环中途长大的，
            # 只在入口判一次，第四轮就会被顶爆。
            msgs, report = trimmer.fit(
                merged_messages,
                budget=budget,
                target=int(budget * cfg.context.target_ratio),
                tools_tokens=tools_tokens,
                keep_recent=cfg.context.keep_recent,
            )
            last_report = report
            compacted = compacted or report.touched
            msgs = await maybe_spine(msgs, report, budget, tools_tokens)
            payload["messages"] = msgs

            resp: dict | None = None
            for attempt in (1, 2):
                try:
                    resp = await base.chat(payload)
                    break
                except httpx.HTTPStatusError as exc:
                    # 装不下不是「请求非法」，不该让整轮工具调用白做。
                    # 紧急裁剪 + 记住教训 + 重试一次。
                    if attempt == 1 and cfg.context.overflow_retry and is_overflow(exc):
                        logger.warning(
                            "上游报上下文溢出（%s），紧急裁剪后重试一次：%s",
                            exc.response.status_code,
                            (exc.response.text or "")[:200],
                        )
                        estimator.shrink(0.85, reason="overflow")
                        budget = current_budget()
                        # 预算本身已经被证明不可信，按「刚才那份实际没装下」来砍更靠谱
                        target = max(1024, int(min(budget * 0.45, last_report.before * 0.4)))
                        msgs, report = trimmer.fit(
                            merged_messages,
                            budget=budget,
                            target=target,
                            tools_tokens=tools_tokens,
                            keep_recent=cfg.context.keep_recent,
                            force=True,
                        )
                        last_report = report
                        compacted = True
                        msgs = await maybe_spine(msgs, report, budget, tools_tokens)
                        payload["messages"] = msgs
                        continue

                    detail = (exc.response.text or "")[:400]
                    url = str(exc.request.url)
                    logger.error(
                        "基础模型返回 %s url=%s 消息数=%s 估算=%s: %s",
                        exc.response.status_code,
                        url,
                        len(payload.get("messages") or []),
                        last_report.after,
                        detail,
                    )
                    return JSONResponse(
                        status_code=502,
                        content={
                            "error": {
                                "message": f"local upstream {exc.response.status_code} at {url}: {detail}",
                                "type": "upstream_error",
                            }
                        },
                    )
                except httpx.HTTPError as exc:
                    logger.error("本地模型不可达: %s", exc)
                    return JSONResponse(
                        status_code=502,
                        content={
                            "error": {"message": f"local upstream unreachable: {exc}", "type": "upstream_error"}
                        },
                    )

            if resp is None:
                return JSONResponse(
                    status_code=502,
                    content={"error": {"message": "local upstream returned no response", "type": "upstream_error"}},
                )

            last_resp = resp

            # 用上游返回的真实 prompt_tokens 校准估算器，顺带发现静默截断
            actual = int(((resp.get("usage") or {}).get("prompt_tokens")) or 0)
            if actual and last_report.after and estimator.observe(last_report.after, actual):
                truncated = True
                ctx["truncated"] += 1
            choices = resp.get("choices") or [{}]
            msg = choices[0].get("message") or {}
            content = flatten_content(msg.get("content"))
            finish = str(choices[0].get("finish_reason") or "")
            calls = [tc for tc in (msg.get("tool_calls") or []) if isinstance(tc, dict)]

            # 推理型模型会把思维链算进 max_tokens：预算不够时既没内容也没工具调用，
            # 这一轮完全白花。加倍预算重试一次，比把空回复交给用户强。
            if not calls and not content and finish == "length" and not escalated:
                current = int(base_payload.get("max_tokens") or 0)
                cap = cfg.base.max_tokens
                if current and current < cap:
                    escalated = True
                    base_payload["max_tokens"] = min(max(current * 2, 2048), cap)
                    logger.warning(
                        "空回复（预算被推理吃光）max_tokens %s -> %s，重试",
                        current or "未设",
                        base_payload["max_tokens"],
                    )
                    continue

            consult_calls = [tc for tc in calls if tool_call_name(tc) == CONSULT_NAME]
            other_calls = [tc for tc in calls if tool_call_name(tc) != CONSULT_NAME]

            if not consult_calls:
                final_message = {"role": "assistant", "content": content}
                if msg.get("reasoning_content"):
                    final_message["reasoning_content"] = msg["reasoning_content"]
                if other_calls:
                    final_message["tool_calls"] = serialize_tool_calls(other_calls)
                    finish_reason = "tool_calls"
                break

            echoed: dict = {
                "role": "assistant",
                "content": content or "",
                "tool_calls": [
                    {
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": CONSULT_NAME,
                            "arguments": (tc.get("function") or {}).get("arguments") or "{}",
                        },
                    }
                    for tc in consult_calls
                ],
            }
            # 丢掉 reasoning_content 会让模型在下一轮重新推一遍，白烧 token；
            # llama.cpp 的 Qwen 模板认得这个字段。
            if msg.get("reasoning_content"):
                echoed["reasoning_content"] = msg["reasoning_content"]
            merged_messages.append(echoed)

            for tc in consult_calls:
                call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                kind, ask, brief = parse_consult_args(tc)

                if not ask or not brief:
                    merged_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": "[网关] 参数不完整：ask 和 brief 都必须填写。请补齐后重新调用；如果本地已经能解决，就直接解决。",
                        }
                    )
                    continue

                if not advanced_ready:
                    turn.denied += 1
                    merged_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": "[网关] 进阶模型未配置（缺 base_url / model）。不要重试，用本地能力完成，必要时在答复里说明这一节没有外部输入。",
                        }
                    )
                    continue

                # 质量闸放在配额闸之前：一份烂 brief 花掉一次额度还拿回错方案，
                # 比不调用更糟。退回不计数，但要限次，否则模型会在原地打转。
                issue = precheck(
                    kind,
                    ask,
                    brief,
                    max_ask_chars=min(400, cfg.guard.max_brief_chars),
                )
                if issue and turn.precheck_rejects < 2:
                    turn.precheck_rejects += 1
                    logger.info(
                        "退回不完整委托 turn=%s kind=%s 第 %s 次", turn.turn_id, kind, turn.precheck_rejects
                    )
                    merged_messages.append({"role": "tool", "tool_call_id": call_id, "content": issue})
                    continue
                if issue:
                    logger.warning("委托单两次未补齐，仍然放行 turn=%s kind=%s", turn.turn_id, kind)

                decision = await guard.reserve(turn, kind, ask, brief)
                if not decision.allowed:
                    logger.info("拦截进阶调用 turn=%s code=%s", turn.turn_id, decision.code)
                    merged_messages.append({"role": "tool", "tool_call_id": call_id, "content": decision.notice})
                    continue

                # 放行之后才读盘：被拦下的调用不该在磁盘上留下痕迹
                refs_result = resolver.expand(parse_refs(tc), turn_id=turn.turn_id)
                if refs_result.files:
                    refs_files += len(refs_result.files)
                    logger.info(
                        "refs 展开 turn=%s 文件=%d 字数=%d%s",
                        turn.turn_id,
                        len(refs_result.files),
                        refs_result.chars,
                        f" 拒绝={refs_result.rejected}" if refs_result.rejected else "",
                    )

                try:
                    result = await advanced.consult(
                        kind,
                        ask,
                        brief,
                        cfg.guard.max_brief_chars,
                        cfg.guard.max_result_chars,
                        store=artifacts,
                        turn_id=turn.turn_id,
                        inline_chars=cfg.artifacts.inline_chars,
                        refs_text=refs_result.text,
                    )
                except Exception as exc:  # noqa: BLE001
                    # consult 内部已经兜住了 httpx 的各种失败，能走到这里说明是
                    # 预期之外的（响应结构畸形、存档失败等）。此时配额已经扣了：
                    # 不退的话这一次白花；让整请求 500 更糟 —— 模型连"失败了"都
                    # 不知道，只看到一个断掉的连接。
                    logger.warning("进阶模型调用异常 turn=%s: %s", turn.turn_id, exc, exc_info=True)
                    await guard.refund(turn)
                    merged_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": "[网关失败] 进阶模型调用出现异常，本次未消耗额度。"
                            "不要重试，用本地能力继续；必要的话在答复里说明这一节缺外部输入。",
                        }
                    )
                    continue

                if result.not_sent:
                    await guard.refund(turn)
                await guard.commit(turn, kind, ask, {
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                }, result.ok, result.elapsed_ms, result.error)

                if result.ok:
                    logger.info(
                        "进阶模型返回 turn=%s kind=%s %sms tokens=%s/%s%s",
                        turn.turn_id,
                        kind,
                        result.elapsed_ms,
                        result.prompt_tokens,
                        result.completion_tokens,
                        f" 存档={result.artifact.path}" if result.artifact else "",
                    )
                    # 引用的成败要告诉本地模型：它以为给了 8 个文件、实际只进了 2 个，
                    # 如果不讲，它会基于「进阶模型看过全部」这个错误前提继续干活。
                    tool_text = result.text
                    if refs_result.text or refs_result.rejected or refs_result.error:
                        tool_text = refs_result.notice() + "\n\n" + tool_text
                    merged_messages.append({"role": "tool", "tool_call_id": call_id, "content": tool_text})
                else:
                    logger.warning("进阶模型失败 turn=%s err=%s", turn.turn_id, result.error)
                    merged_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": f"[网关] 进阶模型调用未成功（{result.error}）。不要重试，用本地能力继续，并在答复里说明这一节没有外部输入。",
                        }
                    )

            if other_calls:
                final_message = {"role": "assistant", "content": content}
                if msg.get("reasoning_content"):
                    final_message["reasoning_content"] = msg["reasoning_content"]
                final_message["tool_calls"] = serialize_tool_calls(other_calls)
                finish_reason = "tool_calls"
                break

            if rounds >= cfg.guard.max_tool_rounds:
                logger.info("轮次用尽 turn=%s rounds=%s，强制收尾", turn.turn_id, rounds)
                # 合进 system，不追加新 system：Qwen 那类模板要求 system 只能在开头
                stop_msgs = inject_rules(merged_messages, force_finish)
                payload = {k: v for k, v in base_payload.items() if k != "tool_choice"}
                stop_msgs, stop_report = trimmer.fit(
                    stop_msgs,
                    budget=budget,
                    target=int(budget * cfg.context.target_ratio),
                    tools_tokens=0,
                    keep_recent=cfg.context.keep_recent,
                )
                last_report = stop_report
                stop_msgs = await maybe_spine(stop_msgs, stop_report, budget, 0)
                payload["messages"] = stop_msgs
                try:
                    try:
                        resp = await base.chat(payload)
                    except httpx.HTTPStatusError as exc:
                        if not is_overflow(exc):
                            raise
                        logger.warning("收尾调用仍溢出，硬截断后重试")
                        stop_msgs, stop_report = trimmer.fit(
                            stop_msgs,
                            budget=budget,
                            target=max(1024, int(budget * 0.4)),
                            tools_tokens=0,
                            keep_recent=cfg.context.keep_recent,
                            force=True,
                        )
                        last_report = stop_report
                        stop_msgs = await maybe_spine(stop_msgs, stop_report, budget, 0)
                        payload["messages"] = stop_msgs
                        resp = await base.chat(payload)
                except httpx.HTTPError as exc:
                    logger.error("收尾调用失败: %s", exc)
                    final_message = {
                        "role": "assistant",
                        "content": "[网关] 咨询轮次已用尽，且本地模型收尾失败。请检查本地服务后重试。",
                    }
                    finish_reason = "stop"
                    break
                last_resp = resp
                msg = ((resp.get("choices") or [{}])[0].get("message") or {})
                final_message = {"role": "assistant", "content": flatten_content(msg.get("content"))}
                if msg.get("reasoning_content"):
                    final_message["reasoning_content"] = msg["reasoning_content"]
                finish_reason = "stop"
                break

        if compacted:
            ctx["compactions"] += 1
        ctx["last"] = last_report.as_dict()

        snap = await guard.snapshot()
        extra = {
            "cloud_calls": turn.calls,
            "cloud_denied": turn.denied,
            "rounds": rounds,
            "key": turn.key_id,
            "remaining_today": snap["remaining_today"],
            "daily_limit": snap["daily_limit"],
            "context": last_report.as_dict(),
            "context_budget": budget,
            "truncated": truncated,
            "refs_files": refs_files,
        }
        usage = build_usage(last_resp, turn)
        headers = {
            "X-Steward-Calls": str(turn.calls),
            "X-Steward-Denied": str(turn.denied),
            "X-Steward-Remaining": "" if snap["remaining_today"] is None else str(snap["remaining_today"]),
            "X-Steward-Rounds": str(rounds),
            "X-Steward-Ctx": f"used={last_report.after} budget={budget} saved={last_report.saved}",
        }
        if refs_files:
            headers["X-Steward-Refs"] = str(refs_files)
        if truncated:
            headers["X-Steward-Truncated"] = "1"

        if not want_stream:
            return JSONResponse(
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": client_model,
                    "choices": [
                        {
                            "index": 0,
                            "message": final_message,
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": usage,
                    "x_steward": extra,
                },
                headers=headers,
            )

        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        content_text = str(final_message.get("content") or "")
        reasoning_text = str(final_message.get("reasoning_content") or "")
        out_tools = final_message.get("tool_calls") or []

        async def event_stream():
            def frame(delta: dict, finish: str | None = None) -> str:
                data = {
                    "id": cid,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": client_model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            yield frame({"role": "assistant"})
            for piece in chunk_text(reasoning_text):
                if piece:
                    yield frame({"reasoning_content": piece})
            for piece in chunk_text(content_text):
                if piece:
                    yield frame({"content": piece})
            for idx, tc in enumerate(out_tools):
                yield frame(
                    {
                        "tool_calls": [
                            {
                                "index": idx,
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["function"]["name"], "arguments": ""},
                            }
                        ]
                    }
                )
                args = tc["function"]["arguments"]
                for piece in chunk_text(args, 120):
                    yield frame({"tool_calls": [{"index": idx, "function": {"arguments": piece}}]})
            yield frame({}, finish_reason)
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
