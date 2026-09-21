from __future__ import annotations

import json
from typing import Any, Callable

import httpx2
import pytest

from weread_mcp.config import Settings
from weread_mcp.server import create_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    return Settings(api_key="wrk-test-key", transport="sse", log_level="WARNING")


@pytest.fixture
def gateway_recorder() -> list[dict[str, Any]]:
    """收集所有发往网关的请求，便于断言参数拼装是否正确。"""
    return []


@pytest.fixture
def make_server(
    settings: Settings, gateway_recorder: list[dict[str, Any]]
) -> Callable[..., Any]:
    """构造一个把网关请求接到内存 mock 上的 MCPServer。"""

    def factory(responses: dict[str, Any] | None = None, *, custom: Settings | None = None):
        table = responses or {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content.decode("utf-8"))
            gateway_recorder.append(
                {
                    "body": body,
                    "authorization": request.headers.get("authorization"),
                }
            )
            payload = table.get(body["api_name"], {"errcode": 0})
            if isinstance(payload, httpx2.Response):
                return payload
            return httpx2.Response(200, json=payload)

        def client_factory() -> httpx2.AsyncClient:
            return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))

        return create_server(custom or settings, client_factory=client_factory)

    return factory
