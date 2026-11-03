# SPDX-License-Identifier: Apache-2.0
"""存档的格式、去重、批量落盘与回收。

这一层的风险不是崩溃，是"慢慢变脏"：文件越来越多、格式各写各的、
同一份内容存五遍、index 无限长。所以断言集中在不变量上。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from steward.artifacts import _header
from steward.config import ArtifactsCfg
from steward.artifacts import Store

LONG_DOC = (
    "## 1. 结论\n用两级缓存。\n\n"
    "## 2. 执行步骤\n"
    + "\n".join(f"{i}. 动作：第 {i} 步，失败判据是错误率超过 5%" for i in range(1, 60))
    + "\n\n## 3. 风险\n- 一致性延迟"
)


def make_store(tmp_path, **kwargs) -> Store:
    params = dict(
        dir=str(tmp_path / "art"),
        enabled=True,
        inline_chars=800,
        min_store_chars=1500,
        absolute=True,
    )
    params.update(kwargs)
    return Store(ArtifactsCfg(**params))


def fake_day(store: Store, day: str, size: int = 1000) -> Path:
    d = Path(store.root) / day / "consult"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.md").write_text("x" * size, encoding="utf-8")
    return d


# ---------------------------------------------------------------- 格式


def test_header_shape_is_uniform():
    """两类文件（交付物 / 工具批次）抬头形状必须一致，脚本才能统一扫。"""
    for kind in ("consult", "tool-batch", "design"):
        extra = {"parts": 2} if kind.endswith("batch") else None
        body = _header(kind, "t1", 123, extra).splitlines()
        assert body[0] == "---"
        assert body[-1] == "---"
        assert f"kind: {kind}" in body
        assert any(line.startswith("created:") for line in body)
        assert "chars: 123" in body


def test_header_is_parseable_and_body_intact(tmp_path):
    store = make_store(tmp_path)
    art = store.save(LONG_DOC, kind="consult", turn="t7")
    text = Path(art.abs_path).read_text(encoding="utf-8")
    head, _, body = text.partition("\n---\n")
    meta = {k: v for k, v in (line.split(": ", 1) for line in head.replace("---\n", "").splitlines() if ": " in line)}
    assert meta["kind"] == "consult"
    assert meta["turn"] == "t7"
    assert int(meta["chars"]) == len(LONG_DOC)
    assert body.lstrip("\n") == LONG_DOC


def test_every_written_file_starts_with_header(tmp_path):
    store = make_store(tmp_path)
    store.save(LONG_DOC, kind="consult", turn="t1")
    store.spill_batch(["a" * 2000, "b" * 2000], "tool")
    for path in Path(tmp_path).rglob("*"):
        if path.is_file() and path.suffix in (".md", ".log"):
            assert path.read_text(encoding="utf-8").startswith("---\n"), path


# ---------------------------------------------------------------- 去重


def test_same_content_in_different_turns_reuses_one_file(tmp_path):
    """路径带 turn 会让同一份内容在多轮各存一份，纯属浪费磁盘。"""
    store = make_store(tmp_path)
    a = store.save(LONG_DOC, kind="consult", turn="t1")
    b = store.save(LONG_DOC, kind="consult", turn="t2")
    assert a.abs_path == b.abs_path
    assert len(list(Path(tmp_path).rglob("*.md"))) == 1


def test_different_content_gets_different_files(tmp_path):
    store = make_store(tmp_path)
    a = store.save(LONG_DOC, kind="consult", turn="t1")
    b = store.save(LONG_DOC + "补一段", kind="consult", turn="t1")
    assert a.abs_path != b.abs_path


def test_path_does_not_contain_turn(tmp_path):
    """turn 只进抬头，不进路径——否则去重就失效了。"""
    store = make_store(tmp_path)
    art = store.save(LONG_DOC, kind="consult", turn="t_abc123")
    assert "t_abc123" not in art.abs_path


# ---------------------------------------------------------------- 批量落盘


def test_batch_writes_one_file_not_many(tmp_path):
    """一次裁剪压掉上百条工具输出，逐条落盘就是上百个文件。"""
    store = make_store(tmp_path)
    texts = [f"输出 {i}\n" + "行\n" * 50 for i in range(60)]
    refs = store.spill_batch(texts, "tool")
    assert len(refs) == 60
    assert len(list(Path(tmp_path).rglob("*.log"))) == 1


def test_batch_anchors_point_at_the_right_lines(tmp_path):
    store = make_store(tmp_path)
    texts = [f"HEAD{i}\n" + "\n".join(f"l{i}-{j}" for j in range(4)) for i in range(3)]
    refs = store.spill_batch(texts, "tool")
    lines = Path(refs[0].split("#")[0]).read_text(encoding="utf-8").splitlines()
    for i, ref in enumerate(refs):
        start, end = (int(x) for x in ref.split("#L")[1].split("-"))
        seg = "\n".join(lines[start - 1 : end])
        assert f"HEAD{i}" in seg
        assert f"l{i}-3" in seg
        assert f"l{(i + 1) % 3}-0" not in seg  # 没串到隔壁


def test_batch_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    texts = [f"内容{i}" for i in range(4)]
    first = store.spill_batch(texts, "tool")
    second = store.spill_batch(texts, "tool")
    assert first == second
    assert len(list(Path(tmp_path).rglob("*.log"))) == 1


def test_batch_splits_when_over_limit(tmp_path):
    store = make_store(tmp_path, batch_max_chars=2000)
    refs = store.spill_batch(["x" * 900 for _ in range(6)], "tool")
    assert len(refs) == 6
    assert len({r.split("#")[0] for r in refs}) == 3  # 每两个一组


def test_batch_empty_input(tmp_path):
    store = make_store(tmp_path)
    assert store.spill_batch([], "tool") == []


# ---------------------------------------------------------------- 回收


def test_stats_counts_by_day(tmp_path):
    store = make_store(tmp_path)
    fake_day(store, "20260101")
    fake_day(store, "20260102")
    s = store.stats()
    assert set(s["days"]) == {"20260101", "20260102"}
    assert s["files"] == 2


def test_maintain_removes_old_days(tmp_path):
    """按目录名里的日期判断，不按 mtime。

    mtime 是「最后一次写入」：一个天天在追加的旧目录会永远显得很新，
    结果就是永远删不掉。
    """
    store = make_store(tmp_path, max_age_days=30)
    old = fake_day(store, "20200101")
    today = fake_day(store, datetime.now().strftime("%Y%m%d"))
    result = store.maintain(force=True)
    assert "20200101" in result["removed_days"]
    assert not old.exists()
    assert today.exists()


def test_maintain_keeps_old_dir_that_was_touched_recently(tmp_path):
    """反过来也要成立：旧日期 + 刚写过，仍然要清掉。"""
    store = make_store(tmp_path, max_age_days=30)
    old = fake_day(store, "20200101")
    os.utime(old, (time.time(), time.time()))
    store.maintain(force=True)
    assert not old.exists()


def test_maintain_respects_total_cap(tmp_path):
    store = make_store(tmp_path, max_age_days=0, max_total_mb=1)
    d1 = fake_day(store, "20250101", size=700_000)
    d2 = fake_day(store, "20250102", size=700_000)
    result = store.maintain(force=True)
    assert result["freed_bytes"] > 0
    assert not d1.exists() and d2.exists()  # 从最旧的开始删，删到够为止


def test_maintain_dry_run_deletes_nothing(tmp_path):
    store = make_store(tmp_path, max_age_days=1)
    old = fake_day(store, "20200101")
    result = store.maintain(force=True, dry_run=True)
    assert result["removed_days"]
    assert old.exists()


def test_maintain_is_throttled(tmp_path):
    store = make_store(tmp_path, clean_interval_s=600)
    store.maintain(force=True)
    assert store.maintain()["skipped"] is True


def test_maintain_ignores_non_day_dirs(tmp_path):
    """只有 YYYYMMDD 才是清理单位，别把别的目录删了。"""
    store = make_store(tmp_path, max_age_days=1)
    keep = Path(store.root) / "manual" / "notes"
    keep.mkdir(parents=True, exist_ok=True)
    (keep / "a.md").write_text("x", encoding="utf-8")
    store.maintain(force=True)
    assert keep.exists()


def test_index_is_trimmed(tmp_path):
    store = make_store(tmp_path, index_max_lines=5)
    for i in range(12):
        store.save(LONG_DOC + str(i), kind="consult", turn="t1")
    store.maintain(force=True)
    lines = (Path(store.root) / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 5


def test_index_records_kind_and_turn(tmp_path):
    store = make_store(tmp_path)
    store.save(LONG_DOC, kind="consult", turn="t9")
    line = json.loads((Path(store.root) / "index.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["kind"] == "consult"
    assert line["turn"] == "t9"
    assert line["chars"] == len(LONG_DOC)
