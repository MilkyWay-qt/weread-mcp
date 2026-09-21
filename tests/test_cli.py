"""命令行参数覆盖环境变量。"""

from __future__ import annotations

import pytest

from pathlib import Path

from weread_mcp.cli import build_parser, dotenv_candidates, settings_from_args


def test_defaults_to_sse():
    settings = settings_from_args([])
    assert settings.transport == "sse"
    assert settings.sse_path == "/sse"
    assert settings.message_path == "/messages/"


def test_cli_overrides():
    settings = settings_from_args(
        ["--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9000", "--api-key", "wrk-x"]
    )
    assert settings.transport == "streamable-http"
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.api_key == "wrk-x"


def test_dotenv_is_looked_up_in_project_root():
    """stdio 被客户端拉起时工作目录不是项目目录，必须能回退到项目根的 .env。"""
    candidates = dotenv_candidates()
    project_root = Path(__file__).resolve().parents[1]
    assert candidates[-1] == project_root / ".env"


def test_unknown_transport_is_rejected():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--transport", "websocket"])
