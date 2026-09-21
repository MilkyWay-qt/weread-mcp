"""微信读书 Agent API Gateway 客户端。

所有能力都走同一个入口：

    POST https://i.weread.qq.com/api/agent/gateway
    Authorization: Bearer $WEREAD_API_KEY
    {"api_name": "/store/search", "keyword": "三体", "skill_version": "1.0.4"}

业务参数必须和 `api_name`、`skill_version` **平铺在同一层**，不能包在
`params` / `data` / `body` 里，否则后端收不到参数（表现为分页失效等）。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import httpx2

logger = logging.getLogger(__name__)

__all__ = [
    "WeReadError",
    "MissingApiKeyError",
    "GatewayTransportError",
    "GatewayHTTPStatusError",
    "GatewayBusinessError",
    "WeReadGateway",
    "mask_api_key",
]

MISSING_API_KEY_MESSAGE = (
    "未配置微信读书 API Key。\n"
    "1) 前往 https://weread.qq.com/r/weread-skills 获取 API Key（格式 wrk-xxxxxxxx）\n"
    "2) 服务端设置环境变量：export WEREAD_API_KEY=wrk-xxxxxxxx 后重启本 MCP Server\n"
    "   或在 MCP 客户端的请求头中携带 Authorization: Bearer wrk-xxxxxxxx"
)


def mask_api_key(api_key: str | None) -> str:
    """日志/报错里只展示脱敏后的 Key。"""
    if not api_key:
        return "<empty>"
    if len(api_key) <= 8:
        return api_key[:2] + "***"
    return f"{api_key[:6]}***{api_key[-2:]}"


class WeReadError(Exception):
    """微信读书网关相关错误的基类。"""


class MissingApiKeyError(WeReadError):
    """没有可用的 API Key。"""

    def __init__(self, message: str = MISSING_API_KEY_MESSAGE) -> None:
        super().__init__(message)


class GatewayTransportError(WeReadError):
    """网络层失败（连接失败 / 超时 / 响应不是 JSON）。"""


class GatewayHTTPStatusError(WeReadError):
    """网关返回了非 2xx 状态码。"""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"网关返回 HTTP {status_code}：{body[:500]}")


class GatewayBusinessError(WeReadError):
    """网关返回 errcode != 0。"""

    def __init__(self, errcode: Any, errmsg: str, payload: Mapping[str, Any]) -> None:
        self.errcode = errcode
        self.errmsg = errmsg
        self.payload = dict(payload)
        super().__init__(f"接口返回错误（errcode={errcode}）：{errmsg}")


class WeReadGateway:
    """对 Agent API Gateway 的薄封装，负责鉴权、参数平铺与错误归一化。"""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        base_url: str,
        skill_version: str,
        default_api_key: str | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._skill_version = skill_version
        self._default_api_key = default_api_key

    @property
    def skill_version(self) -> str:
        return self._skill_version

    def build_body(self, api_name: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """拼装请求体：业务参数平铺，值为 None 的参数直接省略。"""
        body: dict[str, Any] = {"api_name": api_name, "skill_version": self._skill_version}
        for key, value in (params or {}).items():
            if value is None:
                continue
            if key in ("api_name", "skill_version"):
                continue
            body[key] = value
        return body

    def resolve_api_key(self, api_key: str | None = None) -> str:
        key = (api_key or self._default_api_key or "").strip()
        if not key:
            raise MissingApiKeyError()
        return key

    async def call(
        self,
        api_name: str,
        params: Mapping[str, Any] | None = None,
        *,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """调用一个网关接口，返回解析后的 JSON 对象。"""
        key = self.resolve_api_key(api_key)
        body = self.build_body(api_name, params)

        logger.debug(
            "weread gateway call api_name=%s params=%s key=%s",
            api_name,
            sorted(k for k in body if k not in ("api_name", "skill_version")),
            mask_api_key(key),
        )

        try:
            response = await self._client.post(
                self._base_url,
                json=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
        except Exception as exc:  # noqa: BLE001 - httpx2 的异常层级在此统一收敛
            raise GatewayTransportError(f"请求微信读书网关失败：{exc}") from exc

        if response.status_code >= 400:
            raise GatewayHTTPStatusError(response.status_code, response.text)

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise GatewayTransportError(
                f"网关响应不是合法 JSON：{response.text[:500]}"
            ) from exc

        if not isinstance(data, dict):
            return {"data": data}

        errcode = data.get("errcode")
        if errcode not in (None, 0):
            errmsg = str(data.get("errmsg") or data.get("errMsg") or "接口返回未知错误")
            raise GatewayBusinessError(errcode, errmsg, data)

        upgrade_info = data.get("upgrade_info")
        if upgrade_info:
            logger.warning(
                "微信读书网关要求升级 skill_version（当前 %s）：%s",
                self._skill_version,
                upgrade_info,
            )

        return data
