"""backend.config 的 .env 加载：只补缺失项，已有环境变量优先。"""

import os

from backend.config import _load_env_file


def test_loads_missing_values(tmp_path, monkeypatch):
    monkeypatch.delenv("QODER_ENV_TEST_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("QODER_ENV_TEST_KEY=from-file\n", encoding="utf-8")

    _load_env_file(env_file)

    assert os.environ["QODER_ENV_TEST_KEY"] == "from-file"


def test_existing_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("QODER_ENV_TEST_KEY", "from-shell")
    env_file = tmp_path / ".env"
    env_file.write_text("QODER_ENV_TEST_KEY=from-file\n", encoding="utf-8")

    _load_env_file(env_file)

    assert os.environ["QODER_ENV_TEST_KEY"] == "from-shell"


def test_parses_export_quotes_comments_and_bom(tmp_path, monkeypatch):
    monkeypatch.delenv("QODER_ENV_TEST_A", raising=False)
    monkeypatch.delenv("QODER_ENV_TEST_B", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\ufeff# 注释\n\n"
        'export QODER_ENV_TEST_A="a b"\n'
        "QODER_ENV_TEST_B='b'\n"
        "not a pair\n",
        encoding="utf-8",
    )

    _load_env_file(env_file)

    assert os.environ["QODER_ENV_TEST_A"] == "a b"
    assert os.environ["QODER_ENV_TEST_B"] == "b"


def test_missing_file_is_noop(tmp_path):
    _load_env_file(tmp_path / "missing.env")
