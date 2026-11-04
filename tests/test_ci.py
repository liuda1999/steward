# SPDX-License-Identifier: Apache-2.0
"""CI 配置的约束。

这个仓库的测试连同文档、启动脚本、打包元数据一起盯。CI 自己若没人盯，
最容易出现的是「workflow 被删了或改歪了，但徽章还一直显示绿」——
静态徽章那会儿是手写数字，现在换成真实状态，就更得保证它真的在跑。

这里只锁结构与边界，不锁具体措辞：
1. workflow 存在、能被 YAML 解析、触发条件齐全；
2. 矩阵覆盖 pyproject 里声明过的每个 Python 版本和两个平台；
3. 装的是项目自己的依赖与包，跑的是全量测试；
4. CI 只做测试，不接管发布、不需要任何令牌；
5. 两份 README 的徽章指向这个 workflow，且不再有手写的用例数。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
ZH = ROOT / "README.md"
EN = ROOT / "README.en.md"
BADGE = "https://github.com/liuda1999/steward/actions/workflows/tests.yml/badge.svg"
RUNS = "https://github.com/liuda1999/steward/actions/workflows/tests.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict:
    # PyYAML 走 YAML 1.1，裸的 `on` 会被解析成布尔 True。
    # BaseLoader 把所有标量当字符串，正好用来检查结构而不被类型坑到。
    return yaml.load(_text(), Loader=yaml.BaseLoader)


def test_workflow_exists():
    """没有 workflow，徽章就是一张永远不变的图片。"""
    assert WORKFLOW.is_file(), "缺 .github/workflows/tests.yml"


def test_workflow_is_valid_yaml():
    """YAML 少个缩进，GitHub 直接不认这个 workflow，且不会标红别的检查。"""
    data = _workflow()
    assert isinstance(data, dict)
    assert "on" in data, "on 键丢了（或写成了会被 YAML 1.1 转成布尔的形式）"
    assert "jobs" in data


def test_workflow_triggers_on_push_pr_and_manual_dispatch():
    """push / PR / 手动都要能触发。

    发布脚本走 Git Data API 重建提交后仍会移动 main，push 这条能兜住；
    workflow_dispatch 则是给「刚加完 workflow 想立刻验一次」用的。
    """
    triggers = _workflow()["on"]
    for name in ("push", "pull_request", "workflow_dispatch"):
        assert name in triggers, f"缺触发条件：{name}"
    assert "main" in triggers["push"]["branches"]


def test_matrix_matches_the_declared_python_versions():
    """声明支持 3.11/3.12/3.13 就必须真的测这三档。

    单靠本机 Windows + 3.13 是发现不了版本相关问题的；反过来，
    声明里加了一档却没进矩阵，用户看到的兼容性就是没验证过的。
    """
    declared = {
        c.rsplit(" :: ", 1)[-1]
        for c in tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["classifiers"]
        if c.startswith("Programming Language :: Python :: 3.")
    }
    matrix = _workflow()["jobs"]["pytest"]["strategy"]["matrix"]
    assert set(matrix["python"]) == declared, f"矩阵 {matrix['python']} 与声明 {declared} 不一致"
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest"}


def test_workflow_installs_the_package_and_runs_the_full_suite():
    """只装 requirements 跑测试，会漏掉 `pip install -e .` 才暴露的打包问题。"""
    text = _text()
    assert "actions/checkout@" in text, "没 checkout，git 索引相关测试会失真"
    assert "actions/setup-python@" in text
    for command in (
        "pip install -r requirements.txt",
        "pip install -e .",
        "python -m pytest -q",
    ):
        assert command in text, f"workflow 里没有：{command}"


def test_workflow_only_tests_and_needs_no_secrets():
    """CI 不接管发布，也不该持有 PAT。

    发布脚本已经处理了代理/CA 兼容，令牌只从本地环境变量读；把它搬进 CI
    等于多一份长期存在的凭据。这条盯着别让 workflow 长出发布步骤。
    """
    text = _text()
    # 只看配置行：注释里说明「发布由本地脚本做」是给读的人看的，不是步骤。
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("secrets.", "GITHUB_TOKEN", "STEWARD_GH_TOKEN", "publish.py", "create_release"):
        assert forbidden not in body, f"CI 不该出现：{forbidden}"


def test_both_readmes_point_at_the_real_badge():
    """徽章必须指向真实运行状态，而且中英两边都得换。"""
    for path in (ZH, EN):
        text = path.read_text(encoding="utf-8")
        assert BADGE in text, f"{path.name} 没换成真实 Actions 徽章"
        assert RUNS in text, f"{path.name} 的徽章没链接到 workflow 页面"


def test_readmes_no_longer_hardcode_a_test_count():
    """手写数字一改测试就得跟着改，漏改就是对外虚报。

    换成动态徽章之后，两份 README 都不该再出现 `badge/tests-NNN` 这种写法。
    发布说明里的历史数字是记录，不受这条约束。
    """
    for path in (ZH, EN):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"badge/tests-\d+", text), f"{path.name} 还在写死测试数"
