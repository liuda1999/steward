# SPDX-License-Identifier: Apache-2.0
"""本地文件引用（refs）：让进阶模型直接读真实源码，而不是读模型的转述。

这是整个网关里**唯一一处主动把本地内容发出去**的通道，所以边界条件全部写在
这里，不散落在调用方：

  · 默认关闭，且开关在配置里，模型无法自己打开；
  · 根目录白名单，resolve 之后必须落在白名单内 —— 这一条同时防住了 `../..`
    和「符号链接指向白名单外面」，因为 resolve() 会把链接展平再判断；
  · 文件名黑名单，默认挡住密钥和凭据类文件；
  · 二进制拒读、单文件字节上限、总量字符上限、文件数上限；
  · 每次展开都写审计日志，记谁在什么时候读了哪几个文件。

设计意图是**克制**：refs 不是「把工作目录挂给云端」，而是「这一次委托允许它
看这几个文件」。授权粒度是单次调用，不是会话。
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("steward")

# 默认拒绝：密钥、凭据、版本库内部。故意不含 *token* —— 会误伤 tokenizer.py
DEFAULT_DENY: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.kdbx",
    "*.htpasswd",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*credential*",
    "*secret*",
    "*.netrc",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git",
    ".git/*",
    "*/.git/*",
    ".ssh",
    "*/.ssh/*",
)

_LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".ps1": "powershell",
    ".sql": "sql",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".ini": "ini",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".lua": "lua",
    ".vue": "vue",
    ".dart": "dart",
}

_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:[-~:]\s*(\d+))?\s*$")


@dataclass
class RefFile:
    """一个真的读到了的文件。"""

    path: str  # 相对白名单根目录的展示路径
    lines: int  # 文件总行数
    shown: int  # 实际给出去的行数
    chars: int
    truncated: bool = False
    binary: bool = False


@dataclass
class RefResult:
    """一次展开的结果。

    text 为空且 error 非空表示整体没成（未启用 / 没配白名单），
    这种情况下调用方应该把 error 告诉本地模型，让它别抱期望。
    rejected 里的每一条都是**单个文件**被拒的原因，其余文件照常给。
    """

    text: str = ""
    files: list[RefFile] = field(default_factory=list)
    chars: int = 0
    rejected: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.files) and not self.error

    def notice(self) -> str:
        """回灌给本地模型的一句话，让它知道哪些没读到。"""
        if self.error:
            return f"[refs] {self.error}，请把关键内容直接写进 brief。"
        parts = [f"[refs] 已附上 {len(self.files)} 个文件共 {self.chars:,} 字"]
        if self.rejected:
            parts.append("未附：" + "；".join(self.rejected[:5]))
        return "。".join(parts) + "。"


class _Reject(Exception):
    """单个引用不可用。消息直接进 rejected，要能给人看懂。"""


class Resolver:
    def __init__(self, cfg, audit_path: Path | None = None):
        self.cfg = cfg
        self.roots: list[Path] = []
        for raw in cfg.roots or []:
            try:
                root = Path(raw).expanduser().resolve()
            except OSError:
                logger.warning("refs.roots 里这一项无法解析，已跳过: %s", raw)
                continue
            if not root.exists():
                # 不存在时行为是「全部拒绝」，是安全的，但用户会一头雾水，这里先吵一声
                logger.warning("refs.roots 指向的目录不存在: %s（所有引用都会被拒）", root)
            self.roots.append(root)
        self.audit_path = audit_path
        self.deny = tuple(cfg.deny or DEFAULT_DENY)

    # ---------------------------------------------------------------- 对外

    def expand(self, specs, turn_id: str = "") -> RefResult:
        if not self.cfg.enabled:
            return RefResult(error="本地文件引用未启用（refs.enabled）")
        if not self.roots:
            return RefResult(error="未配置 refs.roots 白名单")
        if not isinstance(specs, list) or not specs:
            return RefResult()

        result = RefResult()
        used = 0
        seen: set[Path] = set()

        for spec in specs:
            if not isinstance(spec, dict):
                result.rejected.append("引用格式不对，应为对象")
                continue
            if len(result.files) >= self.cfg.max_files:
                result.rejected.append(f"已达文件数上限 {self.cfg.max_files}")
                break
            try:
                targets, note = self._targets(spec)
            except _Reject as exc:
                result.rejected.append(str(exc))
                continue
            if note:
                result.rejected.append(note)
            for target in targets:
                if len(result.files) >= self.cfg.max_files:
                    result.rejected.append(f"已达文件数上限 {self.cfg.max_files}")
                    break
                if target in seen:
                    continue
                seen.add(target)
                try:
                    block, info = self._render(target, spec, self.cfg.max_total_chars - used)
                except _Reject as exc:
                    result.rejected.append(str(exc))
                    continue
                if not block:
                    continue
                result.text += block
                result.files.append(info)
                used += info.chars
                if used >= self.cfg.max_total_chars:
                    result.rejected.append(f"已达总字数上限 {self.cfg.max_total_chars:,}")
                    break

        result.chars = used
        self._audit(turn_id, result)
        return result

    # ---------------------------------------------------------------- 取路径

    def _targets(self, spec: dict) -> tuple[list[Path], str]:
        """返回 (目标路径, 需要告诉模型的提示)。第二个元素为空表示没有话要说。"""
        if "glob" in spec:
            return self._glob(str(spec.get("glob") or ""), spec.get("max_files"))
        if "path" in spec:
            return [self._safe(str(spec.get("path") or ""))], ""
        raise _Reject("引用缺 path 或 glob")

    def _glob(self, pattern: str, max_files) -> tuple[list[Path], str]:
        pattern = pattern.strip()
        if not pattern:
            raise _Reject("glob 为空")
        # 绝对通配符只在某个白名单根之内才有意义，先按根拼相对来跑
        limit = self.cfg.max_files
        try:
            if max_files is not None:
                limit = min(limit, max(1, int(max_files)))
        except (TypeError, ValueError):
            pass

        out: list[Path] = []
        hit_limit = False
        for root in self.roots:
            for hit in sorted(root.glob(pattern)):
                if len(out) >= limit:
                    # 静默截断是最坏的：模型以为给了 8 个文件、实际 2 个，会基于错误前提干活
                    hit_limit = True
                    break
                if not hit.is_file():
                    continue
                try:
                    out.append(self._safe(str(hit)))
                except _Reject:
                    continue  # 通配符扫到的敏感文件直接跳过，不报错
            if hit_limit:
                break
        if not out:
            raise _Reject(f"glob 没有匹配到文件: {pattern}")
        note = f"glob {pattern} 命中超过 {limit} 个，只取了前 {len(out)} 个" if hit_limit else ""
        return out, note

    def _safe(self, raw: str) -> Path:
        raw = raw.strip()
        if not raw:
            raise _Reject("路径为空")
        if "\x00" in raw:
            raise _Reject("路径含非法字符")
        if len(raw) > 1024:
            raise _Reject("路径过长")

        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            # 相对路径按白名单根依次试，取第一个存在的
            found = None
            for root in self.roots:
                probe = root / candidate
                if probe.exists():
                    found = probe
                    break
            candidate = found or (self.roots[0] / candidate)

        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise _Reject(f"路径无法解析: {raw}（{exc}）") from exc

        # 这一条同时防住 ../.. 和「符号链接指向白名单外」：
        # resolve() 已经把链接展平，落在哪个根里面就是哪个根。
        rel = None
        for root in self.roots:
            if resolved == root or root in resolved.parents:
                rel = resolved.relative_to(root)
                break
        if rel is None:
            raise _Reject(f"不在白名单内: {raw}")

        shown = rel.as_posix() or resolved.name
        for pattern in self.deny:
            if fnmatch.fnmatch(shown, pattern) or fnmatch.fnmatch(resolved.name, pattern):
                raise _Reject(f"命中拒绝规则 {pattern}: {shown}")

        if not resolved.exists():
            raise _Reject(f"文件不存在: {shown}")
        if not resolved.is_file():
            raise _Reject(f"不是普通文件: {shown}")
        return resolved

    # ---------------------------------------------------------------- 读与画

    def _render(self, path: Path, spec: dict, remaining: int) -> tuple[str, RefFile]:
        if remaining <= 0:
            raise _Reject("已达总字数上限")

        try:
            raw = path.read_bytes()[: self.cfg.max_file_bytes + 1]
        except OSError as exc:
            raise _Reject(f"读取失败 {path.name}: {exc}") from exc

        head = raw[:8192]
        if b"\x00" in head:
            raise _Reject(f"二进制文件，不读: {path.name}")

        truncated = len(raw) > self.cfg.max_file_bytes
        if truncated:
            raw = raw[: self.cfg.max_file_bytes]
        text = raw.decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        total = len(all_lines)

        shown, first, last, numbered = self._select(all_lines, spec)
        if not shown:
            raise _Reject(f"没有可显示的内容: {path.name}")

        if numbered:
            body = "\n".join(f"{first + i:>5}  {line}" for i, line in enumerate(shown))
        else:
            body = "\n".join(shown)

        head_line = self._header(path, first, last, total, len(shown), numbered, truncated)
        lang = _LANG_BY_EXT.get(path.suffix.lower(), "")
        block = f"\n{head_line}\n```{lang}\n{body}\n```\n"

        # 单块超出剩余额度就截，保证总量可控
        if len(block) > remaining:
            cut = max(0, remaining - len(head_line) - 40)
            body = body[:cut] + "\n... [超出总量上限，已截断]"
            block = f"\n{head_line}\n```{lang}\n{body}\n```\n"
            truncated = True

        info = RefFile(
            path=self._shown(path),
            lines=total,
            shown=len(shown),
            chars=len(block),
            truncated=truncated,
        )
        return block, info

    def _select(self, lines: list[str], spec: dict):
        """返回 (要显示的行走, 起始行号, 结束行号, 是否带行号)。"""
        total = len(lines)
        grep = spec.get("grep")
        if isinstance(grep, str) and grep.strip():
            pat = grep.strip()
            ctx = max(0, int(self.cfg.grep_context))
            hits = [i for i, line in enumerate(lines) if pat.lower() in line.lower()]
            if not hits:
                raise _Reject(f"grep 无匹配: {pat[:40]}")
            spans: list[tuple[int, int]] = []
            for i in hits:
                lo, hi = max(0, i - ctx), min(total - 1, i + ctx)
                if spans and lo <= spans[-1][1] + 1:
                    spans[-1] = (spans[-1][0], max(spans[-1][1], hi))
                else:
                    spans.append((lo, hi))
            picked: list[str] = []
            for k, (lo, hi) in enumerate(spans):
                if k:
                    picked.append("      ...")
                picked.extend(lines[lo : hi + 1])
            first = spans[0][0] + 1
            last = spans[-1][1] + 1
            return picked, first, last, True

        rng = spec.get("range")
        if isinstance(rng, str) and rng.strip():
            match = _RANGE_RE.match(rng)
            if not match:
                raise _Reject(f"range 格式应为 \"120-260\"，收到: {rng[:20]}")
            start = max(1, int(match.group(1)))
            end = int(match.group(2)) if match.group(2) else start
            if end < start:
                start, end = end, start
            if start > total:
                raise _Reject(f"range 起点 {start} 超出文件行数 {total}")
            end = min(end, total)
            return lines[start - 1 : end], start, end, True

        return lines, 1, total, False

    def _header(self, path, first, last, total, shown, numbered, truncated) -> str:
        bits = [f"[ref] {self._shown(path)}"]
        if numbered:
            bits.append(f"第 {first}-{last} 行 / 共 {total} 行")
        else:
            bits.append(f"共 {total} 行")
        if truncated:
            bits.append("已截断")
        return " · ".join(bits)

    def _shown(self, path: Path) -> str:
        for root in self.roots:
            if path == root or root in path.parents:
                return path.relative_to(root).as_posix()
        return path.name

    # ---------------------------------------------------------------- 审计

    def _audit(self, turn_id: str, result: RefResult) -> None:
        if self.audit_path is None:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "turn": turn_id,
                "files": [f.path for f in result.files],
                "chars": result.chars,
                "rejected": result.rejected,
                "denied": self.deny is not None,
            }
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # 审计写不进去不该让委托失败，但要吵一声
            logger.warning("refs 审计日志写入失败: %s", exc)
