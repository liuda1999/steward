# SPDX-License-Identifier: Apache-2.0
"""Web 控制台：把「填两个槽位的 API」从改 YAML 变成点几下。

为什么不并进 `server.py`：路由（鉴权、工具循环）和「配置写盘」是两件事。
后者要处理注释保留、密钥分离、保存前校验这些麻烦事，塞在一起会让本来就
一千行的 server.py 更难读。

三个设计约束，都是踩过的坑决定的：

1. **不许丢掉注释。** config.yaml 是带注释的说明书，整份重写等于把它变成
   一台机器生成的配置。所以按行改值，注释和空行原样留着。
2. **密钥不进 config.yaml。** 那里是 `${BASE_API_KEY}` 占位，真实值写同目录
   的 `.env`（权限 0600）。控制台也遵守这条，不然一份配置文件被复制出去
   就带走两把上游 key。
3. **保存前先跑一遍启动校验。** 一个能存进去却起不来的配置，比不让存更糟：
   用户会以为改好了，直到重启才发现。

控制台本身要能防住两个东西：**别的网页跨站改配置**（CSRF）和**局域网里
别人点开就进**。前者靠 SameSite=Strict cookie + Origin 校验，后者靠一次性
口令 —— 口令在首次启动时生成，打印在终端里。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__
from .cloud import AdvancedClient
from .config import AppCfg, EndpointCfg, config_from_dict, is_loopback
from .envfile import read_env, write_env
from .keys import KeyStore
from .upstream import probe

logger = logging.getLogger(__name__)

COOKIE = "steward_console"
#: 页面把口令放在这个头里发。自定义头跨域会被浏览器先做 CORS 预检，
#: 而我们不发 CORS 头，所以别的站点就算知道地址也发不动 —— 这是主要的防跨站手段。
TOKEN_HEADER = "X-Console-Token"
TOKEN_NAME = "console.token"
PAGE = Path(__file__).resolve().parent / "web" / "console.html"

#: 界面上可以改的字段：段名 → {键: 类型}
#: 类型只影响前端控件和写盘时的序列化，不做额外校验（校验交给 AppCfg.validate）。
EDITABLE: dict[str, dict[str, str]] = {
    "base": {"base_url": "str", "model": "str", "protocol": "str", "thinking": "str", "timeout_s": "float"},
    "advanced": {"base_url": "str", "model": "str", "protocol": "str", "thinking": "str", "timeout_s": "float"},
    "guard": {"max_calls_per_turn": "int", "daily_limit": "int", "min_interval_s": "int"},
    "context": {"ctx_limit": "int", "target_ratio": "float", "spine": "bool"},
    "artifacts": {"max_age_days": "int", "max_total_mb": "int"},
    "refs": {"enabled": "bool", "roots": "list"},
    "server": {"host": "str", "port": "int"},
}

#: 写进 .env 的密钥字段：段名 → 环境变量名
SECRETS = {"base": "BASE_API_KEY", "advanced": "ADVANCED_API_KEY"}

#: 前端用这个标记表示「清空」，空字符串表示「不动」——
#: 否则界面上留空的密码框每次保存都会把已有的 key 抹掉。
CLEAR = "__CLEAR__"


class ConsoleError(Exception):
    """可以直接展示给用户看的错误。"""


# ---------------------------------------------------------------- 口令与登录


class Session:
    """控制台自己的会话。和项目 key 是两套东西：

    项目 key 是发给智能体的，控制台口令是给人用的。混用会出现
    「把智能体的 key 贴进网页就能改配置」，而那把 key 通常是明文写在
    客户端配置里的。
    """

    def __init__(self, path: Path):
        self.path = path
        self.token = self._load_or_create()
        self._fails: list[float] = []

    def _load_or_create(self) -> str:
        try:
            token = self.path.read_text(encoding="utf-8").strip()
            if token:
                return token
        except OSError:
            pass
        token = secrets.token_urlsafe(18)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(token + "\n", encoding="utf-8")
            os.chmod(self.path, 0o600)
        except OSError:
            logger.warning("控制台口令写不进 %s，本次会话用临时口令", self.path)
        return token

    def cookie_value(self) -> str:
        return hmac.new(self.token.encode(), b"steward-console-v1", "sha256").hexdigest()

    def authorized(self, request: Request) -> bool:
        """这个请求算不算已登录。

        两条路：
        - **请求头带口令**（页面里的主路径）。沙箱 iframe 存不了 cookie，
          SameSite=Strict 的 cookie 也不会跨站发送，所以只靠 cookie 的话，
          在预览面板这类环境里根本进不去。
        - **cookie**（普通浏览器的便利）。cookie 里存的是口令的 HMAC。
        """
        header = (request.headers.get(TOKEN_HEADER) or "").strip()
        if header and hmac.compare_digest(header, self.token):
            return True
        got = request.cookies.get(COOKIE, "")
        return bool(got) and hmac.compare_digest(got, self.cookie_value())

    def verify_token(self, token: str) -> bool:
        """带上限的比对：连错 8 次就锁一分钟。

        这是个能改配置、能读密钥是否存在 的入口，摆在 127.0.0.1 也不该
        允许无限次猜测 —— 本机上任何脚本都能试。
        """
        now = time.time()
        self._fails = [t for t in self._fails if now - t < 60]
        if len(self._fails) >= 8:
            raise ConsoleError("尝试次数过多，请等一分钟")
        if hmac.compare_digest(token, self.token):
            self._fails.clear()
            return True
        self._fails.append(now)
        return False


def origin_allowed(request: Request, cfg: AppCfg) -> bool:
    """挡跨站请求，但别把正当用法挡在外面。

    之前只认「Origin 的 netloc 等于 Host 头」，结果把两类正当访问误杀了：

    - **沙箱 iframe**（浏览器的预览面板、嵌入式工具）：Origin 是 `null`，
      因为页面处在不透明来源里；
    - **经本地代理访问**（预览面板常这么干）：Origin 的主机名和 Host 头对不上。

    现在放行三类：同源、`null`（不透明来源）、以及本机来源（回环地址 + 我们的端口）。
    真正要挡的是 `https://evil.example` 这种 —— 它的 Origin 永远改不成上面三类，
    而且自定义请求头跨域要先过 CORS 预检，我们不发 CORS 头，浏览器直接拦掉。
    """
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return True  # 非浏览器（curl、脚本），它们本来也拿不到浏览器的凭据
    if origin == "null":
        return True  # 沙箱 iframe：不透明来源
    netloc = origin.split("://", 1)[-1]
    if netloc == request.headers.get("host", ""):
        return True  # 同源
    host, _, port = netloc.rpartition(":")
    host = host.strip("[]")
    if port.isdigit() and int(port) == int(cfg.server.port) and is_loopback(host):
        return True  # 本机来源（经代理访问本机端口）
    return False


# ---------------------------------------------------------------- 读写配置


def _scalar(value: Any) -> str:
    """用 yaml 自己的规则序列化单个值，省得手写引号规则。"""
    text = yaml.safe_dump(value, allow_unicode=True, default_flow_style=True, width=10**6).strip()
    return text.rstrip("...").strip() if text.endswith("...") else text


_VALUE_WITH_COMMENT = re.compile(r"^(?P<head>\s*(?P<key>[A-Za-z_][\w-]*)\s*:\s*)(?P<value>.*?)(?P<comment>\s+#.*)?$")


def set_in_text(text: str, section: str, key: str, value: Any) -> str:
    """在 YAML 文本里改一个值，保留缩进、注释、空行和别的字段顺序。

    段里没有这个键时**补一行**而不是放弃 —— 旧配置里缺的字段（比如
    timeout_s）如果只能改已有的键，界面上填了却静默无效，用户只能靠猜。
    补出来的行放在该段末尾，缩进跟同段其它键一致。
    """
    lines = text.splitlines()
    in_section = False
    section_start = -1
    section_end = len(lines)
    indent_hint = "  "

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        if indent == 0 and (stripped.endswith(":") or re.match(r"^[A-Za-z_][\w-]*:", stripped)):
            name = stripped.split(":", 1)[0].strip()
            if in_section:
                section_end = i
                break
            in_section = name == section
            if in_section:
                section_start = i
            continue
        if in_section and indent > 0:
            indent_hint = " " * indent

        if not in_section or indent == 0:
            continue

        m = _VALUE_WITH_COMMENT.match(line)
        if not m or m.group("key") != key:
            continue
        head = m.group("head")
        comment = m.group("comment") or ""
        lines[i] = f"{head}{_scalar(value)}{comment}"
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    # 段里没有这个键：补一行
    if section_start < 0:
        return text  # 连这个段都没有 —— 不乱加，交给写盘前校验去报错
    insert_at = section_end
    while insert_at > section_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, f"{indent_hint}{key}: {_scalar(value)}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _section_dict(data: dict, name: str) -> dict:
    got = data.get(name)
    return got if isinstance(got, dict) else {}


_MISSING = object()


def _equivalent(old: Any, new: Any) -> bool:
    """旧值和新值算不算同一个东西。

    两边类型来源不同：旧值来自 YAML（可能是 int / float / bool / None），
    新值来自表单（数字控件给的是 number，文本控件给的是 str）。只比最终
    语义，不比类型，否则 "600" 和 600 会被当成一次改动。
    """
    if old is _MISSING:
        return False
    if isinstance(old, bool) or isinstance(new, bool):
        return bool(old) == bool(new)
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return float(old) == float(new)
    if isinstance(old, list) or isinstance(new, list):
        return [str(x) for x in (old or [])] == [str(x) for x in (new or [])]
    if old is None:
        return new in ("", None)
    return str(old) == str(new)


def apply_changes(
    cfg_path: Path,
    env_path: Path,
    payload: dict,
    *,
    current: dict | None = None,
) -> dict:
    """把前端提交的改动写进 config.yaml 与 .env。

    先在一份内存副本上改 + 校验，通过了才落盘。落盘用临时文件 + 原子替换，
    中途断电不会留下半份配置。
    """
    try:
        original = cfg_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConsoleError(f"读不到配置文件 {cfg_path}：{exc}") from exc

    text = original
    changed: list[str] = []
    env_updates: dict[str, str] = {}
    removed_env: list[str] = []

    # 拿旧值做比对：界面是「整份表单提交」，不比对的话每次保存都会把
    # 22 个字段全报成改动，用户改一个数字看到「共 22 项」只会以为出了错，
    # 而且没改的行也被重写一遍（注释虽然保住了，但 diff 会变脏）。
    try:
        doc = yaml.safe_load(original) or {}
    except yaml.YAMLError:
        doc = {}

    for section, fields in EDITABLE.items():
        got = payload.get(section)
        if not isinstance(got, dict):
            continue
        for key, kind in fields.items():
            if key not in got:
                continue
            raw = got[key]
            value = _coerce(kind, raw, f"{section}.{key}")
            current_value = (doc.get(section) or {}).get(key, _MISSING) if isinstance(doc.get(section), dict) else _MISSING
            if not _equivalent(current_value, value):
                text = set_in_text(text, section, key, value)
                changed.append(f"{section}.{key}")
                if not isinstance(doc.get(section), dict):
                    doc[section] = {}
                doc[section][key] = value

    for section, env_name in SECRETS.items():
        got = payload.get(section)
        if not isinstance(got, dict):
            continue
        secret = got.get("api_key")
        if not isinstance(secret, str) or not secret:
            continue  # 空 = 不改
        if secret == CLEAR:
            removed_env.append(env_name)
        else:
            env_updates[env_name] = secret.strip()
        changed.append(f"{section}.api_key")

    # 写盘前校验：能存进去却起不来的配置比不让存更糟
    try:
        candidate = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConsoleError(f"生成的 YAML 不合法，已放弃保存：{exc}") from exc

    cfg = config_from_dict(candidate)
    problems = cfg.validate()
    if problems:
        raise ConsoleError("配置还不能启动：" + "；".join(problems))

    _atomic_write(cfg_path, text)
    if env_updates or removed_env:
        write_env(env_path, env_updates, drop=removed_env)

    return {
        "changed": changed,
        "restart_required": True,
        "warnings": cfg.warnings(),
        "config_path": str(cfg_path),
    }


def _coerce(kind: str, raw: Any, where: str) -> Any:
    if kind == "str":
        if not isinstance(raw, str):
            raise ConsoleError(f"{where} 需要字符串")
        return raw.strip()
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ConsoleError(f"{where} 需要整数") from exc
    if kind == "float":
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ConsoleError(f"{where} 需要数字") from exc
    if kind == "bool":
        return bool(raw)
    if kind == "list":
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise ConsoleError(f"{where} 需要字符串数组")
        return [x.strip() for x in raw if x.strip()]
    raise ConsoleError(f"{where} 的类型 {kind} 不认识")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 状态


def display_cfg(cfg: AppCfg, cfg_path: Path) -> AppCfg:
    """读盘上的配置来展示，而不是内存里那份。

    控制台改的是文件，改完要重启才生效。如果界面回显内存里的旧值，
    用户会看到"刚存的地址又空了"，然后存第二遍 —— 这比不显示更糟。
    文件读不出来（手改坏了）就退回内存里的，至少界面还能打开。
    """
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    try:
        return config_from_dict(data)
    except Exception:  # noqa: BLE001 - 任何解析失败都不该让界面挂掉
        return cfg


def public_state(cfg: AppCfg, cfg_path: Path, env_path: Path, keys: KeyStore) -> dict:
    """给界面看的状态。密钥只回报「有没有」，绝不回报值。

    `has_key` 要同时看内存里的配置和磁盘上的 .env：刚在控制台存进去的 key
    还没经过重启，cfg 里是旧的；只看 cfg 会让用户以为没存上，于是再存一遍。
    """
    on_disk = read_env(env_path)

    def slot(ep: EndpointCfg, env_name: str) -> dict:
        return {
            "base_url": ep.base_url,
            "model": ep.model,
            "protocol": ep.protocol,
            "thinking": ep.thinking,
            "has_key": bool(ep.api_key) or bool(on_disk.get(env_name)),
            "configured": bool(ep.base_url and ep.model),
            "timeout_s": ep.timeout_s,
        }

    data = {
        "version": __version__,
        "config_path": str(cfg_path),
        "env_path": str(env_path),
        "env_exists": env_path.exists(),
        "slots": {
            "base": slot(cfg.base, SECRETS["base"]),
            "advanced": slot(cfg.advanced, SECRETS["advanced"]),
        },
        "guard": asdict(cfg.guard)
        | {"remaining_today": None},
        "context": {
            "ctx_limit": cfg.context.ctx_limit,
            "target_ratio": cfg.context.target_ratio,
            "spine": cfg.context.spine,
        },
        "artifacts": {
            "enabled": cfg.artifacts.enabled,
            "dir": cfg.artifacts.dir,
            "max_age_days": cfg.artifacts.max_age_days,
            "max_total_mb": cfg.artifacts.max_total_mb,
        },
        "refs": {"enabled": cfg.refs.enabled, "roots": cfg.refs.roots},
        "server": {"host": cfg.server.host, "port": cfg.server.port, "loopback": is_loopback(cfg.server.host)},
        "auth": {"mode": cfg.auth.mode, "keys": keys.summary()},
        "warnings": cfg.warnings(),
    }
    return data


# ---------------------------------------------------------------- 路由


def install(app, cfg: AppCfg, cfg_path: Path, keys: KeyStore, page: Path | None = None) -> Session:
    """把控制台挂到 app 上，返回会话对象（启动时要用它打印口令）。"""
    session = Session(Path(cfg.auth.keys_file).parent / TOKEN_NAME)
    html_path = page or PAGE
    router = APIRouter()

    def guard(request: Request) -> None:
        if not origin_allowed(request, cfg):
            raise ConsoleError("来源校验失败：只接受本机或同源发起的请求")
        if not session.authorized(request):
            raise ConsoleError("未登录")

    def fail(exc: ConsoleError, status: int = 400) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=status)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        try:
            html = html_path.read_text(encoding="utf-8")
        except OSError:
            return HTMLResponse("<h1>控制台页面缺失</h1><p>找不到 steward/web/console.html</p>", status_code=500)
        return HTMLResponse(html)

    @app.get("/logo.png")
    async def logo():
        """页眉与 favicon 用的标识图。

        和 console.html 放同一目录：换页面（测试用临时目录）时图片也跟着走，
        不会出现「页面在、图不在」。文件缺失只影响观感，不该 500。
        """
        path = html_path.parent / "logo.png"
        if not path.is_file():
            return JSONResponse({"ok": False, "error": "logo.png 不存在"}, status_code=404)
        return FileResponse(path, media_type="image/png")

    @router.post("/login")
    async def login(request: Request) -> JSONResponse:
        if not origin_allowed(request, cfg):
            return fail(ConsoleError("来源校验失败"), 403)
        body = await request.json()
        try:
            ok = session.verify_token(str(body.get("token") or "").strip())
        except ConsoleError as exc:
            return fail(exc, 429)
        if not ok:
            return fail(ConsoleError("口令不对"), 401)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            COOKIE,
            session.cookie_value(),
            max_age=7 * 86400,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return resp

    @router.post("/logout")
    async def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @router.get("/state")
    async def state(request: Request) -> JSONResponse:
        if not session.authorized(request):
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        return JSONResponse(
            {"ok": True, "data": public_state(display_cfg(cfg, cfg_path), cfg_path, cfg_path.parent / ".env", keys)}
        )

    @router.post("/save")
    async def save(request: Request) -> JSONResponse:
        try:
            guard(request)
        except ConsoleError as exc:
            return fail(exc, 403 if "来源" in str(exc) else 401)
        body = await request.json()
        try:
            result = apply_changes(cfg_path, cfg_path.parent / ".env", body.get("values") or {})
        except ConsoleError as exc:
            return fail(exc)
        logger.info("控制台改写了配置：%s（重启后生效）", "、".join(result["changed"]) or "无变化")
        return JSONResponse({"ok": True, "data": result})

    @router.post("/test")
    async def test_slot(request: Request) -> JSONResponse:
        """拿界面上填的、还没保存的值去探一次上游。

        先测再存，比存完重启发现打不通好。这里只用只读的 /v1/models，
        不产生推理开销。
        """
        try:
            guard(request)
        except ConsoleError as exc:
            return fail(exc, 403 if "来源" in str(exc) else 401)
        body = await request.json()
        slot = str(body.get("slot") or "")
        if slot not in SECRETS:
            return fail(ConsoleError("槽位只能是 base 或 advanced"))
        raw = body.get("values") or {}
        if not isinstance(raw, dict):
            return fail(ConsoleError("values 需要对象"))
        # 输入框留空的意思是「不改」，不是「清空」——测试连接也得照这个来。
        # 否则界面上明明显示「已设置」，点一下测试却因为没有 key 得到 401，
        # 用户会以为是自己填错了地址。
        stored = cfg.base if slot == "base" else cfg.advanced
        api_key = str(raw.get("api_key") or "").strip() or (stored.api_key or "")
        ep = EndpointCfg(
            base_url=str(raw.get("base_url") or "").strip(),
            model=str(raw.get("model") or "").strip(),
            api_key=api_key,
            protocol=str(raw.get("protocol") or "chat"),
        )
        if not ep.base_url:
            return fail(ConsoleError("先填地址"))
        info = await asyncio.to_thread(probe, ep, 8.0)
        return JSONResponse(
            {
                "ok": True,
                "data": {
                    "reachable": bool(info.get("ok")),
                    "detail": info.get("detail") or "",
                    "status": info.get("status"),
                    "models": (info.get("models") or [])[:8],
                    "context": info.get("context"),
                    "slots": info.get("slots"),
                    "model_known": (ep.model in (info.get("models") or [])) if info.get("models") else None,
                },
            }
        )

    @router.post("/restart")
    async def restart(request: Request) -> JSONResponse:
        """改完配置立即重启。

        走的是**同进程重启**：标记一下，让 serve 循环在 uvicorn 退出后
        重新读配置再起一次。端口在两次服务之间就释放了，不会和「还没退
        干净的老进程」抢同一个 socket，日志也还在用户原来那个终端里。

        派生新进程的做法看着更常规，但那会让两个进程抢端口，还要处理
        孤儿进程和日志去向 —— 为了一次重启引入这些不值当。
        """
        try:
            guard(request)
        except ConsoleError as exc:
            return fail(exc, 403 if "来源" in str(exc) else 401)

        server = getattr(request.app.state, "uvicorn_server", None)
        if server is None:
            return fail(
                ConsoleError(
                    "这个进程不是以 serve 方式启动的，没法自动重启；"
                    "请手动重启网关（或用 python -m steward serve 启动它）"
                ),
                409,
            )

        from .server import request_restart

        request_restart()

        async def stop_soon() -> None:
            # 先让响应发出去，再关服务；否则浏览器只看到一个断连，
            # 分不清是「重启了」还是「崩了」
            await asyncio.sleep(0.3)
            server.should_exit = True

        asyncio.create_task(stop_soon())
        return JSONResponse({"ok": True, "data": {"restarting": True}})

    @router.get("/keys")
    async def list_keys(request: Request) -> JSONResponse:
        if not session.authorized(request):
            return JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
        return JSONResponse({"ok": True, "data": [k.public() for k in keys.all()]})

    @router.post("/keys/new")
    async def new_key(request: Request) -> JSONResponse:
        try:
            guard(request)
        except ConsoleError as exc:
            return fail(exc, 403 if "来源" in str(exc) else 401)
        body = await request.json()
        label = str(body.get("label") or "").strip() or "console"
        token, rec = keys.issue(label)
        logger.info("控制台签发项目 key：%s", label)
        # 明文只在这一刻返回一次，之后库里只有 hash
        return JSONResponse({"ok": True, "data": {"id": rec.id, "label": rec.label, "token": token}})

    @router.post("/keys/revoke")
    async def revoke_key(request: Request) -> JSONResponse:
        try:
            guard(request)
        except ConsoleError as exc:
            return fail(exc, 403 if "来源" in str(exc) else 401)
        body = await request.json()
        ok = keys.revoke(str(body.get("id") or ""))
        return JSONResponse({"ok": ok, "error": "" if ok else "没有这把 key"})

    app.include_router(router, prefix="/console/api")
    return session
