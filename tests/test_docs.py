# SPDX-License-Identifier: Apache-2.0
"""文档的一致性约束。

双语文档最容易出的问题不是翻得不好，是**一边改了另一边没跟上**：
中文加了一节，英文还停在上一版；改了许可证，某个角落还写着旧协议。
这类错没有报错，只有读者会看见，所以拿测试盯着。

这里不比对措辞——两版本来就该各自通顺——只比对**结构**和**事实**。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZH = ROOT / "README.md"
EN = ROOT / "README.en.md"


def _headings(text: str, level: int) -> list[str]:
    return re.findall(rf"^{'#' * level} (.+)$", text, re.M)


def _links(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", text)]


def test_both_readmes_exist():
    assert ZH.is_file(), "缺中文 README"
    assert EN.is_file(), "缺英文 README"


def test_they_link_to_each_other():
    """语言切换必须双向：只在一个方向加链接，另一半读者就卡住了。"""
    zh, en = ZH.read_text(encoding="utf-8"), EN.read_text(encoding="utf-8")
    assert "README.en.md" in zh
    assert "README.md" in en


def test_section_counts_match():
    """中英文的章节数应当一致，差一个就说明有一边落后了。"""
    zh = _headings(ZH.read_text(encoding="utf-8"), 2)
    en = _headings(EN.read_text(encoding="utf-8"), 2)
    assert len(zh) == len(en), f"中文 {len(zh)} 节 / 英文 {len(en)} 节：\n{zh}\n{en}"


def test_english_readme_is_actually_english():
    """混进中文说明是整段复制过去的，读者会看到两种语言交错。

    唯一允许的是语言切换那一行——它必须写「简体中文」。
    """
    for lineno, line in enumerate(EN.read_text(encoding="utf-8").splitlines(), 1):
        if "简体中文" in line:
            continue
        assert not re.search(r"[\u4e00-\u9fff]", line), f"第 {lineno} 行混入中文：{line.strip()[:60]}"


def test_same_diagrams_in_both():
    """三张图是同一批文件，两版都得引用，否则一半读者看不到图。

    只看相对路径：徽章也是 .svg，但它是外链，不该被算进来。
    """
    zh, en = (
        {link for link in _links(p.read_text(encoding="utf-8")) if not link.startswith("http")}
        for p in (ZH, EN)
    )
    svgs = {link for link in zh | en if link.endswith(".svg")}
    assert len(svgs) == 3, svgs
    assert svgs <= zh, f"中文缺图：{svgs - zh}"
    assert svgs <= en, f"英文缺图：{svgs - en}"


def test_both_mention_the_same_version():
    """README 里的版本号必须和代码里的 __version__ 一致。"""
    import steward

    for path in (ZH, EN):
        text = path.read_text(encoding="utf-8")
        assert steward.__version__ in text, f"{path.name} 里的版本号不是 {steward.__version__}"


def test_every_relative_link_resolves():
    """相对链接指向不存在的文件，在 GitHub 上是 404，本地看不出来。"""
    bad: list[str] = []
    for md in ROOT.rglob("*.md"):
        if "__pycache__" in md.parts:
            continue
        for link in _links(md.read_text(encoding="utf-8")):
            if link.startswith(("http", "#", "mailto:")):
                continue
            target = link.split("#")[0]
            if target and not (md.parent / target).exists():
                bad.append(f"{md.relative_to(ROOT)} -> {link}")
    assert not bad, "失效链接：\n" + "\n".join(bad)


def test_license_is_apache_and_referenced():
    """协议换过一次，很容易只改一处。"""
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in lic and "Version 2.0" in lic
    assert (ROOT / "NOTICE").is_file(), "Apache-2.0 要求保留 NOTICE"
    for path in (ZH, EN):
        assert "Apache-2.0" in path.read_text(encoding="utf-8"), path.name


def test_source_files_carry_spdx():
    """SPDX 头是给工具链看的，漏一个都能被扫描器标成「许可证未知」。"""
    missing = [
        p.relative_to(ROOT).as_posix()
        for folder in ("steward", "scripts", "tests")
        for p in (ROOT / folder).glob("*.py")
        if "SPDX-License-Identifier: Apache-2.0" not in p.read_text(encoding="utf-8").splitlines()[0]
    ]
    assert not missing, f"缺 SPDX 头：{missing}"
