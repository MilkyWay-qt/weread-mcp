"""把官方 weread-skills 的能力文档作为 MCP 资源暴露出去。

工具说明里只能放最关键的口径规则；完整的字段表、few-shot 和工作流仍然很有价值，
因此原样内置为资源，客户端可以按需读取：

    weread://skills            # 总纲（SKILL.md）
    weread://skills/shelf      # 书架口径
    weread://skills/readdata   # 阅读统计字段单位
    ...
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError

from .server import AppState

__all__ = ["register_resources", "SKILL_DOCS", "skills_dir", "read_skill_doc"]

SKILL_DOCS: dict[str, str] = {
    "index": "总纲：统一入口、鉴权、请求/响应格式与通用规则（官方 SKILL.md）",
    "search": "搜索：scope 选择指引、关键词提取规则、回包字段与翻页",
    "book": "书籍信息：详情、章节目录、阅读进度（progress 口径）",
    "shelf": "书架：books / albums / mp 的数量口径与公开私密统计",
    "notes": "笔记划线：统计口径与导出口径、游标分页、热门划线与想法",
    "readdata": "阅读统计：字段单位（秒）、周期组合、日均口径",
    "review": "书籍公开点评：双层嵌套结构、评分星级换算、筛选类型",
    "discover": "推荐发现：个性化推荐与相似书推荐的必填参数",
    "profile": "阅读概况：书架 + 最近进度 + 笔记的组合工作流",
}
"""资源名 -> 说明。`index` 对应官方 SKILL.md，其余对应 skills/<name>.md。"""


def skills_dir() -> Path:
    return Path(__file__).resolve().parent / "skills"


def read_skill_doc(name: str) -> str:
    """读取内置的能力文档。"""
    if name not in SKILL_DOCS:
        raise ResourceNotFoundError(
            f"未知的能力文档：{name}。可用文档：{', '.join(sorted(SKILL_DOCS))}"
        )
    filename = "SKILL.md" if name == "index" else f"{name}.md"
    path = skills_dir() / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 打包异常时才会触发
        raise ResourceNotFoundError(f"能力文档 {filename} 不存在或不可读：{exc}") from exc


def register_resources(server: MCPServer[AppState]) -> None:
    """把每份能力文档注册成一个静态资源。"""

    for name, description in SKILL_DOCS.items():
        uri = "weread://skills" if name == "index" else f"weread://skills/{name}"

        def _make_reader(doc_name: str):
            async def read_doc() -> str:
                return read_skill_doc(doc_name)

            read_doc.__name__ = f"skill_doc_{doc_name}"
            return read_doc

        server.resource(
            uri,
            name=f"weread-skill-{name}",
            title=f"微信读书能力文档 · {name}",
            description=description,
            mime_type="text/markdown",
        )(_make_reader(name))
