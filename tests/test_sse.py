"""端到端验证：真的起一个 HTTP 服务，用 MCP 客户端通过 URL(SSE) 连上来。"""

from __future__ import annotations

import socket

import anyio
import httpx2
import pytest
import uvicorn
from mcp.client import Client
from mcp.client.sse import sse_client

pytestmark = pytest.mark.anyio


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_until_up(base_url: str, timeout: float = 10.0) -> None:
    async with httpx2.AsyncClient() as client:
        with anyio.fail_after(timeout):
            while True:
                try:
                    response = await client.get(f"{base_url}/healthz")
                    if response.status_code == 200:
                        return
                except Exception:  # noqa: BLE001 - 启动过程中连接失败是正常的
                    pass
                await anyio.sleep(0.05)


async def test_sse_url_serves_mcp(make_server, gateway_recorder):
    server = make_server(
        {"/store/search": {"errcode": 0, "hasMore": 0, "results": [{"title": "电子书"}]}}
    )
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    app = server.sse_app(host="127.0.0.1", sse_path="/sse", message_path="/messages/")
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(uvicorn_server.serve)
        try:
            await _wait_until_up(base_url)

            async with httpx2.AsyncClient() as http_client:
                health = await http_client.get(f"{base_url}/healthz")
            assert health.json()["server"] == "weread-mcp"

            async with Client(sse_client(f"{base_url}/sse"), mode="legacy") as client:
                tools = await client.list_tools()
                assert "search_books" in {tool.name for tool in tools.tools}

                result = await client.call_tool("search_books", {"keyword": "三体"})
                assert result.is_error is False
                assert result.structured_content["results"][0]["title"] == "电子书"

                resources = await client.list_resources()
                assert "weread://skills" in {str(r.uri) for r in resources.resources}
        finally:
            uvicorn_server.should_exit = True

    assert gateway_recorder[-1]["body"]["api_name"] == "/store/search"


async def test_client_header_api_key_overrides_server_key(make_server, gateway_recorder):
    """多用户部署：客户端用 Authorization 头带自己的 Key。"""
    server = make_server({"/shelf/sync": {"errcode": 0, "books": [], "albums": []}})
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    app = server.sse_app(host="127.0.0.1")
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(uvicorn_server.serve)
        try:
            await _wait_until_up(base_url)
            transport = sse_client(
                f"{base_url}/sse", headers={"Authorization": "Bearer wrk-from-client"}
            )
            async with Client(transport, mode="legacy") as client:
                result = await client.call_tool("get_shelf", {})
            assert result.is_error is False
        finally:
            uvicorn_server.should_exit = True

    assert gateway_recorder[-1]["authorization"] == "Bearer wrk-from-client"
