"""构造微信读书 MCP Server。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional

import httpx2
from mcp.server.mcpserver import MCPServer

from .config import Settings
from .gateway import WeReadGateway

logger = logging.getLogger(__name__)

__all__ = ["AppState", "create_server", "SERVER_INSTRUCTIONS"]


SERVER_INSTRUCTIONS = """\
微信读书（WeRead）MCP Server —— 搜索书籍、书架、笔记划线、书评、阅读统计、推荐发现。

## 鉴权
所有工具通过服务端配置的 `WEREAD_API_KEY`（格式 `wrk-xxxxxxxx`）调用微信读书 Agent 网关。
API Key 绑定用户身份（vid），需要用户身份的接口会自动注入，无需传 vid。
如果服务端未配置，也可由客户端在 HTTP 头中携带 `Authorization: Bearer wrk-xxxxxxxx`
或 `X-WeRead-Api-Key: wrk-xxxxxxxx`。

## 通用规则（来自官方 weread-skills，务必遵守）
1. bookId 解析：用户给书名时，先用 `search_books` 拿到 bookId，再执行后续操作；对话中记住已查到的 bookId。
2. 只依据接口返回数据作答，不编造、不推断接口未返回的信息；字段含义以工具说明为准，禁止按字段名直译。
3. 时间戳：所有 Unix 时间戳（updateTime / createTime / finishTime / readUpdateTime 等）展示时转为 `YYYY-MM-DD`。
4. 阅读时长：所有时长字段单位为**秒**，展示时转为「X小时Y分钟」。
5. 阅读进度 `book.progress` 是 0-100 的整数，展示必须带 `%`；只有 `progress=100` 且有 `finishTime` 才算读完。
6. 书架数量：`books.length + albums.length + (mp 非空 ? 1 : 0)`，`albums` 是专辑/有声书，也算书架里的书。
7. 笔记数量：`reviewCount + noteCount + bookmarkCount`；`noteCount` 是划线条数，不是总笔记数。
8. 深度链接：回包里有 `deepLink` 就展示为 `[打开阅读]({deepLink})`；没有就不要自己拼 `weread://`。
9. 搜索结果是分页片段，用「为您找到」，不要说「共有 / 一共 / 总共」。
10. 不要向用户暴露参数拼装、scope 选择等中间推理过程。

## 更完整的字段说明
官方能力文档已作为 MCP 资源提供，URI 形如 `weread://skills/shelf`、`weread://skills/notes`、
`weread://skills/readdata` 等，回答涉及字段口径时可先读取对应资源。
"""


@dataclass
class AppState:
    """lifespan 期间共享的服务端状态。"""

    settings: Settings
    gateway: WeReadGateway


ClientFactory = Callable[[], "httpx2.AsyncClient"]


def create_server(
    settings: Settings | None = None,
    *,
    client_factory: Optional[ClientFactory] = None,
) -> MCPServer[AppState]:
    """创建并返回配置好的 MCPServer 实例。

    Args:
        settings: 运行配置，默认从环境变量读取。
        client_factory: 自定义 HTTP 客户端工厂，测试时可注入 mock transport。
    """
    from . import __version__
    from .resources import register_resources
    from .tools import register_tools

    resolved = settings or Settings.from_env()

    def _default_client_factory() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            timeout=httpx2.Timeout(resolved.request_timeout),
            headers={"User-Agent": f"weread-mcp/{__version__}"},
        )

    make_client = client_factory or _default_client_factory

    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppState]) -> AsyncIterator[AppState]:
        async with make_client() as client:
            gateway = WeReadGateway(
                client,
                base_url=resolved.base_url,
                skill_version=resolved.skill_version,
                default_api_key=resolved.api_key,
            )
            if not resolved.api_key:
                logger.warning(
                    "未设置 WEREAD_API_KEY；每次调用需由客户端在请求头中携带 API Key。"
                )
            yield AppState(settings=resolved, gateway=gateway)

    server: MCPServer[AppState] = MCPServer(
        name="weread",
        title="微信读书",
        description="微信读书助手：搜索书籍、书架、笔记划线、书评、阅读统计、推荐发现",
        instructions=SERVER_INSTRUCTIONS,
        website_url="https://weread.qq.com/",
        version=__version__,
        lifespan=lifespan,
        log_level=resolved.log_level,
    )

    register_tools(server)
    register_resources(server)
    _register_health_route(server, resolved)
    return server


def _register_health_route(server: MCPServer[AppState], settings: Settings) -> None:
    """一个公开的健康检查端点，方便放在反向代理/容器编排后面。"""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from . import __version__

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:  # pragma: no cover - 简单路由
        return JSONResponse(
            {
                "status": "ok",
                "server": "weread-mcp",
                "version": __version__,
                "transport": settings.transport,
                "skill_version": settings.skill_version,
                "server_api_key_configured": bool(settings.api_key),
            }
        )
