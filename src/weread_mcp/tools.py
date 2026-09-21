"""把官方 weread-skills 的每个网关接口暴露成一个 MCP 工具。

每个工具的 docstring 就是模型看到的 description，因此这里保留了官方文档中
**容易出错的口径规则**（单位、计数方式、分页方式、字段歧义）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .gateway import (
    GatewayBusinessError,
    GatewayHTTPStatusError,
    GatewayTransportError,
    MissingApiKeyError,
    WeReadGateway,
)
from .server import AppState

__all__ = ["register_tools", "format_duration"]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
"""微信读书网关目前全部是只读查询接口。"""


# --------------------------------------------------------------------------------------
# 内部工具函数
# --------------------------------------------------------------------------------------


def _state(ctx: Context[AppState]) -> AppState:
    state = ctx.request_context.lifespan_context
    if not isinstance(state, AppState):  # pragma: no cover - 理论上不会发生
        raise ToolError("服务端状态未初始化")
    return state


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    value = headers.get(name)
    if value is None:
        lowered = name.lower()
        for key, candidate in headers.items():
            if key.lower() == lowered:
                value = candidate
                break
    return value.strip() if isinstance(value, str) and value.strip() else None


def _resolve_api_key(ctx: Context[AppState]) -> str | None:
    """优先使用客户端请求头里的 Key，其次用服务端环境变量里的 Key。"""
    state = _state(ctx)
    if state.settings.allow_client_api_key:
        authorization = _header(ctx.headers, "Authorization")
        if authorization and authorization.lower().startswith("bearer "):
            candidate = authorization[7:].strip()
            if candidate:
                return candidate
        candidate = _header(ctx.headers, "X-WeRead-Api-Key")
        if candidate:
            return candidate
    return None


def _gateway(ctx: Context[AppState]) -> WeReadGateway:
    return _state(ctx).gateway


async def _call(
    ctx: Context[AppState],
    api_name: str,
    params: Mapping[str, Any] | None = None,
    *,
    failure_message: str = "接口调用失败，请稍后再试~",
) -> dict[str, Any]:
    """调用网关并把异常转换成对模型友好的 ToolError。"""
    try:
        return await _gateway(ctx).call(api_name, params, api_key=_resolve_api_key(ctx))
    except MissingApiKeyError as exc:
        raise ToolError(str(exc)) from exc
    except GatewayBusinessError as exc:
        raise ToolError(f"{failure_message}（errcode={exc.errcode}：{exc.errmsg}）") from exc
    except GatewayHTTPStatusError as exc:
        if exc.status_code in (401, 403):
            raise ToolError(
                "微信读书鉴权失败：API Key 可能无效或已过期，"
                "请到 https://weread.qq.com/r/weread-skills 重新获取后更新 WEREAD_API_KEY。"
            ) from exc
        raise ToolError(f"{failure_message}（HTTP {exc.status_code}）") from exc
    except GatewayTransportError as exc:
        raise ToolError(f"{failure_message}（{exc}）") from exc


def format_duration(seconds: Any) -> str | None:
    """把秒数转成「X小时Y分钟」，非法输入返回 None。"""
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}小时{minutes}分钟"
    if hours:
        return f"{hours}小时"
    return f"{minutes}分钟"


def _reading_seconds(book: Mapping[str, Any]) -> Any:
    """单本书累计阅读时长（秒）。

    实测 `/book/getprogress`：真正的累计时长在 `readingTime`，
    `recordReadingTime` 是朗读/记录类时长，普通阅读时通常为 0
    （与 readdata.md 的定义一致，官方 book.md 的描述有误）。
    优先取 readingTime，缺失时再回退到 recordReadingTime。
    """
    value = book.get("readingTime")
    if value is None:
        value = book.get("recordReadingTime")
    return value


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _shelf_counts(payload: Mapping[str, Any]) -> dict[str, Any]:
    """按 shelf.md 的强制口径计算书架数量。"""
    books = _as_list(payload.get("books"))
    albums = _as_list(payload.get("albums"))
    has_mp = 1 if payload.get("mp") else 0

    secret_books = sum(1 for b in books if isinstance(b, dict) and b.get("secret") == 1)
    secret_albums = sum(
        1
        for a in albums
        if isinstance(a, dict)
        and isinstance(a.get("albumInfoExtra"), dict)
        and a["albumInfoExtra"].get("secret") == 1
    )
    public_books = sum(1 for b in books if isinstance(b, dict) and b.get("secret") == 0)
    public_albums = sum(
        1
        for a in albums
        if isinstance(a, dict)
        and isinstance(a.get("albumInfoExtra"), dict)
        and a["albumInfoExtra"].get("secret") == 0
    )

    return {
        "shelfItemTotal": len(books) + len(albums) + has_mp,
        "ebookCount": len(books),
        "albumCount": len(albums),
        "articleCollectionCount": has_mp,
        "secretCount": secret_books + secret_albums + has_mp,
        "publicCount": public_books + public_albums,
        "formula": "books.length + albums.length + (mp 非空 ? 1 : 0)",
    }


def _notebook_counts(payload: Mapping[str, Any]) -> dict[str, Any]:
    """按 notes.md 的统计口径给出每本书的总笔记数（降序）。"""
    rows: list[dict[str, Any]] = []
    for entry in _as_list(payload.get("books")):
        if not isinstance(entry, dict):
            continue
        book = entry.get("book") if isinstance(entry.get("book"), dict) else {}
        review_count = int(entry.get("reviewCount") or 0)
        note_count = int(entry.get("noteCount") or 0)
        bookmark_count = int(entry.get("bookmarkCount") or 0)
        rows.append(
            {
                "bookId": entry.get("bookId"),
                "title": book.get("title"),
                "author": book.get("author"),
                "reviewCount": review_count,
                "noteCount": note_count,
                "bookmarkCount": bookmark_count,
                "totalNoteCount": review_count + note_count + bookmark_count,
                "sort": entry.get("sort"),
            }
        )
    rows.sort(key=lambda row: row["totalNoteCount"], reverse=True)
    return {
        "formula": "reviewCount + noteCount + bookmarkCount",
        "noteCountByBook": rows,
        "pageTotalNoteCount": sum(row["totalNoteCount"] for row in rows),
    }


# --------------------------------------------------------------------------------------
# 工具注册
# --------------------------------------------------------------------------------------


def register_tools(server: MCPServer[AppState]) -> None:  # noqa: C901 - 注册函数天然较长
    """把全部微信读书能力注册为 MCP 工具。"""

    # ---------------------------------------------------------------- 搜索
    @server.tool(title="搜索书籍", annotations=READ_ONLY)
    async def search_books(
        ctx: Context[AppState],
        keyword: Annotated[
            str,
            Field(
                description=(
                    "搜索关键词。需要先去掉「帮我 / 搜一下 / 有没有」等口语化前后缀，"
                    "只保留核心检索词；同时给出书名和作者时用区分度更高的词。"
                )
            ),
        ],
        scope: Annotated[
            int,
            Field(
                description=(
                    "搜索类型，必须显式传：0=全部（泛搜索），10=电子书（明确找书/要 bookId），"
                    "16=网文小说，14=微信听书/有声书/专辑/播客，6=作者，12=全文，13=书单，"
                    "2=公众号，4=文章。"
                )
            ),
        ] = 10,
        count: Annotated[
            int | None,
            Field(description="每页数量。用户没有指定数量时不要传（服务端默认 15）。"),
        ] = None,
        max_idx: Annotated[
            int | None,
            Field(description="翻页偏移：取上一页最后一条的 searchIdx。首页不传。"),
        ] = None,
    ) -> dict[str, Any]:
        """在微信读书书城搜索（`/store/search`）。

        用户说书名时先用本工具拿 bookId，再调用其它工具。

        注意：请求参数 `scope` 和回包 `results[].scope` 不是一回事——请求 `scope=10` 时
        电子书分组回包可能是 `scope=17`，**不要**用 `results[].scope == 10` 过滤结果；
        标题为「电子书」或含 `books` 的分组都可以展示。

        翻页：`hasMore=1` 时，用最后一条的 `searchIdx` 作为下一次的 `max_idx`。
        搜索结果只是分页片段，表述用「为您找到」，不要说「共有/一共/总共」。
        空结果时回复：抱歉，没有找到与「{keyword}」相关的结果。
        """
        return await _call(
            ctx,
            "/store/search",
            {"keyword": keyword, "scope": scope, "count": count, "maxIdx": max_idx},
            failure_message="搜索服务暂时不可用，请稍后再试。如果持续出现问题，可以换个关键词尝试。",
        )

    # ---------------------------------------------------------------- 书籍信息
    @server.tool(title="书籍详情", annotations=READ_ONLY)
    async def get_book_info(
        ctx: Context[AppState],
        book_id: Annotated[str, Field(description="书籍 ID，来自 search_books 等接口的 bookId")],
    ) -> dict[str, Any]:
        """获取书籍基本信息（`/book/info`）：书名、作者、译者、简介、分类、出版社、
        出版时间、ISBN、字数、评分（newRating 为百分制）等。

        回包若含 `deepLink`，展示为 `[打开阅读]({deepLink})`。
        失败时回复：暂时无法获取书籍信息，请稍后再试~
        """
        return await _call(
            ctx,
            "/book/info",
            {"bookId": book_id},
            failure_message="暂时无法获取书籍信息，请稍后再试~",
        )

    @server.tool(title="章节目录", annotations=READ_ONLY)
    async def get_book_chapters(
        ctx: Context[AppState],
        book_id: Annotated[str, Field(description="书籍 ID")],
    ) -> dict[str, Any]:
        """获取书籍章节目录（`/book/chapterinfo`）。

        `chapters[].level` 是目录层级（1=一级标题），展示时按层级缩进并标注字数与付费状态
        （`price=0` 免费、`paid=1` 已购买）。

        `chapters[].chapterUid` 是 `get_chapter_underlines` / `get_best_highlights` /
        `get_highlight_reviews` 的入参，需要按章节查划线时先用本工具取得。
        """
        return await _call(
            ctx,
            "/book/chapterinfo",
            {"bookId": book_id},
            failure_message="暂时无法获取章节目录，请稍后再试~",
        )

    @server.tool(title="阅读进度", annotations=READ_ONLY)
    async def get_reading_progress(
        ctx: Context[AppState],
        book_id: Annotated[str, Field(description="书籍 ID")],
    ) -> dict[str, Any]:
        """获取某本书的阅读进度（`/book/getprogress`）。

        关键口径：
        - `book.progress` 是 0-100 的**整数百分比**，1 表示 1%（不是 100%）；展示必须带 `%`。
          只有 `progress=100` 且存在 `book.finishTime` 才代表读完。
        - 累计阅读时长看 `book.readingTime`（秒），展示转「X小时Y分钟」。
          `book.recordReadingTime` 是朗读/记录类时长，普通阅读通常为 0，**不要**拿它当阅读时长。
        - `book.updateTime` / `book.finishTime` 是 Unix 时间戳，展示转 `YYYY-MM-DD`。

        返回中的 `_computed` 是本服务端按上述规则算好的展示值。
        """
        payload = await _call(
            ctx,
            "/book/getprogress",
            {"bookId": book_id},
            failure_message="暂时无法获取阅读进度，请稍后再试~",
        )
        book = payload.get("book")
        if isinstance(book, dict):
            payload["_computed"] = {
                "progressText": f"{book.get('progress', 0)}%",
                "finished": book.get("progress") == 100 and bool(book.get("finishTime")),
                "readingTimeText": format_duration(_reading_seconds(book)),
            }
        return payload

    # ---------------------------------------------------------------- 书架
    @server.tool(title="我的书架", annotations=READ_ONLY)
    async def get_shelf(ctx: Context[AppState]) -> dict[str, Any]:
        """获取当前用户书架（`/shelf/sync`，无参数，身份由 API Key 决定）。

        数量口径（**强制**）：
        - 书架条目总数 = `books.length + albums.length + (mp 非空 ? 1 : 0)`；
          `albums[]` 是专辑/有声书，在书架里同样按「书」管理，必须计入总数，
          不能用「另外还有」把专辑或文章收藏排除在总数之外。
        - 「电子书数」才用 `bookCount` / `books.length`。
        - 私密阅读数 = `books[].secret==1` + `albums[].albumInfoExtra.secret==1` + (`mp` 非空 ? 1 : 0)。
        - 不要遍历 `books` 逐个调 `get_book_info` 去判断有声书，直接用 `albums`。

        本服务端已按上述公式把结果算好放在 `_computed` 字段里，直接引用即可。
        失败时回复：书架信息暂时没拉到，请稍后再试~
        """
        payload = await _call(
            ctx,
            "/shelf/sync",
            failure_message="书架信息暂时没拉到，请稍后再试~",
        )
        payload["_computed"] = _shelf_counts(payload)
        return payload

    # ---------------------------------------------------------------- 笔记划线
    @server.tool(title="笔记本概览", annotations=READ_ONLY)
    async def list_notebooks(
        ctx: Context[AppState],
        count: Annotated[int, Field(description="每页数量，默认 20", ge=1, le=200)] = 20,
        last_sort: Annotated[
            int | None,
            Field(description="翻页游标：上一页 books 最后一项的 sort 值。首页不传。"),
        ] = None,
    ) -> dict[str, Any]:
        """列出所有有笔记的书（`/user/notebooks`）。

        字段口径：
        - `books[].noteCount` 是**划线（高亮原文）条数**，不是该书总笔记数。
        - 单本书总笔记数 = `reviewCount + noteCount + bookmarkCount`。
        - `reviewCount` 已包含个人点评/书评想法，不要再额外加「点评数」，否则重复计算。
        - 本接口不返回 `highlightCount`；「高亮数/划线数」对应的是 `noteCount`。

        分页只支持游标：首次只传 `count`，`hasMore=1` 时取本页最后一项的 `sort`
        作为下一次的 `last_sort`；**不支持** offset/limit。需要完整排行时循环到 `hasMore=0`。

        `_computed.noteCountByBook` 是本服务端按公式算好并降序排序的结果。
        """
        payload = await _call(
            ctx,
            "/user/notebooks",
            {"count": count, "lastSort": last_sort},
            failure_message="笔记数据暂时无法获取，请稍后再试~",
        )
        payload["_computed"] = _notebook_counts(payload)
        return payload

    @server.tool(title="我的划线", annotations=READ_ONLY)
    async def get_book_highlights(
        ctx: Context[AppState],
        book_id: Annotated[str, Field(description="书籍 ID")],
    ) -> dict[str, Any]:
        """获取我在某本书里的划线内容（`/book/bookmarklist`）。

        接口已自动过滤书签（type=0），只返回划线（type=1）；书签内容当前无法导出，
        书签**数量**见 `list_notebooks` 的 `bookmarkCount`。

        用 `chapters[].chapterUid` / `title` 把 `updated[]` 里的划线按章节分组展示，
        原文用引用格式 `>` 标注，`createTime` 转 `YYYY-MM-DD`。

        用户说「导出这本书的所有笔记」时，必须同时调用本工具和 `get_my_reviews`，
        只返回划线是不完整的。
        """
        return await _call(
            ctx,
            "/book/bookmarklist",
            {"bookId": book_id},
            failure_message="划线数据暂时无法获取，请稍后再试~",
        )

    @server.tool(title="我的想法与点评", annotations=READ_ONLY)
    async def get_my_reviews(
        ctx: Context[AppState],
        book_id: Annotated[str, Field(description="书籍 ID（网关参数名为 bookid）")],
        count: Annotated[int, Field(description="每页数量，默认 20", ge=1, le=200)] = 20,
        synckey: Annotated[int, Field(description="翻页游标，首次传 0")] = 0,
    ) -> dict[str, Any]:
        """获取我在某本书里的个人想法与点评（`/review/list/mine`），
        包含划线想法、章节点评和整本书评。

        - `reviews[].review.abstract` / `range` 是条件字段：只有能定位到原文的想法才有值；
          有值时按「原文 + 想法」展示，整本书评/章节点评可能没有。
        - `star` 为评分（0-5，-1=无评分），`chapterName` 仅章节点评有值。
        - 翻页：`hasMore=1` 时把回包的 `synckey` 传回本工具。

        与 `get_book_highlights` 配合才是完整的「单本书笔记内容」。
        """
        return await _call(
            ctx,
            "/review/list/mine",
            {"bookid": book_id, "count": count, "synckey": synckey},
            failure_message="想法/点评数据暂时无法获取，请稍后再试~",
        )

    @server.tool(title="章节划线热度", annotations=READ_ONLY)
    async def get_chapter_underlines(
        ctx: Context[AppState],
        book_id: Annotated[str, Field(description="书籍 ID")],
        chapter_uid: Annotated[int, Field(description="章节 UID，来自 get_book_chapters")],
        synckey: Annotated[int, Field(description="增量同步 key，默认 0")] = 0,
    ) -> dict[str, Any]:
        """获取某章节内每条划线的热度统计（`/book/underlines`）。

        只有人数/得分/位置范围，**不含划线文本**，用于展示「X人划线」标签。
        需要划线原文请用 `get_best_highlights`。
        """
        return await _call(
            ctx,
            "/book/underlines",
            {"bookId": book_id, "chapterUid": chapter_uid, "synckey": synckey},
            failure_message="划线热度数据暂时无法获取，请稍后再试~",
        )

    @server.tool(title="热门划线", annotations=READ_ONLY)
    async def get_best_highlights(
        ctx: Context[AppState],
        book_id: Annotated[str, Field(description="书籍 ID")],
        chapter_uid: Annotated[
            int, Field(description="章节 UID，0 表示全书（默认）；按章节查时来自 get_book_chapters")
        ] = 0,
        synckey: Annotated[int, Field(description="增量同步 key，默认 0")] = 0,
    ) -> dict[str, Any]:
        """获取书籍/章节的热门划线（`/book/bestbookmarks`），含划线原文 `markText`
        和划线人数 `items[].totalCount`，按热度排序。

        服务端固定返回前 20 条，不支持分页。
        `items[].range` 可以传给 `get_highlight_reviews`，查看该条划线下面的想法。
        """
        return await _call(
            ctx,
            "/book/bestbookmarks",
            {"bookId": book_id, "chapterUid": chapter_uid, "synckey": synckey},
            failure_message="热门划线数据暂时无法获取，请稍后再试~",
        )

    @server.tool(title="划线下的想法", annotations=READ_ONLY)
    async def get_highlight_reviews(
        ctx: Context[AppState],
        book_id: Annotated[str, Field(description="书籍 ID")],
        chapter_uid: Annotated[int, Field(description="章节 UID")],
        ranges: Annotated[
            list[str],
            Field(
                description=(
                    "要查询的划线位置范围列表，如 [\"393-401\"]，"
                    "取自 get_best_highlights 返回的 items[].range"
                ),
                min_length=1,
            ),
        ],
        count: Annotated[
            int, Field(description="每个 range 拉取的想法数量，服务端上限 20", ge=1, le=20)
        ] = 20,
        max_idx: Annotated[int, Field(description="翻页偏移，默认 0")] = 0,
        synckey: Annotated[int, Field(description="翻页游标，默认 0")] = 0,
    ) -> dict[str, Any]:
        """获取指定划线范围下面的想法/评论（`/book/readreviews`）。

        本工具会把 `ranges` 组装成网关要求的 `reviews` 数组
        （这是少数允许作为数组传入的业务字段）。

        回包 `reviews[].pageReviews[].review` 里有 `abstract`（划线原文）、
        `content`（想法内容）、`author`、`createTime`。
        需要单条想法的完整详情（含评论/点赞）时再调 `get_review_detail`。
        """
        payload_reviews = [
            {"range": item, "count": count, "maxIdx": max_idx, "synckey": synckey}
            for item in ranges
        ]
        return await _call(
            ctx,
            "/book/readreviews",
            {"bookId": book_id, "chapterUid": chapter_uid, "reviews": payload_reviews},
            failure_message="划线想法数据暂时无法获取，请稍后再试~",
        )

    @server.tool(title="想法详情", annotations=READ_ONLY)
    async def get_review_detail(
        ctx: Context[AppState],
        review_id: Annotated[str, Field(description="想法/评论 ID")],
        comments_count: Annotated[int, Field(description="拉取评论数量，默认 10", ge=0)] = 10,
        comments_direction: Annotated[
            Literal[0, 1], Field(description="评论排序方向：0=倒序，1=正序")
        ] = 0,
        likes_count: Annotated[int, Field(description="拉取点赞数量，默认 10", ge=0)] = 10,
        likes_direction: Annotated[Literal[0, 1], Field(description="点赞排序方向：0=倒序")] = 0,
        synckey: Annotated[int, Field(description="增量同步 key，默认 0")] = 0,
    ) -> dict[str, Any]:
        """获取单条想法的完整详情（`/review/single`），含内容、作者、评论与点赞。"""
        return await _call(
            ctx,
            "/review/single",
            {
                "reviewId": review_id,
                "commentsCount": comments_count,
                "commentsDirection": comments_direction,
                "likesCount": likes_count,
                "likesDirection": likes_direction,
                "synckey": synckey,
            },
            failure_message="想法详情暂时无法获取，请稍后再试~",
        )

    # ---------------------------------------------------------------- 阅读统计
    @server.tool(title="阅读统计", annotations=READ_ONLY)
    async def get_read_stats(
        ctx: Context[AppState],
        mode: Annotated[
            Literal["weekly", "monthly", "annually", "overall"],
            Field(description="统计维度：weekly=本周，monthly=本月（默认），annually=本年，overall=总计"),
        ] = "monthly",
        base_time: Annotated[
            int,
            Field(
                description=(
                    "基准时间戳，0=当前周期。服务端会归一化到周期起点（周一/月初/年初）；"
                    "查历史就传该周期内任一 Unix 时间戳；overall 固定为 0。"
                )
            ),
        ] = 0,
    ) -> dict[str, Any]:
        """获取个人阅读统计（`/readdata/detail`）：时长、天数、排行、偏好分析。

        **单位与口径（极易出错，必须遵守）**：
        - `totalReadTime` 是该周期总阅读/收听时长，单位**秒**，禁止当成分钟或小时；
          统计总时长优先用它，`readTimes` 只用于明细或交叉校验。
        - `dayAverageReadTime` 是按**自然日**平均的秒数，分母不是 `readDays`；
          需要「阅读日均」要自己用 `totalReadTime / readDays` 算并说明。
        - `readDays` 是有效阅读天数（单日满 1 分钟）。
        - `compare` 是与上一周期日均的比例，0.2 表示约增长 20%。
        - `preferAuthor[].readTime` 是格式化字符串（如「5小时30分钟」），不是秒。
        - `preferTime` 是 24 小时时段分布（秒），顺序从 6 点开始到次日 5 点，不是从 0 点。

        本接口只支持固定自然周期，不能传任意起止日期。跨区间要组合：
        整年用 `annually` 逐年查询并累加，整月用 `monthly`；边界不完整时优先用
        `dailyReadTimes` 做日级扣减，没有日级明细就用月级近似并在回答中说明口径。

        `_computed` 里给出了换算好的「X小时Y分钟」文案。
        失败时回复：阅读数据暂时无法获取，请稍后再试~
        """
        payload = await _call(
            ctx,
            "/readdata/detail",
            {"mode": mode, "baseTime": base_time},
            failure_message="阅读数据暂时无法获取，请稍后再试~",
        )
        computed: dict[str, Any] = {
            "mode": mode,
            "totalReadTimeText": format_duration(payload.get("totalReadTime")),
            "dayAverageReadTimeText": format_duration(payload.get("dayAverageReadTime")),
        }
        read_days = payload.get("readDays")
        total = payload.get("totalReadTime")
        if isinstance(read_days, int) and read_days > 0 and isinstance(total, int):
            computed["perReadDayText"] = format_duration(total // read_days)
            computed["perReadDayNote"] = "阅读日均 = totalReadTime / readDays（接口不直接返回）"
        payload["_computed"] = computed
        return payload

    # ---------------------------------------------------------------- 书籍点评
    @server.tool(title="书籍公开点评", annotations=READ_ONLY)
    async def list_book_reviews(
        ctx: Context[AppState],
        book_id: Annotated[str, Field(description="书籍 ID")],
        review_list_type: Annotated[
            Literal[0, 1, 2, 3, 4],
            Field(description="筛选类型：0=全部（默认），1=推荐，2=不行/差评，3=最新，4=一般"),
        ] = 0,
        count: Annotated[int, Field(description="每页数量，默认 20", ge=1, le=100)] = 20,
        max_idx: Annotated[int, Field(description="翻页偏移：上一页最后一条的 idx")] = 0,
        synckey: Annotated[int, Field(description="翻页游标，首次 0")] = 0,
    ) -> dict[str, Any]:
        """获取书籍的**公开点评**（`/review/list`）——其他读者的评价，
        不是个人笔记（个人笔记用 `get_my_reviews`）。

        结构注意：存在双层嵌套，点评内容在 `reviews[].review.review.content`、
        `...star`、`...author.name`、`...book.title`。

        评分换算：20=⭐，40=⭐⭐，60=⭐⭐⭐，80=⭐⭐⭐⭐，100=⭐⭐⭐⭐⭐。
        长点评截取前 200 字并提示可展开；`createTime` 转 `YYYY-MM-DD`。
        无结果回复：这本书暂时还没有公开点评哦~
        """
        return await _call(
            ctx,
            "/review/list",
            {
                "bookId": book_id,
                "reviewListType": review_list_type,
                "count": count,
                "maxIdx": max_idx,
                "synckey": synckey,
            },
            failure_message="点评数据获取失败，请稍后再试~",
        )

    # ---------------------------------------------------------------- 推荐发现
    @server.tool(title="为你推荐", annotations=READ_ONLY)
    async def recommend_books(
        ctx: Context[AppState],
        count: Annotated[int, Field(description="每页数量，默认 12", ge=1, le=50)] = 12,
        max_idx: Annotated[int, Field(description="翻页偏移：上一页最后一条的 searchIdx")] = 0,
    ) -> dict[str, Any]:
        """基于个人阅读记录的个性化推荐（`/book/recommend`），
        与 App 首页「为你推荐」一致。

        每本书含 `reason`（推荐理由）、`newRating`（0-100）、`readingCount`（在读人数）。
        翻页用最后一条的 `searchIdx` 作为下次的 `max_idx`。
        无结果回复：暂时没有找到合适的推荐，换个关键词试试？
        """
        return await _call(
            ctx,
            "/book/recommend",
            {"count": count, "maxIdx": max_idx},
            failure_message="推荐数据获取失败，请稍后再试~",
        )

    @server.tool(title="相似书推荐", annotations=READ_ONLY)
    async def similar_books(
        ctx: Context[AppState],
        book_id: Annotated[str, Field(description="书籍 ID")],
        count: Annotated[
            int, Field(description="每页数量，必须显式传，首次推荐传 12", ge=1, le=50)
        ] = 12,
        max_idx: Annotated[
            int, Field(description="翻页偏移，必须显式传，首次 0；翻页传上一页最后一条的 idx")
        ] = 0,
        session_id: Annotated[
            str | None, Field(description="翻页会话 ID：首次不传，之后传回包里的 sessionId")
        ] = None,
    ) -> dict[str, Any]:
        """基于某本书的相似推荐（`/book/similar`），与 App 书籍详情页「相似推荐」一致。

        底层转发到 `/book/detailinfo?listtypes=2`，要求 count / maxIdx 必须显式传入，
        不能依赖默认值，否则结果异常。

        结果在 `booksimilar.books[].book.bookInfo`，翻页用最后一条的 `idx` 作为 `max_idx`，
        并带上 `booksimilar.sessionId`。
        """
        return await _call(
            ctx,
            "/book/similar",
            {"bookId": book_id, "count": count, "maxIdx": max_idx, "sessionId": session_id},
            failure_message="推荐数据获取失败，请稍后再试~",
        )

    # ---------------------------------------------------------------- 组合能力
    @server.tool(title="阅读概况", annotations=READ_ONLY)
    async def get_reading_overview(
        ctx: Context[AppState],
        recent_book_limit: Annotated[
            int,
            Field(description="查询阅读进度的最近书籍数量，默认 5（避免大书架产生过多调用）", ge=0, le=10),
        ] = 5,
    ) -> dict[str, Any]:
        """一次性汇总用户阅读概况（对应官方 `profile.md` 的工作流）：
        书架总览 + 最近阅读的前 N 本电子书进度 + 笔记概览。

        内部依次调用 `/shelf/sync`、按 `readUpdateTime` 降序取前 N 本调 `/book/getprogress`、
        再调 `/user/notebooks`。只对 `books[]` 里的电子书查进度，不对 `albums[]` 专辑调用。

        书架为空时回复：你的书架还没有书哦，要不要去发现页看看推荐？
        失败时回复：阅读概况暂时无法获取，请稍后再试~
        """
        shelf = await _call(
            ctx,
            "/shelf/sync",
            failure_message="阅读概况暂时无法获取，请稍后再试~",
        )
        counts = _shelf_counts(shelf)

        books = [b for b in _as_list(shelf.get("books")) if isinstance(b, dict)]
        books.sort(key=lambda b: b.get("readUpdateTime") or 0, reverse=True)
        recent = books[:recent_book_limit] if recent_book_limit else []

        recent_progress: list[dict[str, Any]] = []
        for book in recent:
            book_id = book.get("bookId")
            if not book_id:
                continue
            try:
                progress = await _call(ctx, "/book/getprogress", {"bookId": book_id})
            except ToolError as exc:
                recent_progress.append({"bookId": book_id, "title": book.get("title"), "error": str(exc)})
                continue
            detail = progress.get("book") if isinstance(progress.get("book"), dict) else {}
            recent_progress.append(
                {
                    "bookId": book_id,
                    "title": book.get("title"),
                    "author": book.get("author"),
                    "deepLink": book.get("deepLink"),
                    "readUpdateTime": book.get("readUpdateTime"),
                    "progress": detail.get("progress"),
                    "progressText": f"{detail.get('progress', 0)}%",
                    "finished": detail.get("progress") == 100 and bool(detail.get("finishTime")),
                    "finishTime": detail.get("finishTime"),
                    "readingTime": _reading_seconds(detail),
                    "readingTimeText": format_duration(_reading_seconds(detail)),
                }
            )

        notebooks = await _call(
            ctx,
            "/user/notebooks",
            {"count": 100},
            failure_message="阅读概况暂时无法获取，请稍后再试~",
        )

        return {
            "shelf": {
                "counts": counts,
                "archive": shelf.get("archive"),
                "note": "概况只含数量；需要完整书架列表请调用 get_shelf",
            },
            "recentReading": recent_progress,
            "notes": {
                "totalBookCount": notebooks.get("totalBookCount"),
                "totalNoteCount": notebooks.get("totalNoteCount"),
                "hasMore": notebooks.get("hasMore"),
                "noteCountByBook": _notebook_counts(notebooks)["noteCountByBook"],
            },
        }

    # ---------------------------------------------------------------- 网关元能力
    @server.tool(title="网关接口清单", annotations=READ_ONLY)
    async def list_gateway_apis(ctx: Context[AppState]) -> dict[str, Any]:
        """列出微信读书网关当前可用的全部接口及参数定义（`{"api_name": "/_list"}`）。

        用于排查「某个能力是否还存在 / 参数是否变化」，日常问答不需要调用。
        """
        return await _call(
            ctx,
            "/_list",
            failure_message="接口清单暂时无法获取，请稍后再试~",
        )

    @server.tool(title="调用任意网关接口", annotations=READ_ONLY)
    async def call_gateway(
        ctx: Context[AppState],
        api_name: Annotated[str, Field(description="接口名，如 /store/search，可用 list_gateway_apis 查询")],
        params: Annotated[
            dict[str, Any] | None,
            Field(description="业务参数，会被平铺到请求体顶层（不要再嵌套 params/data/body）"),
        ] = None,
    ) -> dict[str, Any]:
        """直连网关的兜底工具：当某个接口还没有对应的专用工具时使用。

        本工具会自动补上 `skill_version` 与鉴权头，并把 `params` 平铺到 body 顶层。
        能用专用工具时优先用专用工具——它们的说明里带有字段口径与避坑规则。
        """
        return await _call(ctx, api_name, params or {})
