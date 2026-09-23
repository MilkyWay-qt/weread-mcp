"""HTTP 传输（SSE / Streamable HTTP）的应用组装：访问令牌鉴权 + Host 白名单。

部署到公网（HTTPS 反向代理、Cloudflare Tunnel、云平台）时需要这一层：

* **访问令牌**：公网 URL 背后是你的微信读书账号，任何拿到 URL 的人都能读取你的书架和笔记。
  设置 `WEREAD_MCP_AUTH_TOKEN` 后，所有 MCP 请求都必须带
  `Authorization: Bearer <token>`（或 `X-API-Key: <token>`）。这个令牌与微信读书
  API Key 完全无关，可随时更换；微信读书 API Key 只存在于服务端，永远不会发给客户端。
* **Host 白名单**：服务监听 127.0.0.1 时，SDK 会自动开启 DNS rebinding 防护，只接受
  `Host: localhost/127.0.0.1`。经过反向代理或隧道转发的请求带的是公网域名，需要把域名
  加入 `WEREAD_MCP_ALLOWED_HOSTS`，否则会被 421 拒绝。
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Iterable

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import Settings

logger = logging.getLogger(__name__)

__all__ = ["BearerAuthMiddleware", "build_http_app", "transport_security_for"]

PUBLIC_PATHS = frozenset({"/healthz"})
"""不需要访问令牌的路径。"""

_LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOCAL_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


class BearerAuthMiddleware:
    """纯 ASGI 中间件：校验访问令牌，放行健康检查与 CORS 预检。"""

    def __init__(self, app: ASGIApp, token: str, public_paths: Iterable[str] = PUBLIC_PATHS) -> None:
        self.app = app
        self._token = token.encode("utf-8")
        self._public_paths = frozenset(public_paths)

    def _extract(self, scope: Scope) -> bytes | None:
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").strip()
        if authorization[:7].lower() == b"bearer ":
            candidate = authorization[7:].strip()
            if candidate:
                return candidate
        api_key = headers.get(b"x-api-key", b"").strip()
        return api_key or None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan 等直接透传
            await self.app(scope, receive, send)
            return
        if scope.get("path") in self._public_paths or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        provided = self._extract(scope)
        if provided is not None and hmac.compare_digest(provided, self._token):
            await self.app(scope, receive, send)
            return

        client = scope.get("client") or ("?", 0)
        logger.warning("拒绝未授权的 MCP 请求：%s %s 来自 %s", scope.get("method"), scope.get("path"), client[0])
        body = json.dumps(
            {"error": "unauthorized", "message": "缺少或错误的访问令牌（Authorization: Bearer <token>）"},
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b'Bearer realm="weread-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def transport_security_for(settings: Settings) -> TransportSecuritySettings | None:
    """根据配置决定 DNS rebinding 防护策略。

    未配置 `allowed_hosts` 时返回 None，沿用 SDK 默认行为（监听 localhost 时自动只允许本机 Host）。
    """
    if not settings.allowed_hosts:
        return None
    hosts = list(_LOCAL_HOSTS)
    origins = list(_LOCAL_ORIGINS)
    for host in settings.allowed_hosts:
        hosts.extend([host, f"{host}:*"])
        origins.extend([f"https://{host}", f"http://{host}"])
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def build_http_app(server: MCPServer, settings: Settings) -> ASGIApp:
    """按配置组装 SSE 或 Streamable HTTP 的 ASGI 应用。"""
    security = transport_security_for(settings)

    if settings.transport == "sse":
        app: ASGIApp = server.sse_app(
            sse_path=settings.sse_path,
            message_path=settings.message_path,
            transport_security=security,
            host=settings.host,
        )
    elif settings.transport == "streamable-http":
        app = server.streamable_http_app(
            streamable_http_path=settings.streamable_http_path,
            json_response=settings.json_response,
            stateless_http=settings.stateless_http,
            transport_security=security,
            host=settings.host,
        )
    else:  # pragma: no cover - stdio 不走 HTTP
        raise ValueError(f"transport={settings.transport} 不是 HTTP 传输")

    if settings.auth_token:
        app = BearerAuthMiddleware(app, settings.auth_token)
    return app
