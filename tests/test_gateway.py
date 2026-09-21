"""网关客户端的单元测试：参数平铺、错误归一化、Key 处理。"""

from __future__ import annotations

import json

import httpx2
import pytest

from weread_mcp.config import DEFAULT_BASE_URL, Settings
from weread_mcp.gateway import (
    GatewayBusinessError,
    GatewayHTTPStatusError,
    GatewayTransportError,
    MissingApiKeyError,
    WeReadGateway,
    mask_api_key,
)

pytestmark = pytest.mark.anyio


def _gateway(handler, *, default_api_key: str | None = "wrk-default") -> WeReadGateway:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return WeReadGateway(
        client,
        base_url=DEFAULT_BASE_URL,
        skill_version="1.0.4",
        default_api_key=default_api_key,
    )


def test_build_body_flattens_params_and_drops_none():
    gateway = _gateway(lambda request: httpx2.Response(200, json={}))
    body = gateway.build_body("/user/notebooks", {"count": 100, "lastSort": None})

    assert body == {"api_name": "/user/notebooks", "skill_version": "1.0.4", "count": 100}
    # 业务参数必须平铺，不能出现 params/data/body 嵌套
    assert "params" not in body


def test_build_body_cannot_be_hijacked():
    gateway = _gateway(lambda request: httpx2.Response(200, json={}))
    body = gateway.build_body("/store/search", {"api_name": "/evil", "skill_version": "0.0.1"})
    assert body == {"api_name": "/store/search", "skill_version": "1.0.4"}


def test_mask_api_key():
    assert mask_api_key(None) == "<empty>"
    assert mask_api_key("wrk-1234567890").startswith("wrk-12")
    assert "1234567890" not in mask_api_key("wrk-1234567890")


async def test_call_sends_bearer_token_and_returns_payload():
    seen: dict[str, object] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx2.Response(200, json={"errcode": 0, "sid": "s-1"})

    gateway = _gateway(handler)
    result = await gateway.call("/store/search", {"keyword": "三体", "scope": 10})

    assert result["sid"] == "s-1"
    assert seen["auth"] == "Bearer wrk-default"
    assert seen["body"] == {
        "api_name": "/store/search",
        "skill_version": "1.0.4",
        "keyword": "三体",
        "scope": 10,
    }


async def test_per_call_api_key_overrides_default():
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers.get("authorization") == "Bearer wrk-caller"
        return httpx2.Response(200, json={})

    gateway = _gateway(handler)
    await gateway.call("/shelf/sync", api_key="wrk-caller")


async def test_missing_api_key_raises():
    gateway = _gateway(lambda request: httpx2.Response(200, json={}), default_api_key=None)
    with pytest.raises(MissingApiKeyError):
        await gateway.call("/shelf/sync")


async def test_business_error_is_raised():
    gateway = _gateway(
        lambda request: httpx2.Response(200, json={"errcode": -2012, "errmsg": "登录态失效"})
    )
    with pytest.raises(GatewayBusinessError) as excinfo:
        await gateway.call("/shelf/sync")
    assert excinfo.value.errcode == -2012
    assert "登录态失效" in str(excinfo.value)


async def test_http_error_is_raised():
    gateway = _gateway(lambda request: httpx2.Response(500, text="boom"))
    with pytest.raises(GatewayHTTPStatusError) as excinfo:
        await gateway.call("/shelf/sync")
    assert excinfo.value.status_code == 500


async def test_non_json_response_is_transport_error():
    gateway = _gateway(lambda request: httpx2.Response(200, text="<html>"))
    with pytest.raises(GatewayTransportError):
        await gateway.call("/shelf/sync")


async def test_upgrade_info_is_passed_through(caplog):
    gateway = _gateway(
        lambda request: httpx2.Response(
            200, json={"errcode": 0, "upgrade_info": {"message": "请升级到 1.0.5"}}
        )
    )
    result = await gateway.call("/shelf/sync")
    assert result["upgrade_info"]["message"] == "请升级到 1.0.5"


def test_settings_from_env_overrides():
    settings = Settings.from_env(
        {
            "WEREAD_API_KEY": " wrk-abc ",
            "WEREAD_MCP_TRANSPORT": "streamable-http",
            "WEREAD_MCP_PORT": "9001",
            "WEREAD_ALLOW_CLIENT_API_KEY": "false",
        }
    )
    assert settings.api_key == "wrk-abc"
    assert settings.transport == "streamable-http"
    assert settings.port == 9001
    assert settings.allow_client_api_key is False


def test_settings_rejects_bad_transport():
    with pytest.raises(ValueError):
        Settings.from_env({"WEREAD_MCP_TRANSPORT": "websocket"})
