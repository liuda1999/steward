import json

from steward.artifacts import Store, table_of_contents
from steward.config import ArtifactsCfg


def make_store(tmp_path, **kwargs) -> Store:
    params = dict(dir=str(tmp_path / "artifacts"), enabled=True, inline_chars=400, min_store_chars=100)
    params.update(kwargs)
    return Store(ArtifactsCfg(**params))


LONG_DOC = (
    "## 1. 结论\n用两级缓存，L1 进程内、L2 Redis。\n\n"
    "## 2. 执行步骤\n"
    + "\n".join(f"{i}. 动作：第 {i} 步，输入是热点 key 列表，失败判据是错误率上升超过 5%" for i in range(1, 61))
    + "\n\n## 3. 风险与不确定项\n- 一致性延迟，触发条件是写多读少\n\n## 4. 待本地补充\n- 现有 Redis 规格"
)


def test_save_writes_file_and_metadata(tmp_path):
    store = make_store(tmp_path)
    art = store.save(LONG_DOC, kind="design", turn="t1")
    assert art.chars == len(LONG_DOC)
    assert art.tokens > 0
    assert art.id.startswith("art_")
    from pathlib import Path

    path = Path(art.abs_path)
    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert body.startswith("---\n")  # 统一抬头，文件自描述
    assert f"kind: design" in body.split("---")[1]
    assert "turn: t1" in body.split("---")[1]
    assert body.split("---\n", 2)[2].lstrip("\n") == LONG_DOC


def test_save_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    first = store.save(LONG_DOC, kind="design", turn="t1")
    second = store.save(LONG_DOC, kind="design", turn="t1")
    assert first.abs_path == second.abs_path
    assert len(list((tmp_path / "artifacts").rglob("*.md"))) == 1


def test_digest_contains_path_toc_and_excerpt(tmp_path):
    store = make_store(tmp_path)
    art = store.save(LONG_DOC, kind="design", turn="t1")
    digest = store.digest(art, LONG_DOC)

    assert art.path in digest
    assert "1. 结论" in digest
    assert "需要细节时" in digest
    assert len(digest) < 1500
    assert len(digest) < len(LONG_DOC) * 0.6


def test_digest_collapses_large_deliverable(tmp_path):
    """真实规模：1.6 万字的交付物应该压到 1/10 以下。"""
    store = make_store(tmp_path, inline_chars=800)
    big = LONG_DOC * 6
    art = store.save(big, kind="design", turn="t1")
    digest = store.digest(art, big)
    assert len(digest) < len(big) / 10
    assert len(digest) < 1800


def test_digest_is_bounded(tmp_path):
    store = make_store(tmp_path, inline_chars=200)
    art = store.save(LONG_DOC, kind="design", turn="t1")
    digest = store.digest(art, LONG_DOC, inline_chars=200)
    assert len(digest) < 900


def test_table_of_contents():
    toc = table_of_contents(LONG_DOC)
    assert toc[0].startswith("1. 结论")
    assert len(toc) == 4


def test_table_of_contents_empty():
    assert table_of_contents("没有标题的纯文本") == []


def test_spill_returns_path(tmp_path):
    store = make_store(tmp_path)
    path = store.spill("原始日志" * 200, kind="tool")
    assert path
    from pathlib import Path

    assert Path(path).exists()


def test_index_appended(tmp_path):
    store = make_store(tmp_path)
    store.save(LONG_DOC, kind="design", turn="t1")
    store.save(LONG_DOC + "尾巴", kind="design", turn="t1")
    lines = (tmp_path / "artifacts" / "index.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["path"]


def test_digest_path_is_absolute_by_default(tmp_path):
    """摘要里的路径必须能直接被智能体打开，不能依赖它和网关同一个 cwd。"""
    from pathlib import Path

    store = make_store(tmp_path)
    art = store.save(LONG_DOC, turn="t3")
    assert Path(art.path).is_absolute()
    assert Path(art.path).exists()


def test_digest_path_can_be_relative(tmp_path):
    from pathlib import Path

    store = make_store(tmp_path, absolute=False)
    art = store.save(LONG_DOC, turn="t3")
    # 不在 cwd 之下就退回绝对路径，绝不给出一个打不开的相对路径
    assert Path(art.path).is_absolute() or Path(art.path).exists()
    assert store.cfg.absolute is False

