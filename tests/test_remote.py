"""公网部署相关：访问令牌鉴权、Host 白名单、令牌与微信读书 Key 的隔离。"""

from __future__ import annotations

import dataclasses
import socket
from contextlib import asynccontextmanager

import anyio
import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.sse import sse_client

from weread_mcp.config import Settings, validate_auth_token
from weread_mcp.http_app import build_http_app

pytestmark = pytest.mark.anyio

TOKEN = "t0k3n-for-tests-0123456789abcdef"
INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def _serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(server.serve)
        async with httpx2.AsyncClient() as probe:
            with anyio.fail_after(10):
                while True:
                    try:
                        if (await probe.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                            break
                    except Exception:  # noqa: BLE001 - 启动中
                        pass
                    await anyio.sleep(0.05)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True


def _remote_settings(settings: Settings, **overrides) -> Settings:
    return dataclasses.replace(settings, auth_token=TOKEN, **overrides)


def test_short_auth_token_is_rejected():
    with pytest.raises(ValueError):
        validate_auth_token("short")
    assert validate_auth_token("  ") is None


def test_allowed_hosts_from_env():
    settings = Settings.from_env(
        {"WEREAD_MCP_ALLOWED_HOSTS": "weread.example.com, mcp.example.org", "WEREAD_MCP_AUTH_TOKEN": TOKEN}
    )
    assert settings.allowed_hosts == ("weread.example.com", "mcp.example.org")
    assert settings.auth_token == TOKEN


async def test_streamable_http_requires_token(make_server, settings):
    remote = _remote_settings(settings, transport="streamable-http", json_response=True)
    app = build_http_app(make_server(custom=remote), remote)

    async with _serve(app, _free_port()) as base:
        async with httpx2.AsyncClient() as http:
            health = await http.get(f"{base}/healthz")
            assert health.status_code == 200 and health.json()["auth_required"] is True

            missing = await http.post(f"{base}/mcp", json=INIT_REQUEST, headers=MCP_HEADERS)
            assert missing.status_code == 401
            assert missing.headers["www-authenticate"].startswith("Bearer")

            wrong = await http.post(
                f"{base}/mcp", json=INIT_REQUEST, headers={**MCP_HEADERS, "Authorization": "Bearer nope"}
            )
            assert wrong.status_code == 401

            ok = await http.post(
                f"{base}/mcp", json=INIT_REQUEST, headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"}
            )
            assert ok.status_code == 200
            assert ok.json()["result"]["serverInfo"]["name"] == "weread"

            via_api_key = await http.post(
                f"{base}/mcp", json=INIT_REQUEST, headers={**MCP_HEADERS, "X-API-Key": TOKEN}
            )
            assert via_api_key.status_code == 200


async def test_sse_with_token_and_key_isolation(make_server, settings, gateway_recorder):
    """访问令牌放在 Authorization 头里，绝不能被当成微信读书 Key 转发给网关。"""
    remote = _remote_settings(settings, transport="sse")
    server = make_server({"/shelf/sync": {"errcode": 0, "books": [], "albums": []}}, custom=remote)
    app = build_http_app(server, remote)

    async with _serve(app, _free_port()) as base:
        async with httpx2.AsyncClient() as http:
            rejected = await http.get(f"{base}/sse")
            assert rejected.status_code == 401

        transport = sse_client(f"{base}/sse", headers={"Authorization": f"Bearer {TOKEN}"})
        async with Client(transport, mode="legacy") as client:
            result = await client.call_tool("get_shelf", {})
        assert result.is_error is False

    # 网关收到的是服务端配置的微信读书 Key，而不是访问令牌
    assert gateway_recorder[-1]["authorization"] == "Bearer wrk-test-key"


async def test_proxy_host_needs_allowlist(make_server, settings):
    """监听 127.0.0.1、经反向代理转发（Host 为公网域名）时，必须配置 allowed_hosts。"""
    public_host = {"Host": "weread.example.com"}
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}", **public_host}

    blocked = _remote_settings(settings, transport="streamable-http", json_response=True)
    async with _serve(build_http_app(make_server(custom=blocked), blocked), _free_port()) as base:
        async with httpx2.AsyncClient() as http:
            response = await http.post(f"{base}/mcp", json=INIT_REQUEST, headers=headers)
        assert response.status_code == 421

    allowed = dataclasses.replace(blocked, allowed_hosts=("weread.example.com",))
    async with _serve(build_http_app(make_server(custom=allowed), allowed), _free_port()) as base:
        async with httpx2.AsyncClient() as http:
            response = await http.post(f"{base}/mcp", json=INIT_REQUEST, headers=headers)
        assert response.status_code == 200
