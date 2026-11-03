# SPDX-License-Identifier: Apache-2.0
"""交付物外置：全文落盘，上下文只留摘要 + 目录 + 路径。

为什么值得单独做一层：一次 consult 的交付物 16K 字符 ≈ 11K tokens，作为
role=tool 永久驻留。客户端每轮重发，每次压缩都要重新处理，而本地模型真正
用到的往往只有其中一节。把它从"聊天消息"改成"存档 + 指针"，上下文成本降
一个数量级，而且全文可以反复读回，不用再花第二次配额。

存档位置默认在客户端工作目录内的 .steward/artifacts，这样智能体自己的
文件读取工具能直接打开。放在网关 state 下，客户端的沙箱可能读不到。

三条约定，都是为了「长期跑不出事」：
  1 每个文件都有统一抬头（id/kind/turn/created/chars），自描述，脚本可扫。
  2 路径只由「日期 + 类型 + 内容 hash」组成，同内容跨轮次不重复落盘。
  3 定期按天回收：超龄的、超总量的先删，index 也有行数上限。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import ArtifactsCfg
from .context import count_tokens

logger = logging.getLogger("steward")

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_DAY_RE = re.compile(r"^\d{8}$")
_MAX_TOC = 12


@dataclass
class Artifact:
    id: str
    path: str
    abs_path: str
    chars: int
    tokens: int
    kind: str
    created: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "chars": self.chars,
            "tokens": self.tokens,
            "kind": self.kind,
            "created": self.created,
        }


def table_of_contents(text: str, limit: int = _MAX_TOC) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if not match:
            continue
        title = match.group(2).strip()
        if not title:
            continue
        items.append(title)
        if len(items) >= limit:
            break
    return items


def _header(kind: str, turn: str, chars: int, extra: dict | None = None) -> str:
    """统一的 YAML 抬头。人翻得懂，脚本也扫得动。"""
    lines = [
        "---",
        f"kind: {kind}",
        f"turn: {turn}",
        f"created: {datetime.now().isoformat(timespec='seconds')}",
        f"chars: {chars}",
    ]
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


class Store:
    def __init__(self, cfg: ArtifactsCfg):
        self.cfg = cfg
        # 相对路径的基准是**网关进程的 cwd**，不是智能体的 cwd。
        # 两者一致（网关就起在项目目录里）才碰巧对得上，所以这里一次性定死绝对路径。
        self.root = Path(cfg.dir).expanduser()
        try:
            self.root = self.root.resolve()
        except OSError:
            pass
        self._last_clean = 0.0

    # ---- 写 ----

    def save(self, text: str, kind: str = "consult", turn: str = "t0", ext: str = "md") -> Artifact:
        """按内容 hash 命名，同样内容重复保存不会产生新文件（幂等）。"""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
        day = datetime.now().strftime("%Y%m%d")
        safe_turn = _safe(turn)
        safe_kind = _safe(kind) or "misc"
        # 路径里刻意不放 turn：同一份内容在不同轮次被存两次没有意义，
        # 序号留在抬头里就够了。按天分桶则刚好是回收的单位。
        target = self.root / day / safe_kind / f"{digest}.{_safe(ext) or 'md'}"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            body = _header(kind, safe_turn, len(text), {"id": f"art_{digest}"}) + text
            target.write_text(body, encoding="utf-8")
            self._index(target, kind, safe_turn, len(text))
            self.maintain()
        return Artifact(
            id=f"art_{digest}",
            path=self._display(target),
            abs_path=str(target.resolve()),
            chars=len(text),
            tokens=count_tokens(text),
            kind=kind,
            created=datetime.now().isoformat(timespec="seconds"),
        )

    def spill(self, text: str, kind: str = "tool") -> str:
        """单条落盘（批量接口不可用时的退路）。"""
        return self.save(text, kind=kind, turn="trim", ext="log").path

    def spill_batch(self, texts: list[str], kind: str = "tool") -> list[str]:
        """一次裁剪的多个工具输出合成一个文件，返回每条的「路径#行区间」。

        逐条落盘会把一次裁剪变成上百个文件——目录难看，inode 也浪费。
        合成一个文件之后，锚点仍然精确到行，智能体照样能只读那一段。
        """
        if not texts:
            return []
        out = ["" for _ in texts]
        chunk: list[int] = []
        size = 0
        groups: list[list[int]] = []
        for i, text in enumerate(texts):
            if chunk and size + len(text) > self.cfg.batch_max_chars:
                groups.append(chunk)
                chunk, size = [], 0
            chunk.append(i)
            size += len(text)
        if chunk:
            groups.append(chunk)

        for group in groups:
            refs = self._write_group([(i, texts[i]) for i in group], kind)
            for i, ref in zip(group, refs):
                out[i] = ref
        return out

    def _write_group(self, items: list[tuple[int, str]], kind: str) -> list[str]:
        stamp = datetime.now().strftime("%Y%m%d")
        merged = "\n".join(f"{i}\x00{text}" for i, text in items)
        digest = hashlib.sha256(merged.encode("utf-8")).hexdigest()[:10]
        safe_kind = _safe(kind) or "misc"
        target = self.root / stamp / f"{safe_kind}-batch" / f"{digest}.log"
        if target.exists():
            return [f"{self._display(target)}#L{start}-{end}" for start, end in _ranges(target)]

        # 抬头必须 splitlines 展开：它本身是多行字符串，若当成一个列表元素，
        # 后面按 len(lines) 算出来的行号会整体偏移（实测偏 8 行）。
        lines: list[str] = _header(
            f"{kind}-batch", "trim", sum(len(t) for _, t in items), {"parts": len(items)}
        ).splitlines()
        ranges: list[tuple[int, int]] = []
        # 行号从抬头之后算起，锚点要落在正文上
        for n, (_, text) in enumerate(items, 1):
            body = text.rstrip("\n").splitlines() or [""]
            lines.append(f"===== part {n} =====")
            start = len(lines) + 1  # 1-based，指向正文第一行
            lines.extend(body)
            end = len(lines)
            ranges.append((start, end))
        blob = "\n".join(lines) + "\n"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(blob, encoding="utf-8")
        self._index(target, f"{kind}-batch", "trim", len(blob))
        self.maintain()
        return [f"{self._display(target)}#L{start}-{end}" for start, end in ranges]

    # ---- 摘要 ----

    def worth_storing(self, text: str) -> bool:
        """太短的回复存档反而更费——摘要加路径可能比原文还长。"""
        return len(text) >= max(0, self.cfg.min_store_chars)

    def digest(self, art: Artifact, text: str, inline_chars: int = 800) -> str:
        """生成回灌进上下文的短版本。目标 ~500 tokens。

        万一摘要没比原文短（短文档 + 长绝对路径），就直接返回原文——
        存档的意义是省 token，不是走流程。
        """
        lines = [
            f"[交付物已存档] id={art.id}  全文 {art.chars:,} 字 / 约 {art.tokens:,} tokens",
            f"路径: {art.path}",
        ]

        if self.cfg.toc:
            toc = table_of_contents(text)
            if toc:
                lines.append("目录:")
                lines.extend(f"  - {item}" for item in toc)

        body = text.strip()
        excerpt = body[: max(120, inline_chars)]
        if len(body) > len(excerpt):
            excerpt += "\n..."
        lines.append("摘要（开头部分）:")
        lines.append(excerpt)
        lines.append("需要细节时直接读上面这个路径的文件，不要凭摘要猜。")
        digest = "\n".join(lines)
        return digest if len(digest) < len(body) else body

    # ---- 回收 ----

    def stats(self) -> dict:
        """按天统计占用，给 CLI 和 /admin/context 看。"""
        days: dict[str, dict] = {}
        total_files = 0
        total_bytes = 0
        for day_dir in self._day_dirs():
            files = [p for p in day_dir.rglob("*") if p.is_file()]
            size = sum(p.stat().st_size for p in files)
            days[day_dir.name] = {"files": len(files), "bytes": size}
            total_files += len(files)
            total_bytes += size
        return {
            "root": str(self.root),
            "days": dict(sorted(days.items())),
            "files": total_files,
            "bytes": total_bytes,
            "limits": {
                "max_age_days": self.cfg.max_age_days,
                "max_total_mb": self.cfg.max_total_mb,
            },
        }

    def maintain(self, force: bool = False, dry_run: bool = False) -> dict:
        """按策略回收。节流到 clean_interval_s，避免每次写都扫一遍盘。"""
        now = time.time()
        if not force and (now - self._last_clean) < max(0, self.cfg.clean_interval_s):
            return {"skipped": True}
        self._last_clean = now

        removed_days: list[str] = []
        removed_files = 0
        freed = 0

        days = self._day_dirs()
        if self.cfg.max_age_days > 0:
            cutoff = now - self.cfg.max_age_days * 86400
            keep = []
            for day_dir in days:
                # 用目录名里的日期，不用 mtime：mtime 是「最后一次写入」，
                # 一个天天在追加的旧目录会永远显得很新，永远删不掉。
                try:
                    stamp = datetime.strptime(day_dir.name, "%Y%m%d").timestamp()
                except ValueError:
                    keep.append(day_dir)
                    continue
                if stamp < cutoff:
                    files, size = _measure(day_dir)
                    removed_days.append(day_dir.name)
                    removed_files += files
                    freed += size
                    if not dry_run:
                        shutil.rmtree(day_dir, ignore_errors=True)
                else:
                    keep.append(day_dir)
            days = keep

        if self.cfg.max_total_mb > 0:
            budget = self.cfg.max_total_mb * 1024 * 1024
            sizes = [(d, _measure(d)[1]) for d in days]
            total = sum(s for _, s in sizes)
            for day_dir, size in sizes:  # 从最旧的开始删
                if total <= budget:
                    break
                files, real = _measure(day_dir)
                removed_days.append(day_dir.name)
                removed_files += files
                freed += real
                total -= real
                if not dry_run:
                    shutil.rmtree(day_dir, ignore_errors=True)

        trimmed = self._trim_index(dry_run=dry_run)
        result = {
            "removed_days": removed_days,
            "removed_files": removed_files,
            "freed_bytes": freed,
            "index_trimmed": trimmed,
            "dry_run": dry_run,
        }
        if removed_days and not dry_run:
            logger.info("存档回收：删除 %s 个日目录 / %s 个文件 / %.1f MB", len(removed_days), removed_files, freed / 1048576)
        return result

    # ---- 内部 ----

    def _day_dirs(self) -> list[Path]:
        if not self.root.exists():
            return []
        out = [p for p in self.root.iterdir() if p.is_dir() and _DAY_RE.match(p.name)]
        return sorted(out, key=lambda p: p.name)

    def _trim_index(self, dry_run: bool = False) -> int:
        index = self.root / "index.jsonl"
        limit = max(0, self.cfg.index_max_lines)
        if not limit or not index.exists():
            return 0
        try:
            lines = index.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        if len(lines) <= limit:
            return 0
        drop = len(lines) - limit
        if not dry_run:
            try:
                keep = "\n".join(lines[-limit:]) + "\n"
                tmp = index.with_suffix(".jsonl.tmp")
                tmp.write_text(keep, encoding="utf-8")
                tmp.replace(index)
            except OSError:
                return 0
        return drop

    def _display(self, target: Path) -> str:
        resolved = target
        try:
            resolved = target.resolve()
        except OSError:
            return str(target)
        if not self.cfg.absolute:
            # 只在自己确定「网关和智能体同一个 cwd」时才开，否则相对路径对方打不开
            try:
                return str(resolved.relative_to(Path.cwd().resolve()))
            except (ValueError, OSError):
                pass
        return str(resolved)

    def _index(self, target: Path, kind: str, turn: str, chars: int) -> None:
        try:
            index = self.root / "index.jsonl"
            index.parent.mkdir(parents=True, exist_ok=True)
            with index.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "path": str(target),
                            "kind": kind,
                            "turn": turn,
                            "chars": chars,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except OSError:
            pass


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value or ""))[:40]


def _measure(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()]
    try:
        return len(files), sum(p.stat().st_size for p in files)
    except OSError:
        return len(files), 0


def _ranges(path: Path) -> list[tuple[int, int]]:
    """从已存在的批次文件里恢复锚点，避免重复写。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[tuple[int, int]] = []
    start = 0
    for i, line in enumerate(lines, 1):
        if line.startswith("===== part "):
            if start:
                out.append((start, i - 1))  # 段落结束在标记行的前一行
            start = i + 1
    if start:
        out.append((start, len(lines)))
    return out
