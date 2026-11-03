"""配置加载：YAML + 环境变量展开。

优先级：命令行 > 环境变量 > config.yaml > 内置默认值。

两个模型槽位：
  base      基础模型，日常主力，绝大多数请求由它完成
  advanced  进阶模型，只在复杂任务时被调用，受配额限制

命名不按"本地/云端"，因为两者都可能是任意 OpenAI 兼容端点。
旧配置里的 local / cloud 段会自动当作 base / advanced 读入。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        return os.environ.get(name, default if default is not None else "")

    return _ENV_RE.sub(repl, value)


@dataclass
class ServerCfg:
    host: str = "127.0.0.1"
    port: int = 8010
    api_key: str = ""
    log_level: str = "info"


@dataclass
class AuthCfg:
    """下游鉴权：网关自己签发的 key。

    mode=required 时，客户端必须带网关签发的 key；mode=off 仅供本机调试。
    server.api_key 仍然可用，作为兼容旧配置的静态 key。
    """

    mode: str = "required"
    keys_file: str = "state/keys.json"
    auto_issue: bool = True


@dataclass
class EndpointCfg:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    protocol: str = "chat"  # chat | responses
    timeout_s: float = 300.0
    max_tokens: int = 4096
    min_max_tokens: int = 1024  # 客户端给的 max_tokens 小于此值时抬上去，防推理 token 吃掉预算
    retry_404: bool = True  # 上游对复用连接返回 404 时，换短连接重试一次
    thinking: str = "auto"  # auto | off | on，对推理型模型发 chat_template_kwargs.enable_thinking
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class GuardCfg:
    max_calls_per_turn: int = 2
    max_tool_rounds: int = 4
    min_interval_s: float = 30.0
    daily_limit: int = 5
    dedupe: bool = True
    similarity: float = 0.92
    max_brief_chars: int = 6000
    max_result_chars: int = 16000
    state_file: str = "state/usage.json"


@dataclass
class ContextCfg:
    """上下文治理。

    usable_ratio 是「名义窗口里真正能用的比例」——30B 级模型在长上下文上
    的有效区间通常只有 50–60%，按标称值塞满会得到自信的胡编。
    target_ratio 决定一次裁剪砍多深：砍得越深，两次裁剪之间撑得越久，
    但每次要重算的前缀也越多（实测 prefill 约 1300 tok/s）。
    """

    enabled: bool = True
    ctx_limit: int = 0  # 0 = 自动探测
    usable_ratio: float = 0.85
    target_ratio: float = 0.65
    min_clear_tokens: int = 8000  # 一次裁剪至少清这么多，否则不值得让缓存失效
    reserve_output: bool = True  # 从预算里扣掉 max_tokens，留出生成空间
    safety_tokens: int = 512
    strip_reasoning: bool = True
    strip_images: bool = True
    tool_digest: bool = True
    tool_head_lines: int = 40
    tool_tail_lines: int = 40
    keep_recent: int = 4  # 最近几条永不裁剪
    overflow_retry: bool = True
    state_file: str = "state/ctx.json"
    # ---- 任务骨架 ----
    # 裁剪把中间历史删掉之后，模型只能看到「压缩了 N 条」这个计数。
    # 骨架把那片区域提炼成 6 行留在 system 里，代价是裁剪时多一次本地调用。
    spine: bool = True
    spine_min_dropped: int = 4000  # 丢得少于这个 token 数就不值得为它调一次模型
    spine_max_chars: int = 12000  # 喂给提炼模型的原文上限
    spine_timeout_s: float = 30.0
    spine_max_tokens: int = 900
    spine_cache_size: int = 64
    spine_state_file: str = "state/spine.json"


@dataclass
class ArtifactsCfg:
    """交付物存档。默认落在客户端工作目录内，方便智能体直接读。"""

    enabled: bool = True
    dir: str = ".steward/artifacts"
    inline_chars: int = 800  # 回灌摘要的长度，约 500 tokens
    min_store_chars: int = 1500  # 短于此的回复不值得存档
    toc: bool = True
    # 摘要里给智能体的路径是绝对路径还是相对路径。
    # 相对路径的基准是网关进程的 cwd，只有「网关和智能体同一个工作目录」时才成立，
    # 默认用绝对路径：多花 20 来个 token，换掉一整类「文件打不开」的静默失败。
    absolute: bool = True
    # ---- 保留与回收 ----
    # 存档是只增不减的东西，长期跑必然涨。两个上限取先到者：
    max_age_days: int = 30  # 0 表示不按时间清
    max_total_mb: int = 512  # 0 表示不按容量清
    clean_interval_s: int = 600  # 两次清理之间至少隔这么久，别每次写都扫盘
    index_max_lines: int = 5000  # index.jsonl 的保留行数
    # 一次裁剪会压掉上百条工具输出，逐条落盘会写出上百个文件。
    # 合并成一个文件，用行锚点定位，目录也干净。
    batch_max_chars: int = 500_000


@dataclass
class RefsCfg:
    """本地文件引用。默认关闭 —— 这是唯一会把本地内容主动发出去的通道。"""

    enabled: bool = False
    roots: list[str] = field(default_factory=list)  # 白名单根目录，必须绝对路径
    max_file_bytes: int = 65536  # 单文件上限，超了读前缀并标注截断
    max_total_chars: int = 120000  # 一次委托所有引用合计上限（约 38K tokens）
    max_files: int = 20
    grep_context: int = 3  # grep 命中行上下各带几行
    deny: list[str] = field(default_factory=list)  # 空则用内置黑名单
    audit_file: str = "state/refs.jsonl"


@dataclass
class PromptCfg:
    system_text: str = ""
    tool_description: str = ""
    force_finish_text: str = ""


LOOPBACK = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1", "[::1]"}


def is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in LOOPBACK


@dataclass
class AppCfg:
    server: ServerCfg = field(default_factory=ServerCfg)
    auth: AuthCfg = field(default_factory=AuthCfg)
    base: EndpointCfg = field(default_factory=EndpointCfg)
    advanced: EndpointCfg = field(default_factory=EndpointCfg)
    guard: GuardCfg = field(default_factory=GuardCfg)
    context: ContextCfg = field(default_factory=ContextCfg)
    artifacts: ArtifactsCfg = field(default_factory=ArtifactsCfg)
    refs: RefsCfg = field(default_factory=RefsCfg)
    prompt: PromptCfg = field(default_factory=PromptCfg)
    public_model: str = "auto"
    model_aliases: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.auth.mode not in ("required", "off"):
            problems.append("auth.mode 只能是 required 或 off")
        if not is_loopback(self.server.host) and self.auth.mode != "required":
            problems.append(
                f"server.host={self.server.host} 是对外监听，但 auth.mode=off，"
                "局域网里任何人都能用掉你的配额；请改成 required"
            )
        for name, ep in (("base", self.base), ("advanced", self.advanced)):
            if ep.protocol not in ("chat", "responses"):
                problems.append(f"{name}.protocol 只能是 chat 或 responses")
            if ep.thinking not in ("auto", "off", "on"):
                problems.append(f"{name}.thinking 只能是 auto / off / on")
        if not self.base.base_url:
            problems.append("base.base_url 未配置（基础模型）")
        if not self.base.model:
            problems.append("base.model 未配置（基础模型）")
        if not self.advanced.base_url:
            problems.append("advanced.base_url 未配置（进阶模型，不配则无法上云）")
        if not self.advanced.model:
            problems.append("advanced.model 未配置（进阶模型）")
        if self.guard.max_calls_per_turn < 0:
            problems.append("guard.max_calls_per_turn 不能为负")
        if self.guard.max_tool_rounds < 1:
            problems.append("guard.max_tool_rounds 至少为 1")
        if not 0.3 <= self.context.usable_ratio <= 1.0:
            problems.append("context.usable_ratio 应在 0.3–1.0")
        if self.context.target_ratio >= self.context.usable_ratio:
            problems.append("context.target_ratio 应小于 usable_ratio（否则压不到水位以下）")
        if self.context.keep_recent < 1:
            problems.append("context.keep_recent 至少为 1")
        if self.artifacts.enabled and not self.artifacts.dir:
            problems.append("artifacts.dir 不能为空")
        if self.artifacts.max_age_days < 0:
            problems.append("artifacts.max_age_days 不能为负（0 表示不按时间清）")
        if self.artifacts.max_total_mb < 0:
            problems.append("artifacts.max_total_mb 不能为负（0 表示不按容量清）")
        if self.artifacts.batch_max_chars < 1024:
            problems.append("artifacts.batch_max_chars 至少 1024，否则一次裁剪会切出很多小文件")
        if self.context.spine_min_dropped < 0:
            problems.append("context.spine_min_dropped 不能为负")
        if self.refs.enabled:
            # 开了 refs 却没白名单，等于「不设防的文件外发通道」，宁可起不来
            if not self.refs.roots:
                problems.append("refs.enabled=true 但 refs.roots 为空，必须显式配白名单")
            for raw in self.refs.roots:
                if not os.path.isabs(os.path.expanduser(raw)):
                    problems.append(f"refs.roots 需要绝对路径: {raw}")
            if self.refs.max_files < 1:
                problems.append("refs.max_files 至少为 1")
            if self.refs.max_total_chars < 1000:
                problems.append("refs.max_total_chars 过小，至少 1000")
        return problems


def _section(cls: type, data: Any):
    if not isinstance(data, dict):
        return cls()
    known = {f.name for f in fields(cls)}
    kwargs = {k: _expand(v) for k, v in data.items() if k in known}
    return cls(**kwargs)


def _pick(data: dict, *names: str) -> Any:
    """按顺序取第一个存在的段，用于兼容旧字段名。"""
    for name in names:
        if isinstance(data.get(name), dict):
            return data[name]
    return {}


def find_config() -> Path:
    env_path = os.environ.get("STEWARD_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve()
    for name in ("config.yaml", "config.example.yaml"):
        candidate = Path.cwd() / name
        if candidate.exists():
            return candidate
    return Path.cwd() / "config.yaml"


def load_env_file(path: Path) -> None:
    """读同目录 .env。已存在的环境变量优先，不会被覆盖。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


def load_config(path: str | Path | None = None) -> AppCfg:
    cfg_path = Path(path).expanduser().resolve() if path else find_config()
    load_env_file(cfg_path.parent / ".env")
    data: dict = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    cfg = AppCfg(
        server=_section(ServerCfg, data.get("server")),
        auth=_section(AuthCfg, data.get("auth")),
        base=_section(EndpointCfg, _pick(data, "base", "local")),
        advanced=_section(EndpointCfg, _pick(data, "advanced", "cloud")),
        guard=_section(GuardCfg, data.get("guard")),
        context=_section(ContextCfg, data.get("context")),
        artifacts=_section(ArtifactsCfg, data.get("artifacts")),
        refs=_section(RefsCfg, data.get("refs")),
        prompt=_section(PromptCfg, data.get("prompt")),
        public_model=str(_expand(data.get("public_model", "auto")) or "auto"),
        model_aliases=[str(_expand(x)) for x in (data.get("model_aliases") or [])],
    )

    for key, attr, caster in (
        ("STEWARD_HOST", "host", str),
        ("STEWARD_PORT", "port", int),
        ("STEWARD_API_KEY", "api_key", str),
    ):
        if os.environ.get(key):
            setattr(cfg.server, attr, caster(os.environ[key]))

    # 旧名 LOCAL_/CLOUD_ 先读，新名 BASE_/ADVANCED_ 覆盖
    slots = (
        ("LOCAL", cfg.base),
        ("CLOUD", cfg.advanced),
        ("BASE", cfg.base),
        ("ADVANCED", cfg.advanced),
    )
    for prefix, target in slots:
        for key, attr, caster in (
            (f"{prefix}_BASE_URL", "base_url", str),
            (f"{prefix}_MODEL", "model", str),
            (f"{prefix}_API_KEY", "api_key", str),
        ):
            if os.environ.get(key):
                setattr(target, attr, caster(os.environ[key]))

    if os.environ.get("STEWARD_STATE_FILE"):
        cfg.guard.state_file = os.environ["STEWARD_STATE_FILE"]
    if os.environ.get("STEWARD_AUTH_MODE"):
        cfg.auth.mode = os.environ["STEWARD_AUTH_MODE"]
    if os.environ.get("STEWARD_KEYS_FILE"):
        cfg.auth.keys_file = os.environ["STEWARD_KEYS_FILE"]

    # YAML 1.1 把 off/on 当布尔，用户写 mode: off 会变成 False
    if isinstance(cfg.auth.mode, bool):
        cfg.auth.mode = "required" if cfg.auth.mode else "off"
    else:
        cfg.auth.mode = str(cfg.auth.mode).strip().lower()

    for slot in (cfg.base, cfg.advanced):
        if isinstance(slot.thinking, bool):
            slot.thinking = "on" if slot.thinking else "off"
        else:
            slot.thinking = str(slot.thinking).strip().lower()

    # 数值项防呆：YAML 里写成字符串（"0.85"）也能用
    for name, caster in (
        ("ctx_limit", int),
        ("min_clear_tokens", int),
        ("safety_tokens", int),
        ("tool_head_lines", int),
        ("tool_tail_lines", int),
        ("keep_recent", int),
        ("usable_ratio", float),
        ("target_ratio", float),
    ):
        try:
            setattr(cfg.context, name, caster(getattr(cfg.context, name)))
        except (TypeError, ValueError):
            pass
    cfg.context.usable_ratio = min(1.0, max(0.3, cfg.context.usable_ratio))
    cfg.context.target_ratio = min(cfg.context.usable_ratio, max(0.2, cfg.context.target_ratio))
    for name in (
        "inline_chars",
        "min_store_chars",
        "max_age_days",
        "max_total_mb",
        "clean_interval_s",
        "index_max_lines",
        "batch_max_chars",
    ):
        try:
            setattr(cfg.artifacts, name, int(getattr(cfg.artifacts, name)))
        except (TypeError, ValueError):
            pass
    for name in ("max_file_bytes", "max_total_chars", "max_files", "grep_context"):
        try:
            setattr(cfg.refs, name, int(getattr(cfg.refs, name)))
        except (TypeError, ValueError):
            pass
    if isinstance(cfg.refs.roots, str):
        cfg.refs.roots = [cfg.refs.roots]
    if isinstance(cfg.refs.deny, str):
        cfg.refs.deny = [cfg.refs.deny]
    # roots 里的相对路径一律按配置文件目录解析，免得受启动时 cwd 影响
    cfg.refs.roots = [
        str((cfg_path.parent / Path(str(r)).expanduser()).resolve())
        if not Path(str(r)).expanduser().is_absolute()
        else str(Path(str(r)).expanduser().resolve())
        for r in (cfg.refs.roots or [])
    ]
    refs_audit = Path(cfg.refs.audit_file)
    if not refs_audit.is_absolute():
        cfg.refs.audit_file = str((cfg_path.parent / refs_audit).resolve())

    for attr in ("state_file", "keys_file"):
        target = cfg.guard if attr == "state_file" else cfg.auth
        value = Path(getattr(target, attr))
        if not value.is_absolute():
            setattr(target, attr, str((cfg_path.parent / value).resolve()))

    for name in ("state_file", "spine_state_file"):
        value = Path(getattr(cfg.context, name))
        if not value.is_absolute():
            setattr(cfg.context, name, str((cfg_path.parent / value).resolve()))

    return cfg
