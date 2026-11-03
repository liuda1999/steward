# SPDX-License-Identifier: Apache-2.0
"""refs：本地文件引用。

重点不在「能读」，在「读不到什么」。一半以上的用例是拒绝路径。
"""

from __future__ import annotations

import json
from pathlib import Path

from steward.config import RefsCfg
from steward.refs import Resolver


def make(tmp_path, **kwargs) -> Resolver:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    params = dict(
        enabled=True,
        roots=[str(root)],
        max_file_bytes=4096,
        max_total_chars=20000,
        max_files=5,
        grep_context=1,
    )
    params.update(kwargs)
    return Resolver(RefsCfg(**params), audit_path=tmp_path / "audit.jsonl")


def write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


CODE = "\n".join(
    [
        "import os",
        "",
        "def login(user, pwd):",
        "    if not user:",
        "        raise ValueError('no user')",
        "    return check(user, pwd)",
        "",
        "def logout(session):",
        "    session.clear()",
    ]
)


# ---------------------------------------------------------------- 开关


def test_disabled_returns_error(tmp_path):
    r = make(tmp_path, enabled=False)
    result = r.expand([{"path": "a.py"}])
    assert not result.ok
    assert "未启用" in result.error
    assert result.text == ""


def test_no_roots_returns_error(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    resolver = Resolver(RefsCfg(enabled=True, roots=[]))
    result = resolver.expand([{"path": "a.py"}])
    assert not result.ok
    assert "白名单" in result.error


def test_empty_specs_is_noop(tmp_path):
    r = make(tmp_path)
    result = r.expand([])
    assert result.text == ""
    assert result.files == []
    assert result.error == ""


# ---------------------------------------------------------------- 读得到


def test_reads_whole_file(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", "src/a.py", CODE)
    result = r.expand([{"path": "src/a.py"}])
    assert result.ok
    assert "def login(user, pwd):" in result.text
    assert "```python" in result.text
    assert result.files[0].path == "src/a.py"
    assert result.files[0].shown == result.files[0].lines == 9


def test_absolute_path_inside_root(tmp_path):
    r = make(tmp_path)
    p = write(tmp_path / "proj", "a.py", CODE)
    result = r.expand([{"path": str(p)}])
    assert result.ok
    assert result.files[0].path == "a.py"


def test_range_slices_and_numbers_lines(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", "a.py", CODE)
    result = r.expand([{"path": "a.py", "range": "3-5"}])
    assert result.ok
    assert "def login" in result.text
    assert "logout" not in result.text
    # 带行号，且行号是文件里的真实行号
    assert "    3  def login(user, pwd):" in result.text
    assert "第 3-5 行 / 共 9 行" in result.text


def test_range_single_line(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", "a.py", CODE)
    result = r.expand([{"path": "a.py", "range": "9"}])
    assert "session.clear()" in result.text
    assert "第 9-9 行" in result.text


def test_grep_keeps_context_and_marks_gaps(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", "a.py", CODE)
    result = r.expand([{"path": "a.py", "grep": "def "}])
    assert result.ok
    assert "def login" in result.text and "def logout" in result.text
    assert "..." in result.text  # 两段之间要有省略号
    assert "import os" not in result.text


def test_grep_is_case_insensitive(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", "a.py", CODE)
    assert r.expand([{"path": "a.py", "grep": "LOGIN"}]).ok


def test_grep_no_match_is_rejected(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", "a.py", CODE)
    result = r.expand([{"path": "a.py", "grep": "zzzz"}])
    assert not result.ok
    assert any("grep 无匹配" in x for x in result.rejected)


def test_glob_expands_and_respects_max(tmp_path):
    r = make(tmp_path)
    root = tmp_path / "proj"
    for i in range(4):
        write(root, f"pkg/m{i}.py", CODE)
    result = r.expand([{"glob": "pkg/*.py", "max_files": 2}])
    assert len(result.files) == 2


def test_glob_skips_denied_without_error(tmp_path):
    r = make(tmp_path)
    root = tmp_path / "proj"
    write(root, "pkg/a.py", CODE)
    write(root, "pkg/secret.txt", "token=abc")
    result = r.expand([{"glob": "pkg/*"}])
    names = [f.path for f in result.files]
    assert names == ["pkg/a.py"]


def test_duplicate_specs_read_once(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", "a.py", CODE)
    result = r.expand([{"path": "a.py"}, {"path": "a.py"}])
    assert len(result.files) == 1


# ---------------------------------------------------------------- 读不到


def test_escape_via_dotdot_is_rejected(tmp_path):
    r = make(tmp_path)
    write(tmp_path, "outside.txt", "机密")
    result = r.expand([{"path": "../outside.txt"}])
    assert not result.ok
    assert any("不在白名单内" in x for x in result.rejected)


def test_absolute_outside_root_is_rejected(tmp_path):
    r = make(tmp_path)
    secret = write(tmp_path, "outside.txt", "机密")
    result = r.expand([{"path": str(secret)}])
    assert any("不在白名单内" in x for x in result.rejected)


def test_symlink_pointing_outside_is_rejected(tmp_path):
    """这条是整个白名单的要害：resolve() 把链接展平后必须重新判一次。"""
    r = make(tmp_path)
    outside = write(tmp_path, "outside.txt", "机密")
    link = tmp_path / "proj" / "innocent.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        return  # 没有权限建链接的平台跳过
    result = r.expand([{"path": "innocent.py"}])
    assert not result.ok
    assert any("不在白名单内" in x for x in result.rejected)


def test_symlink_inside_root_is_allowed(tmp_path):
    r = make(tmp_path)
    root = tmp_path / "proj"
    target = write(root, "real.py", CODE)
    link = root / "alias.py"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return
    result = r.expand([{"path": "alias.py"}])
    assert result.ok


def test_dotenv_is_denied(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", ".env", "API_KEY=sk-xxx")
    result = r.expand([{"path": ".env"}])
    assert not result.ok
    assert any("拒绝规则" in x for x in result.rejected)


def test_pem_and_rsa_are_denied(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", "server.pem", "-----BEGIN-----")
    write(tmp_path / "proj", "id_rsa", "-----BEGIN-----")
    result = r.expand([{"path": "server.pem"}, {"path": "id_rsa"}])
    assert not result.ok
    assert len(result.rejected) == 2


def test_tokenizer_is_not_denied(tmp_path):
    """黑名单不能宽到把 tokenizer.py 也挡了。"""
    r = make(tmp_path)
    write(tmp_path / "proj", "tokenizer.py", CODE)
    assert r.expand([{"path": "tokenizer.py"}]).ok


def test_binary_is_rejected(tmp_path):
    r = make(tmp_path)
    (tmp_path / "proj" / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    result = r.expand([{"path": "blob.bin"}])
    assert not result.ok
    assert any("二进制" in x for x in result.rejected)


def test_directory_is_rejected(tmp_path):
    r = make(tmp_path)
    (tmp_path / "proj" / "pkg").mkdir(parents=True)
    result = r.expand([{"path": "pkg"}])
    assert any("不是普通文件" in x for x in result.rejected)


def test_missing_file_is_rejected(tmp_path):
    r = make(tmp_path)
    result = r.expand([{"path": "nope.py"}])
    assert any("文件不存在" in x for x in result.rejected)


def test_bad_spec_shape_is_rejected(tmp_path):
    r = make(tmp_path)
    assert any("格式不对" in x for x in r.expand(["a.py"]).rejected)
    assert any("缺 path" in x for x in r.expand([{"range": "1-2"}]).rejected)


# ---------------------------------------------------------------- 限额


def test_large_file_is_truncated_not_dropped(tmp_path):
    r = make(tmp_path, max_file_bytes=500)
    write(tmp_path / "proj", "big.py", "x = 1\n" * 2000)
    result = r.expand([{"path": "big.py"}])
    assert result.ok
    assert result.files[0].truncated
    assert "已截断" in result.text


def test_total_chars_cap_stops_reading(tmp_path):
    r = make(tmp_path, max_total_chars=1200, max_files=10)
    root = tmp_path / "proj"
    for i in range(5):
        write(root, f"m{i}.py", CODE * 3)
    result = r.expand([{"glob": "*.py", "max_files": 10}])
    assert result.chars <= 1300  # 允许最后一块略微超出（截断前的余量判断）
    assert any("总字数上限" in x for x in result.rejected)


def test_max_files_cap_is_reported_not_silent(tmp_path):
    """截断必须说出来：模型以为给了 5 个、实际 2 个，会基于错误前提干活。"""
    r = make(tmp_path, max_files=2)
    root = tmp_path / "proj"
    for i in range(5):
        write(root, f"m{i}.py", CODE)
    result = r.expand([{"glob": "*.py", "max_files": 5}])
    assert len(result.files) == 2
    assert any("只取了前 2 个" in x for x in result.rejected)


# ---------------------------------------------------------------- 告知与审计


def test_notice_mentions_counts_and_rejects(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", "a.py", CODE)
    result = r.expand([{"path": "a.py"}, {"path": ".env"}])
    notice = result.notice()
    assert "已附上 1 个文件" in notice
    assert "未附" in notice


def test_audit_written_per_expansion(tmp_path):
    r = make(tmp_path)
    write(tmp_path / "proj", "a.py", CODE)
    r.expand([{"path": "a.py"}, {"path": "../x"}], turn_id="t9")
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["turn"] == "t9"
    assert rec["files"] == ["a.py"]
    assert rec["rejected"]


def test_audit_failure_does_not_break_expansion(tmp_path):
    r = Resolver(
        RefsCfg(enabled=True, roots=[str(tmp_path)], max_file_bytes=4096),
        audit_path=tmp_path / "a.py",  # 是文件，写不进去
    )
    write(tmp_path, "a.py", CODE)
    result = r.expand([{"path": "a.py"}])
    assert result.ok
