"""weread-mcp —— 把官方微信读书 skills 转成基于 URL(SSE) 的 MCP Server。"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__", "create_server", "Settings"]


def __getattr__(name: str):  # pragma: no cover - 惰性导入，避免 import 包即拉起依赖
    if name == "create_server":
        from .server import create_server

        return create_server
    if name == "Settings":
        from .config import Settings

        return Settings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
