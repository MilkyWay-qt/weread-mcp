"""命令行入口：默认以 SSE（URL）方式启动 MCP Server。"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .config import Settings, validate_auth_token

logger = logging.getLogger("weread_mcp")


def dotenv_candidates() -> list[Path]:
    """按优先级列出可能的 .env 位置。

    stdio 方式被 Claude Desktop 等客户端拉起时，工作目录通常不是项目目录，
    因此除了当前目录，还要回退到项目根目录（相对本文件定位）。
    """
    return [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]


def _load_dotenv() -> None:
    """有 python-dotenv 时自动加载 .env（可选依赖，缺失不报错）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - 可选依赖
        return
    for path in dotenv_candidates():
        if path.is_file():
            load_dotenv(path, override=False)
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weread-mcp",
        description="微信读书 MCP Server（默认 SSE 传输，通过 URL 连接）",
    )
    parser.add_argument("--version", action="version", version=f"weread-mcp {__version__}")
    parser.add_argument(
        "--transport",
        choices=["sse", "streamable-http", "stdio"],
        help="传输方式，默认 sse（可用环境变量 WEREAD_MCP_TRANSPORT 覆盖）",
    )
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1；对外服务用 0.0.0.0")
    parser.add_argument("--port", type=int, help="监听端口，默认 8000")
    parser.add_argument("--sse-path", help="SSE 端点路径，默认 /sse")
    parser.add_argument("--message-path", help="SSE 回传消息路径，默认 /messages/")
    parser.add_argument("--http-path", help="Streamable HTTP 端点路径，默认 /mcp")
    parser.add_argument(
        "--json-response",
        action="store_true",
        help="Streamable HTTP 使用 JSON 响应而非 SSE 流",
    )
    parser.add_argument(
        "--stateless",
        action="store_true",
        help="Streamable HTTP 无状态模式（适合多副本部署）",
    )
    parser.add_argument("--api-key", help="微信读书 API Key，优先级高于 WEREAD_API_KEY")
    parser.add_argument(
        "--auth-token",
        help="MCP 访问令牌（公网部署必设），客户端需带 Authorization: Bearer <token>；"
        "等价于 WEREAD_MCP_AUTH_TOKEN",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        metavar="DOMAIN",
        help="反向代理/隧道对外的域名，可重复；等价于 WEREAD_MCP_ALLOWED_HOSTS（逗号分隔）",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="日志级别，默认 INFO",
    )
    return parser


def settings_from_args(argv: Sequence[str] | None = None) -> Settings:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()

    overrides: dict[str, object] = {}
    if args.transport:
        overrides["transport"] = args.transport
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    if args.sse_path:
        overrides["sse_path"] = args.sse_path
    if args.message_path:
        overrides["message_path"] = args.message_path
    if args.http_path:
        overrides["streamable_http_path"] = args.http_path
    if args.json_response:
        overrides["json_response"] = True
    if args.stateless:
        overrides["stateless_http"] = True
    if args.api_key:
        overrides["api_key"] = args.api_key
    if args.log_level:
        overrides["log_level"] = args.log_level
    if args.auth_token:
        overrides["auth_token"] = validate_auth_token(args.auth_token)
    if args.allowed_host:
        overrides["allowed_hosts"] = tuple(h.strip() for h in args.allowed_host if h.strip())

    return dataclasses.replace(settings, **overrides)  # type: ignore[arg-type]


def main(argv: Sequence[str] | None = None) -> int:
    _load_dotenv()
    settings = settings_from_args(argv)

    from .server import create_server

    server = create_server(settings)

    if settings.transport == "stdio":
        server.run(transport="stdio")
        return 0

    base = f"http://{settings.host}:{settings.port}"
    if settings.transport == "sse":
        endpoint = f"{base}{settings.sse_path}"
    else:
        endpoint = f"{base}{settings.streamable_http_path}"

    print(
        f"weread-mcp {__version__} · transport={settings.transport}\n"
        f"  MCP endpoint : {endpoint}\n"
        f"  health check : {base}/healthz\n"
        f"  API Key      : {'已配置（服务端）' if settings.api_key else '未配置，需由客户端请求头携带'}\n"
        f"  访问令牌     : {'已启用' if settings.auth_token else '未启用'}"
        + (f"\n  允许的域名   : {', '.join(settings.allowed_hosts)}" if settings.allowed_hosts else ""),
        file=sys.stderr,
        flush=True,
    )
    publicly_reachable = settings.host not in ("127.0.0.1", "localhost", "::1") or bool(settings.allowed_hosts)
    if not settings.auth_token and publicly_reachable:
        print(
            "  ⚠️  正在监听非本机地址但未设置访问令牌：任何能访问该端口的人都能读取你的微信读书数据。\n"
            "      请设置 WEREAD_MCP_AUTH_TOKEN（或 --auth-token）。",
            file=sys.stderr,
            flush=True,
        )

    import uvicorn

    from .http_app import build_http_app

    uvicorn.run(
        build_http_app(server, settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
