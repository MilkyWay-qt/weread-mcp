"""运行配置：全部可通过环境变量或命令行参数覆盖。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Mapping

DEFAULT_BASE_URL = "https://i.weread.qq.com/api/agent/gateway"
"""微信读书 Agent API Gateway 统一入口。"""

DEFAULT_SKILL_VERSION = "1.0.4"
"""对应 Tencent/WeChatReading skills 的 version 字段，每次请求必须上报。"""

Transport = Literal["sse", "streamable-http", "stdio"]

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ValueError(f"环境变量 {key} 只接受布尔值（true/false），当前为 {raw!r}")


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:  # pragma: no cover - 仅在配置错误时触发
        raise ValueError(f"环境变量 {key} 需要是整数，当前为 {raw!r}") from exc


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:  # pragma: no cover - 仅在配置错误时触发
        raise ValueError(f"环境变量 {key} 需要是数字，当前为 {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """服务端配置。"""

    # --- 微信读书网关 ---
    api_key: str | None = None
    """服务端默认 API Key（`wrk-xxxxxxxx`），来自 WEREAD_API_KEY。"""

    base_url: str = DEFAULT_BASE_URL
    skill_version: str = DEFAULT_SKILL_VERSION
    request_timeout: float = 30.0

    allow_client_api_key: bool = True
    """允许 MCP 客户端通过 HTTP 头 `Authorization: Bearer wrk-xxx`
    或 `X-WeRead-Api-Key` 携带自己的 Key（多用户部署时使用）。"""

    # --- MCP 传输 ---
    transport: Transport = "sse"
    host: str = "127.0.0.1"
    port: int = 8000
    sse_path: str = "/sse"
    message_path: str = "/messages/"
    streamable_http_path: str = "/mcp"
    json_response: bool = False
    stateless_http: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """从环境变量构造配置。"""
        env = os.environ if env is None else env

        api_key = (env.get("WEREAD_API_KEY") or "").strip() or None
        transport = (env.get("WEREAD_MCP_TRANSPORT") or "sse").strip().lower()
        if transport not in ("sse", "streamable-http", "stdio"):
            raise ValueError(
                f"WEREAD_MCP_TRANSPORT 只支持 sse / streamable-http / stdio，当前为 {transport!r}"
            )

        log_level = (env.get("WEREAD_MCP_LOG_LEVEL") or "INFO").strip().upper()
        if log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"WEREAD_MCP_LOG_LEVEL 不合法：{log_level!r}")

        return cls(
            api_key=api_key,
            base_url=(env.get("WEREAD_API_BASE_URL") or DEFAULT_BASE_URL).strip(),
            skill_version=(env.get("WEREAD_SKILL_VERSION") or DEFAULT_SKILL_VERSION).strip(),
            request_timeout=_env_float(env, "WEREAD_REQUEST_TIMEOUT", 30.0),
            allow_client_api_key=_env_bool(env, "WEREAD_ALLOW_CLIENT_API_KEY", True),
            transport=transport,  # type: ignore[arg-type]
            host=(env.get("WEREAD_MCP_HOST") or "127.0.0.1").strip(),
            port=_env_int(env, "WEREAD_MCP_PORT", 8000),
            sse_path=(env.get("WEREAD_MCP_SSE_PATH") or "/sse").strip(),
            message_path=(env.get("WEREAD_MCP_MESSAGE_PATH") or "/messages/").strip(),
            streamable_http_path=(env.get("WEREAD_MCP_HTTP_PATH") or "/mcp").strip(),
            json_response=_env_bool(env, "WEREAD_MCP_JSON_RESPONSE", False),
            stateless_http=_env_bool(env, "WEREAD_MCP_STATELESS", False),
            log_level=log_level,  # type: ignore[arg-type]
        )
