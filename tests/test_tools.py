"""通过内存 MCP 客户端验证工具注册、参数映射与口径计算。"""

from __future__ import annotations

import pytest
from mcp.client import Client
from mcp.client._memory import InMemoryTransport

from weread_mcp.config import Settings
from weread_mcp.resources import SKILL_DOCS

pytestmark = pytest.mark.anyio


EXPECTED_TOOLS = {
    "search_books",
    "get_book_info",
    "get_book_chapters",
    "get_reading_progress",
    "get_shelf",
    "list_notebooks",
    "get_book_highlights",
    "get_my_reviews",
    "get_chapter_underlines",
    "get_best_highlights",
    "get_highlight_reviews",
    "get_review_detail",
    "get_read_stats",
    "list_book_reviews",
    "recommend_books",
    "similar_books",
    "get_reading_overview",
    "list_gateway_apis",
    "call_gateway",
}


async def test_all_tools_are_registered(make_server):
    server = make_server()
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools.tools}
    assert EXPECTED_TOOLS <= names


async def test_tools_carry_titles_and_descriptions(make_server):
    server = make_server()
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        tools = await client.list_tools()
    for tool in tools.tools:
        assert tool.description, f"{tool.name} 缺少说明"
        assert tool.annotations is not None and tool.annotations.read_only_hint is True


async def test_search_books_flattens_parameters(make_server, gateway_recorder):
    server = make_server({"/store/search": {"errcode": 0, "hasMore": 0, "results": []}})
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        result = await client.call_tool("search_books", {"keyword": "三体", "scope": 0, "count": 5})

    assert result.is_error is False
    body = gateway_recorder[-1]["body"]
    assert body == {
        "api_name": "/store/search",
        "skill_version": "1.0.4",
        "keyword": "三体",
        "scope": 0,
        "count": 5,
    }
    assert gateway_recorder[-1]["authorization"] == "Bearer wrk-test-key"


async def test_optional_params_are_omitted(make_server, gateway_recorder):
    server = make_server({"/store/search": {"errcode": 0}})
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        await client.call_tool("search_books", {"keyword": "三体"})

    body = gateway_recorder[-1]["body"]
    assert "count" not in body and "maxIdx" not in body
    assert body["scope"] == 10


async def test_shelf_counts_follow_official_formula(make_server):
    payload = {
        "errcode": 0,
        "books": [
            {"bookId": "b1", "title": "书一", "secret": 0},
            {"bookId": "b2", "title": "书二", "secret": 1},
        ],
        "albums": [
            {"albumInfo": {"albumId": "a1", "name": "专辑一"}, "albumInfoExtra": {"secret": 0}},
        ],
        "mp": {"title": "文章收藏"},
        "bookCount": 2,
    }
    server = make_server({"/shelf/sync": payload})
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        result = await client.call_tool("get_shelf", {})

    computed = result.structured_content["_computed"]
    # books(2) + albums(1) + mp(1)
    assert computed["shelfItemTotal"] == 4
    assert computed["ebookCount"] == 2
    assert computed["albumCount"] == 1
    assert computed["articleCollectionCount"] == 1
    # secret: 书二 + mp
    assert computed["secretCount"] == 2
    assert computed["publicCount"] == 2


async def test_notebook_total_note_count(make_server):
    payload = {
        "errcode": 0,
        "totalBookCount": 2,
        "books": [
            {
                "bookId": "b1",
                "book": {"title": "少的那本"},
                "reviewCount": 1,
                "noteCount": 2,
                "bookmarkCount": 0,
                "sort": 100,
            },
            {
                "bookId": "b2",
                "book": {"title": "多的那本"},
                "reviewCount": 5,
                "noteCount": 10,
                "bookmarkCount": 3,
                "sort": 200,
            },
        ],
    }
    server = make_server({"/user/notebooks": payload})
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        result = await client.call_tool("list_notebooks", {"count": 20})

    rows = result.structured_content["_computed"]["noteCountByBook"]
    assert [row["bookId"] for row in rows] == ["b2", "b1"]
    assert rows[0]["totalNoteCount"] == 18
    assert rows[1]["totalNoteCount"] == 3


async def test_read_stats_converts_seconds(make_server):
    payload = {
        "errcode": 0,
        "totalReadTime": 7320,  # 2小时2分钟
        "dayAverageReadTime": 1800,
        "readDays": 4,
    }
    server = make_server({"/readdata/detail": payload})
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        result = await client.call_tool("get_read_stats", {"mode": "weekly"})

    computed = result.structured_content["_computed"]
    assert computed["totalReadTimeText"] == "2小时2分钟"
    assert computed["dayAverageReadTimeText"] == "30分钟"
    assert computed["perReadDayText"] == "30分钟"


async def test_progress_percent_is_not_misread(make_server):
    payload = {"errcode": 0, "book": {"progress": 1, "recordReadingTime": 3600}}
    server = make_server({"/book/getprogress": payload})
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        result = await client.call_tool("get_reading_progress", {"book_id": "b1"})

    computed = result.structured_content["_computed"]
    assert computed["progressText"] == "1%"
    assert computed["finished"] is False
    assert computed["readingTimeText"] == "1小时"


async def test_reading_time_prefers_readingtime_field(make_server):
    """真实回包：累计时长在 readingTime，recordReadingTime 为 0。"""
    payload = {
        "errcode": 0,
        "book": {"progress": 39, "readingTime": 28404, "recordReadingTime": 0},
    }
    server = make_server({"/book/getprogress": payload})
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        result = await client.call_tool("get_reading_progress", {"book_id": "44026191"})

    assert result.structured_content["_computed"]["readingTimeText"] == "7小时53分钟"


async def test_highlight_reviews_builds_reviews_array(make_server, gateway_recorder):
    server = make_server({"/book/readreviews": {"errcode": 0, "reviews": []}})
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        await client.call_tool(
            "get_highlight_reviews",
            {"book_id": "b1", "chapter_uid": 7, "ranges": ["393-401", "500-520"], "count": 10},
        )

    body = gateway_recorder[-1]["body"]
    assert body["bookId"] == "b1" and body["chapterUid"] == 7
    assert body["reviews"] == [
        {"range": "393-401", "count": 10, "maxIdx": 0, "synckey": 0},
        {"range": "500-520", "count": 10, "maxIdx": 0, "synckey": 0},
    ]


async def test_reading_overview_combines_three_apis(make_server, gateway_recorder):
    server = make_server(
        {
            "/shelf/sync": {
                "errcode": 0,
                "books": [
                    {"bookId": "old", "title": "旧书", "readUpdateTime": 1, "secret": 0},
                    {"bookId": "new", "title": "新书", "readUpdateTime": 999, "secret": 0},
                ],
                "albums": [],
            },
            "/book/getprogress": {
                "errcode": 0,
                "book": {"progress": 100, "finishTime": 1748563200, "recordReadingTime": 5400},
            },
            "/user/notebooks": {
                "errcode": 0,
                "totalBookCount": 1,
                "totalNoteCount": 9,
                "books": [
                    {
                        "bookId": "new",
                        "book": {"title": "新书"},
                        "reviewCount": 2,
                        "noteCount": 6,
                        "bookmarkCount": 1,
                    }
                ],
            },
        }
    )
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        result = await client.call_tool("get_reading_overview", {"recent_book_limit": 1})

    data = result.structured_content
    assert data["shelf"]["counts"]["shelfItemTotal"] == 2
    # 只查了最近阅读的一本，且是 readUpdateTime 最大的那本
    assert [row["bookId"] for row in data["recentReading"]] == ["new"]
    assert data["recentReading"][0]["finished"] is True
    assert data["recentReading"][0]["readingTimeText"] == "1小时30分钟"
    # 概况不应把整个书架列表倒出来
    assert "books" not in data["shelf"]
    assert data["notes"]["noteCountByBook"][0]["totalNoteCount"] == 9

    called = [entry["body"]["api_name"] for entry in gateway_recorder]
    assert called == ["/shelf/sync", "/book/getprogress", "/user/notebooks"]


async def test_gateway_business_error_becomes_tool_error(make_server):
    server = make_server({"/shelf/sync": {"errcode": -2012, "errmsg": "登录态失效"}})
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        result = await client.call_tool("get_shelf", {})

    assert result.is_error is True
    text = result.content[0].text
    assert "书架信息暂时没拉到" in text and "-2012" in text


async def test_missing_api_key_message(make_server):
    server = make_server(
        {"/shelf/sync": {"errcode": 0}},
        custom=Settings(api_key=None, log_level="WARNING"),
    )
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        result = await client.call_tool("get_shelf", {})

    assert result.is_error is True
    assert "WEREAD_API_KEY" in result.content[0].text


async def test_skill_docs_exposed_as_resources(make_server):
    server = make_server()
    async with Client(InMemoryTransport(server), mode="legacy") as client:
        listing = await client.list_resources()
        uris = {str(resource.uri) for resource in listing.resources}
        assert "weread://skills" in uris
        assert len(uris) >= len(SKILL_DOCS)

        content = await client.read_resource("weread://skills/shelf")

    text = content.contents[0].text
    assert "books.length + albums.length" in text
