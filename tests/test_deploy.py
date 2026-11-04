# SPDX-License-Identifier: Apache-2.0
"""部署与启动入口的验收测试。

这个文件盯的是「用户拿到代码之后能不能跑起来」这件事。它和别的测试
不一样的地方在于，别的测试盯的是**功能对不对**，这里盯的是**入口通不通**。

为什么值得单独盯：

1. **文档里的命令必须真的存在。** README 写 `python -m steward start`，
   而 `start` 这个子命令某次重构时被删了 —— 文档不会报错，用户按着做
   却只得到一句「invalid choice」。所以命令表和 parser 必须对得上。
2. **全新克隆必须能起来。** 之前的版本里，`git clone` 之后第一条
   `serve` 会因为找不到配置而直接失败。这条回归过一次，所以拿测试钉住。
3. **依赖清单不能漂移。** `requirements.txt` 和 `pyproject.toml` 各写一遍
   依赖，漂移了就出现「requirements 装出来能跑，pip 装出来不能跑」。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT))

from steward import __version__  # noqa: E402
from steward.__main__ import COMMANDS, build_parser  # noqa: E402
from steward.config import (  # noqa: E402
    ensure_config,
    find_config,
    is_pristine_config,
    load_config,
    template_path,
)


# ---------------------------------------------------------------- 打包元数据


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_exists_and_is_parseable():
    """没有 pyproject.toml，`pip install -e .` 直接失败 —— 那就没有正式入口。"""
    assert (ROOT / "pyproject.toml").is_file(), "缺 pyproject.toml，pip 装不上"
    data = _pyproject()
    assert data["project"]["name"]
    assert data["build-system"]["build-backend"]


def test_pyproject_version_matches_the_package():
    """两处版本号不一致的话，pip 装出来的和源码跑的不是同一个版本。"""
    assert _pyproject()["project"]["version"] == __version__


def test_console_script_points_at_a_real_entry():
    """`steward` 命令必须真的指到一个能调用的函数上。

    指错了的话，`pip install` 成功、`steward serve` 报 ImportError，
    而用户看到的是「装好了但用不了」。
    """
    scripts = _pyproject()["project"]["scripts"]
    assert "steward" in scripts, "没有 steward 命令行入口"

    target = scripts["steward"]
    module_name, _, func_name = target.partition(":")
    module = __import__(module_name, fromlist=[func_name])
    assert callable(getattr(module, func_name)), f"{target} 不可调用"


def test_dependencies_match_requirements_txt():
    """两份依赖清单漂移，是最难查的一类问题。

    `requirements.txt` 是「照着 README 装」的路，`pyproject.toml` 是
    「pip install」的路。两者不一致时，两条路装出来的环境不一样。
    """
    requirements = {
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    declared = {
        dep.split(">=")[0].split("[")[0].split("==")[0].strip().lower()
        for dep in _pyproject()["project"]["dependencies"]
    }
    assert declared == {r.split(">=")[0].split("[")[0].strip().lower() for r in requirements}


def test_package_data_includes_the_console_page():
    """漏了 console.html，pip 装完控制台就是一个空白页。"""
    package_data = _pyproject()["tool"]["setuptools"]["package-data"]["steward"]
    assert any("html" in item for item in package_data), package_data
    assert any("config.example.yaml" in item for item in package_data), package_data


# ---------------------------------------------------------------- 配置文件定位


def test_template_ships_with_the_package():
    """随包模板是首次启动的原料，缺了就没法自动生成配置。"""
    tpl = template_path()
    assert tpl is not None and tpl.is_file()
    assert tpl.name == "config.example.yaml"


def test_template_is_valid_and_uses_the_safe_port():
    """模板自己得守规矩 —— 它是用户拿到的第一份配置。"""
    from steward.config import DEFAULT_PORT

    text = template_path().read_text(encoding="utf-8")
    assert f"port: {DEFAULT_PORT}" in text
    assert "8010" not in text


def test_template_advanced_slot_is_empty():
    """进阶槽位在模板里必须留空。

    填上一个假域名（比如 your-provider.example.com）会让网关认为
    「云端可用」，于是注入 consult_cloud 工具，每轮都往一个不存在的
    域名发请求。空槽位则是合法且更省的状态：每轮少约 500 tokens。
    """
    cfg = load_config(template_path())
    assert cfg.advanced.base_url == ""
    assert cfg.advanced.model == ""


def test_fresh_start_generates_a_config(tmp_path, monkeypatch):
    """全新克隆 → 第一次 serve：应该自动生成配置，而不是报错退出。

    这条回归过一次。老行为是 `base.base_url 未配置` 直接 return 1，
    用户拿到 clone 下来的代码后完全不知道该建什么文件、放哪。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STEWARD_CONFIG", raising=False)

    target = find_config()
    assert target == tmp_path / "config.yaml"
    assert not target.exists()

    created = ensure_config(target)
    assert created is True
    assert target.is_file()

    # 生成的那份必须能解析、能通过校验 —— 否则照样起不来。
    cfg = load_config(target)
    assert cfg.server.port > 0
    assert cfg.base.base_url


def test_ensure_config_does_not_overwrite(tmp_path):
    """已有的配置不能被模板覆盖 —— 那是用户的数据。"""
    target = tmp_path / "config.yaml"
    target.write_text("base:\n  base_url: http://mine/v1\n  model: mine\n", encoding="utf-8")

    assert ensure_config(target) is False
    assert "mine" in target.read_text(encoding="utf-8")


def test_ensure_config_never_writes_into_the_package():
    """pip 装的包目录多半只读，往里写就是安装事故。"""
    tpl = template_path()
    assert tpl is not None
    assert ensure_config(tpl) is False
    assert tpl.read_text(encoding="utf-8").strip() != ""


def test_pristine_config_is_detected(tmp_path):
    """能分辨「原封不动的模板」和「用户改过的配置」。

    `init` 靠这个判断能不能直接覆盖：覆盖一份没动过的模板不丢数据，
    覆盖用户填过的就是丢数据。
    """
    generated = tmp_path / "config.yaml"
    assert ensure_config(generated) is True
    assert is_pristine_config(generated) is True

    generated.write_text(
        generated.read_text(encoding="utf-8") + "\n# 我改过了\n", encoding="utf-8"
    )
    assert is_pristine_config(generated) is False


def test_config_env_var_still_wins(tmp_path, monkeypatch):
    """`STEWARD_CONFIG` 是给部署用的显式指定，不能被自动生成逻辑盖过。"""
    explicit = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("STEWARD_CONFIG", str(explicit))
    assert find_config() == explicit.resolve()


# ---------------------------------------------------------------- 命令行入口


def test_start_command_exists():
    """正式启动入口不能被重构删掉。"""
    assert "start" in COMMANDS


def test_start_is_registered_in_the_parser():
    parser = build_parser()
    args = parser.parse_args(["start", "--check"])
    assert args.func is not None
    assert args.check is True


def test_every_documented_command_exists():
    """README 里出现的每个 steward 子命令，parser 里都得有。

    文档写错命令名是用户最容易踩的坑：它不报错，只是让你以为项目坏了。
    """
    parser = build_parser()
    for name in COMMANDS:
        args = parser.parse_args([name] if name != "key" else ["key", "list"])
        assert args.func is not None, name


def test_serve_still_works_without_start():
    """`serve` 不能被 `start` 取代 —— 守护进程和 systemd 用的就是它。"""
    parser = build_parser()
    args = parser.parse_args(["serve", "--check"])
    assert args.check is True


def test_start_reports_the_four_checks(tmp_path, monkeypatch, capsys):
    """`start --check` 要把四步都报出来，并且不真的起服务。

    「起服务」这件事必须能单独验证，否则测试要么起一个真的服务、
    要么只能验证到一半。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STEWARD_CONFIG", raising=False)

    from steward.__main__ import main

    assert main(["start", "--check"]) == 0
    out = capsys.readouterr().out
    assert "[1/4]" in out
    assert "[4/4]" in out


def test_start_generates_config_before_checking(tmp_path, monkeypatch):
    """`start --check` 也要生成配置。

    否则「先 start --check 看看环境对不对」这条命令会因为没有配置而
    报错，而它本来是给还没配任何东西的人用的。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STEWARD_CONFIG", raising=False)

    from steward.__main__ import main

    main(["start", "--check"])
    assert (tmp_path / "config.yaml").is_file()


def _free_port() -> int:
    """拿一个当前空闲的端口。

    这两个用例原来写死 27418 —— 用户自己跑着网关时，测试会被真实进程占端口
    挡下来，报「端口已被占用」，看起来像代码坏了。测试不该依赖机器当前状态。
    """
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_port_override_is_not_lost_on_restart(tmp_path, monkeypatch):
    """`--port` 必须真的生效。

    回归过一次：命令行覆盖只在 cmd_serve 里盖一次，而 serve 循环为了
    支持重启会**重新读一遍配置**，覆盖被冲掉了。于是 `serve --port 27xxx`
    悄悄用了配置文件里的端口 —— 用户以为自己换了端口、实际没换，
    真撞上端口冲突时完全摸不着头脑。

    这里盯的是「覆盖要盖在重新读配置之后」，也就是修的位置。
    """
    import asyncio

    import steward.__main__ as cli

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"server:\n  host: 127.0.0.1\n  port: {_free_port()}\n"
        "base:\n  base_url: http://127.0.0.1:11434/v1\n  model: m\n",
        encoding="utf-8",
    )

    captured: dict = {}

    def spy_create(cfg, config_path=None):
        captured["host"] = cfg.server.host
        captured["port"] = cfg.server.port
        raise _StopHere()

    monkeypatch.setattr(cli, "create_app", spy_create)

    override = _free_port()
    args = argparse.Namespace(host=None, port=override)
    with pytest.raises(_StopHere):
        asyncio.run(cli._serve_loop(cfg_path, args))

    assert captured, "没有走到 create_app"
    assert captured["port"] == override, f"实际用了端口 {captured['port']}，不是命令行给的 {override}"


def test_host_override_is_not_lost_on_restart(tmp_path, monkeypatch):
    """`--host` 同理。局域网部署靠的就是它。"""
    import asyncio

    import steward.__main__ as cli

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"server:\n  host: 127.0.0.1\n  port: {_free_port()}\n"
        "base:\n  base_url: http://127.0.0.1:11434/v1\n  model: m\n",
        encoding="utf-8",
    )

    captured: dict = {}

    def spy_create(cfg, config_path=None):
        captured["host"] = cfg.server.host
        raise _StopHere()

    monkeypatch.setattr(cli, "create_app", spy_create)

    args = argparse.Namespace(host="0.0.0.0", port=None)
    with pytest.raises(_StopHere):
        asyncio.run(cli._serve_loop(cfg_path, args))

    assert captured["host"] == "0.0.0.0"


class _StopHere(Exception):
    """用来在 create_app 处中断 _serve_loop。"""


# ---------------------------------------------------------------- 启动脚本


def test_start_scripts_exist():
    """两个平台的启动脚本都得在。"""
    assert (ROOT / "scripts" / "start.sh").is_file()
    assert (ROOT / "scripts" / "start.bat").is_file()


def test_start_sh_is_executable():
    """克隆到 Linux 上之后，`./scripts/start.sh` 必须能直接跑。

    盯的是**git 索引里记的模式**（100755），不是当前机器上的文件权限。
    区别很关键：Windows 上 `core.fileMode=false`，本地 chmod 过也不进索引，
    推上去的还是 100644 —— 用户 clone 到 Linux 就是 permission denied。
    """
    import subprocess

    proc = subprocess.run(
        ["git", "ls-files", "-s", "--", "scripts/start.sh"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip("git 不可用，跳过索引模式检查")

    mode = proc.stdout.split()[0] if proc.stdout.split() else ""
    assert mode == "100755", f"git 索引里的模式是 {mode}，不是 100755（Linux clone 后不可执行）"


def test_start_sh_uses_the_project_entry():
    """脚本必须走 `python -m steward start`，别自己拼一套启动逻辑。"""
    text = (ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
    assert "-m steward start" in text
    assert "3.11" in text, "没做 Python 版本检查"


def test_start_bat_is_ascii_only():
    """.bat 由 cmd.exe 按控制台代码页读，非 ASCII 会变成乱码。

    这条不是洁癖：用户会看到「锟斤拷」之类的字符，以为脚本坏了。
    中文说明放在 README 里，不放在 .bat 里。
    """
    raw = (ROOT / "scripts" / "start.bat").read_bytes()
    assert all(byte < 128 for byte in raw), "start.bat 里有非 ASCII 字符"


def test_start_bat_uses_the_project_entry():
    text = (ROOT / "scripts" / "start.bat").read_text(encoding="utf-8")
    assert "-m steward start" in text


def test_start_bat_falls_back_when_py_is_missing():
    """不能只认 `py` 启动器。

    真实反馈：Windows 上只装了 python（微软商店版不带 py 启动器），
    脚本写死 `py -3` 时窗口一闪就关，用户以为「这个项目根本没有启动方式」。
    所以三个候选都必须先探测再使用，且探测要用 `-c "import sys"`。
    """
    text = (ROOT / "scripts" / "start.bat").read_text(encoding="utf-8")
    assert 'py -3 -c "import sys"' in text, "没探测 py"
    assert 'python -c "import sys"' in text, "没回退到 python"
    assert 'python3 -c "import sys"' in text, "没回退到 python3"
    # 旧写法：无条件 set "PY=py -3"，py 不存在时直接炸。
    assert 'if not defined PY set "PY=py -3"' not in text


def test_start_bat_scans_common_install_dirs():
    """PATH 上没有任何可用 Python 时，还要去常见安装目录找。

    真实反馈：机器上唯一能用的 Python 不在 PATH 上（PATH 里只有微软商店的
    占位程序），脚本只查 PATH 时报 "No Python found"，项目照样起不来。
    所以最后一步要扫标准安装目录，包括 WorkBuddy 自带的那份。
    """
    text = (ROOT / "scripts" / "start.bat").read_text(encoding="utf-8")
    assert ":scan" in text, "没有目录扫描兜底"
    assert r"%LOCALAPPDATA%\Programs\Python\Python3*" in text
    assert r"%ProgramFiles%\Python3*" in text
    assert r"%USERPROFILE%\.workbuddy\binaries\python\versions\*" in text
    # 盘符路径要加引号；"py -3" 不能加，否则 cmd 会去找一个叫 "py -3" 的程序
    assert '"%PY:~1,1%"==":"' in text


def test_template_ships_no_model_aliases():
    """模板不该带默认别名。

    带 [local, steward] 时，客户端会看到三个模型，用户以为是三套模型在跑。
    别名是「需要时自己加」的东西，默认值应该只有一个 public_model。
    """
    text = (ROOT / "steward" / "config.example.yaml").read_text(encoding="utf-8")
    assert "model_aliases: []" in text, "模板又带上默认别名了"


def test_start_scripts_are_documented():
    """启动脚本必须出现在 README 里，否则没人知道它存在。"""
    for name in ("README.md", "README.en.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "start.sh" in text, f"{name} 没提 start.sh"
        assert "start.bat" in text, f"{name} 没提 start.bat"


def test_readme_puts_startup_before_the_architecture():
    """启动方式必须排在「它解决什么问题」之前，不能埋在部署章节里。

    真实反馈：用户把整份介绍翻完，也没找到「日常怎么启动」。
    读者找的是「怎么跑」，不是「你解决了什么」。
    """
    cases = (
        ("README.md", "快速启动", "## 一、"),
        ("README.en.md", "Quick start", "## 1. What it solves"),
    )
    for name, marker, later in cases:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert marker in text, f"{name} 没有「{marker}」章节"
        assert text.index(marker) < text.index(later), (
            f"{name} 里「{marker}」排在「{later}」之后，用户找不到"
        )


# ---------------------------------------------------------------- 部署文档


def test_deployment_guide_exists():
    """部署与启动必须有独立文档，而不是散在 README 里。"""
    guide = ROOT / "docs" / "DEPLOYMENT.md"
    assert guide.is_file()
    text = guide.read_text(encoding="utf-8")
    for topic in ("启动", "局域网", "开机自启", "升级", "排错"):
        assert topic in text, f"部署文档没讲「{topic}」"


def test_deployment_guide_is_linked_from_both_readmes():
    """两份 README 都要能进部署文档，否则一半读者找不到。"""
    for name in ("README.md", "README.en.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "docs/DEPLOYMENT.md" in text, f"{name} 没链接部署文档"


def test_deployment_guide_uses_the_real_port():
    """文档里的端口必须和代码一致。文档写错端口是部署事故的第一来源。"""
    from steward.config import DEFAULT_PORT

    text = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert str(DEFAULT_PORT) in text


def test_deployment_guide_commands_exist():
    """部署文档里出现的 steward 子命令都得是真的。"""
    import re

    text = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    used = set(re.findall(r"steward ([a-z]+)", text))
    for name in used:
        assert name in COMMANDS, f"部署文档提到了不存在的命令：steward {name}"


def test_runtime_config_is_ignored():
    """首次启动生成的 `config.yaml` 不能进仓库。

    里面有用户自己的地址、模型标识、端口，甚至是内网拓扑 —— 提交上去等于
    把部署信息公开。所以 .gitignore 必须挡住仓库根目录那一份。
    """
    import subprocess

    proc = subprocess.run(
        ["git", "check-ignore", "-q", "--", "config.yaml"],
        cwd=str(ROOT),
        capture_output=True,
    )
    assert proc.returncode == 0, "config.yaml 没被 .gitignore 挡住，会被误提交"


def test_the_shipped_template_is_not_ignored():
    """模板本身必须能进仓库 —— 它是首次启动的原料。"""
    import subprocess

    proc = subprocess.run(
        ["git", "check-ignore", "-q", "--", "steward/config.example.yaml"],
        cwd=str(ROOT),
        capture_output=True,
    )
    assert proc.returncode != 0, "steward/config.example.yaml 被 .gitignore 挡了，clone 下来没有模板"


# ---------------------------------------------------------------- 端到端


@pytest.mark.skipif(os.name == "nt", reason="Windows 上没有 ./scripts/start.sh 的可执行语义")
def test_start_script_runs_end_to_end(tmp_path):
    """真的把启动脚本跑一遍，确认它能起服务。

    这是唯一一条覆盖「用户照着 README 做」全过程的测试：
    脚本 → python -m steward start → 配置生成 → 服务监听。
    """
    port = 27431
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONHOME"] = env.get("PYTHONHOME", "")

    proc = subprocess.Popen(
        ["bash", str(ROOT / "scripts" / "start.sh"), "--port", str(port)],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        import time

        import httpx

        deadline = time.time() + 45
        health = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
                break
            except httpx.HTTPError:
                time.sleep(0.4)
        assert health is not None and health.status_code == 200, "启动脚本没能把服务拉起来"
        # 首次启动必须顺手把配置生成好
        assert (tmp_path / "config.yaml").is_file()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
