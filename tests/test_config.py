# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path

from steward.config import load_config


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_new_section_names(tmp_path):
    cfg_path = write(
        tmp_path / "config.yaml",
        """
base:
  base_url: http://127.0.0.1:11434/v1
  model: base-m
advanced:
  base_url: https://api.example.com/v1
  model: adv-m
  api_key: sk-1
""",
    )
    cfg = load_config(cfg_path)

    assert cfg.base.model == "base-m"
    assert cfg.advanced.model == "adv-m"
    assert cfg.advanced.api_key == "sk-1"


def test_legacy_sections_still_load(tmp_path):
    cfg_path = write(
        tmp_path / "config.yaml",
        """
local:
  base_url: http://127.0.0.1:1234/v1
  model: old-local
cloud:
  base_url: https://old.example.com/v1
  model: old-cloud
""",
    )
    cfg = load_config(cfg_path)

    assert cfg.base.model == "old-local"
    assert cfg.advanced.model == "old-cloud"


def test_env_overrides_yaml_and_new_name_wins(tmp_path, monkeypatch):
    cfg_path = write(
        tmp_path / "config.yaml",
        """
base:
  base_url: http://x/v1
  model: from-yaml
""",
    )
    monkeypatch.delenv("BASE_MODEL", raising=False)
    monkeypatch.setenv("LOCAL_MODEL", "from-legacy-env")
    assert load_config(cfg_path).base.model == "from-legacy-env"

    monkeypatch.setenv("BASE_MODEL", "from-new-env")
    assert load_config(cfg_path).base.model == "from-new-env"


def test_dotenv_is_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("CB_TEST_TOKEN", raising=False)
    write(tmp_path / ".env", "CB_TEST_TOKEN=secret-from-dotenv\n")
    cfg_path = write(
        tmp_path / "config.yaml",
        """
advanced:
  base_url: http://x/v1
  model: m
  api_key: ${CB_TEST_TOKEN}
""",
    )
    cfg = load_config(cfg_path)
    assert cfg.advanced.api_key == "secret-from-dotenv"
    assert os.environ["CB_TEST_TOKEN"] == "secret-from-dotenv"


def test_existing_env_not_overwritten_by_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("CB_TEST_TOKEN", "already-set")
    write(tmp_path / ".env", "CB_TEST_TOKEN=from-file\n")
    cfg_path = write(
        tmp_path / "config.yaml",
        """
advanced:
  base_url: http://x/v1
  api_key: ${CB_TEST_TOKEN}
""",
    )
    assert load_config(cfg_path).advanced.api_key == "already-set"


def test_relative_state_and_keys_resolve_against_config_dir(tmp_path):
    sub = tmp_path / "nested"
    sub.mkdir()
    cfg_path = write(sub / "config.yaml", "guard:\n  state_file: state/usage.json\n")
    cfg = load_config(cfg_path)

    assert Path(cfg.guard.state_file) == (sub / "state" / "usage.json").resolve()
    assert Path(cfg.auth.keys_file) == (sub / "state" / "keys.json").resolve()


def test_validate_flags_missing_advanced(tmp_path):
    cfg_path = write(
        tmp_path / "config.yaml",
        """
base:
  base_url: http://127.0.0.1:11434/v1
  model: m
""",
    )
    cfg = load_config(cfg_path)
    # 进阶槽位空着不是错误：只有本地模型的人照样能跑，只是没有外援。
    assert cfg.validate() == []
    assert any("advanced.base_url" in w for w in cfg.warnings())
    assert not any("base.base_url" in w for w in cfg.warnings())


def test_missing_base_is_still_fatal(tmp_path):
    """基础槽位不能空：没有它网关一个模型都没有。"""
    cfg_path = write(
        tmp_path / "config.yaml",
        """
advanced:
  base_url: https://api.example.com/v1
  model: adv
""",
    )
    problems = load_config(cfg_path).validate()
    assert any("base.base_url" in p for p in problems)


def test_protocol_defaults_and_rejects_unknown(tmp_path):
    cfg_path = write(
        tmp_path / "config.yaml",
        """
base:
  base_url: http://127.0.0.1:8080/v1
  model: /models/x.gguf
advanced:
  base_url: https://api.example.com/v1
  model: adv
""",
    )
    cfg = load_config(cfg_path)
    assert cfg.base.protocol == "chat"
    assert cfg.base.min_max_tokens == 1024
    assert cfg.base.retry_404 is True

    cfg.base.protocol = "websocket"
    assert any("base.protocol" in p for p in cfg.validate())


def test_lan_listen_requires_auth(tmp_path):
    cfg_path = write(
        tmp_path / "config.yaml",
        """
server:
  host: 0.0.0.0
auth:
  mode: off
base:
  base_url: http://127.0.0.1:11434/v1
  model: m
advanced:
  base_url: http://127.0.0.1:11434/v1
  model: m
""",
    )
    problems = load_config(cfg_path).validate()
    assert any("局域网" in p for p in problems)

    cfg_path2 = write(
        tmp_path / "config2.yaml",
        """
server:
  host: 0.0.0.0
auth:
  mode: required
base:
  base_url: http://127.0.0.1:11434/v1
  model: m
advanced:
  base_url: http://127.0.0.1:11434/v1
  model: m
""",
    )
    assert load_config(cfg_path2).validate() == []


def test_loopback_host_needs_no_auth(tmp_path):
    cfg_path = write(
        tmp_path / "config.yaml",
        """
server:
  host: 127.0.0.1
auth:
  mode: off
base:
  base_url: http://127.0.0.1:11434/v1
  model: m
advanced:
  base_url: http://127.0.0.1:11434/v1
  model: m
""",
    )
    assert load_config(cfg_path).validate() == []


def test_auth_mode_off_is_not_parsed_as_boolean(tmp_path):
    """YAML 1.1 会把 off 解析成 False。"""
    cfg_path = write(tmp_path / "config.yaml", "auth:\n  mode: off\n")
    assert load_config(cfg_path).auth.mode == "off"

    cfg_path2 = write(tmp_path / "config2.yaml", "auth:\n  mode: 'off'\n")
    assert load_config(cfg_path2).auth.mode == "off"


# ---------------------------------------------------------------- refs 配置


def test_refs_off_by_default(tmp_path):
    cfg = load_config(write(tmp_path / "config.yaml", "base:\n  base_url: http://x/v1\n  model: m\n"))
    assert cfg.refs.enabled is False
    assert cfg.refs.roots == []


def test_refs_enabled_without_roots_fails_validation(tmp_path):
    cfg_path = write(
        tmp_path / "config.yaml",
        """
base:
  base_url: http://127.0.0.1:11434/v1
  model: m
advanced:
  base_url: http://127.0.0.1:11434/v1
  model: m
refs:
  enabled: true
""",
    )
    problems = load_config(cfg_path).validate()
    assert any("refs.roots" in p for p in problems)


def test_refs_roots_must_be_absolute(tmp_path):
    cfg_path = write(
        tmp_path / "config.yaml",
        """
base:
  base_url: http://127.0.0.1:11434/v1
  model: m
advanced:
  base_url: http://127.0.0.1:11434/v1
  model: m
refs:
  enabled: true
  roots: ["relative/dir"]
""",
    )
    cfg = load_config(cfg_path)
    # 相对路径按配置文件目录解析掉，validate 不应该再报「不是绝对路径」
    assert cfg.validate() == []
    assert Path(cfg.refs.roots[0]).is_absolute()


def test_refs_strings_coerced_to_numbers(tmp_path):
    cfg_path = write(
        tmp_path / "config.yaml",
        """
refs:
  enabled: true
  roots: ["/tmp"]
  max_files: "7"
  grep_context: "5"
  max_total_chars: "50000"
""",
    )
    cfg = load_config(cfg_path)
    assert cfg.refs.max_files == 7
    assert cfg.refs.grep_context == 5
    assert cfg.refs.max_total_chars == 50000


def test_refs_root_as_single_string(tmp_path):
    """YAML 里写成标量而不是列表，也得能用。"""
    cfg_path = write(tmp_path / "config.yaml", f"refs:\n  enabled: true\n  roots: {tmp_path}\n")
    assert load_config(cfg_path).refs.roots == [str(tmp_path.resolve())]


def test_refs_audit_file_resolved_next_to_config(tmp_path):
    cfg = load_config(write(tmp_path / "config.yaml", "refs:\n  audit_file: state/refs.jsonl\n"))
    assert cfg.refs.audit_file == str((tmp_path / "state" / "refs.jsonl").resolve())
