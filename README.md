# weread-mcp

把腾讯官方的 [WeChatReading Skills](https://github.com/Tencent/WeChatReading) 转成一个
**基于 URL（SSE）的 MCP Server**，用 [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) v2 实现。

官方 skills 是给「能读 Markdown + 会自己发 HTTP 请求」的 Agent 用的；转成 MCP Server 之后，
任何 MCP 客户端（Claude Desktop / Claude Code / Cherry Studio / 自建 Agent…）都可以直接连一个 URL 使用，
接口参数由服务端校验，字段口径写进了工具说明里。

```
MCP Client ──SSE──▶ weread-mcp ──HTTPS──▶ https://i.weread.qq.com/api/agent/gateway
```

## 能力一览

19 个工具，覆盖官方全部 7 类能力：

| 工具 | 网关接口 | 说明 |
|------|----------|------|
| `search_books` | `/store/search` | 书城搜索，`scope` 区分电子书/网文/听书/作者/全文/书单/公众号/文章 |
| `get_book_info` | `/book/info` | 书籍详情 |
| `get_book_chapters` | `/book/chapterinfo` | 章节目录（拿 `chapterUid`） |
| `get_reading_progress` | `/book/getprogress` | 阅读进度与累计时长 |
| `get_shelf` | `/shelf/sync` | 书架（电子书 + 专辑/有声书 + 文章收藏） |
| `list_notebooks` | `/user/notebooks` | 笔记本概览，游标分页 |
| `get_book_highlights` | `/book/bookmarklist` | 我的划线内容 |
| `get_my_reviews` | `/review/list/mine` | 我的想法与点评 |
| `get_chapter_underlines` | `/book/underlines` | 章节划线热度（无文本） |
| `get_best_highlights` | `/book/bestbookmarks` | 热门划线（含原文与人数） |
| `get_highlight_reviews` | `/book/readreviews` | 某条划线下面的想法 |
| `get_review_detail` | `/review/single` | 单条想法详情 |
| `get_read_stats` | `/readdata/detail` | 阅读时长/天数/排行/偏好 |
| `list_book_reviews` | `/review/list` | 书籍公开点评 |
| `recommend_books` | `/book/recommend` | 为你推荐 |
| `similar_books` | `/book/similar` | 相似书推荐 |
| `get_reading_overview` | 组合调用 | 书架 + 最近 5 本进度 + 笔记概览（对应官方 `profile.md` 工作流） |
| `list_gateway_apis` | `/_list` | 网关接口清单 |
| `call_gateway` | 任意 | 兜底：直调某个还没有专用工具的接口 |

另外把官方能力文档原样作为 **MCP 资源** 暴露，模型需要核对字段口径时可以直接读：

```
weread://skills            # 总纲（SKILL.md）
weread://skills/search     weread://skills/book      weread://skills/shelf
weread://skills/notes      weread://skills/readdata  weread://skills/review
weread://skills/discover   weread://skills/profile
```

### 不只是接口透传

官方文档里那些「一读就懂、一写就错」的口径，这里都固化进了服务端：

- **书架数量**按 `books.length + albums.length + (mp 非空 ? 1 : 0)` 算好放在 `_computed` 里，
  专辑/有声书不会被漏掉；公开/私密数量同样遍历实际条目计算。
- **笔记数量**按 `reviewCount + noteCount + bookmarkCount` 算好并降序排序，
  不会把 `noteCount`（划线条数）当成总笔记数。
- **阅读时长**单位是秒，服务端直接给出「X小时Y分钟」文案，并区分「自然日均」与「阅读日均」。
- **阅读进度** `progress` 是 0-100 整数，服务端给出 `progressText`（带 `%`）和 `finished` 判定。
- **参数平铺**：业务参数一律平铺到 body 顶层，绝不会包进 `params`（官方文档里分页失效的头号原因）。
- `skill_version` 每次自动上报；网关返回 `upgrade_info` 时会告警并原样透传给模型。

## 与官方 weread-skills 怎么选

两者调用的是同一个微信读书网关，数据完全一样。区别在于**谁来发请求、谁来保证口径正确**：
官方 skills 是给 Agent 读的说明书，由模型自己读文档、拼 `curl`、解析 JSON；
本项目把这些工作写进了代码，模型只需要选工具、填参数。

| | 官方 [weread-skills](https://github.com/Tencent/WeChatReading) | weread-mcp（本项目） |
|---|---|---|
| **形态** | 9 份 Markdown 文档 | Python MCP Server |
| **一次提问的流程** | 读 SKILL.md → 读子文档 → shell 里拼 `curl` → 自己解析、计数 | 调一个工具，参数由代码校验，计数由代码算好 |
| **模型往返** | 通常 3～4 轮 | 通常 1 轮 |
| **运行前提** | 客户端能执行 shell，且网络能访问 `i.weread.qq.com` | 任意 MCP 客户端，不需要 shell |
| **后台常驻** | 无 | stdio 模式下 1 个 Python 进程（空闲约几十 MB 内存，不主动发请求） |
| **磁盘占用** | 约 70 KB | 源码约 200 KB + 依赖约 45 MB |
| **上下文开销** | 平时几乎为 0；用到时读文档，每次约 3～8 千 token | 19 个工具定义约 1 万 token 量级；客户端一次性加载时每次对话都占，支持按需加载的客户端则小得多 |
| **口径正确性** | 依赖模型遵守文档里的「必须 / 禁止」规则 | 参数平铺、版本上报、书架与笔记计数、时长换算都在代码里强制执行 |
| **API Key** | 放在 agent 可执行命令的 shell 环境变量中 | 只存在于服务端进程，模型接触不到 |
| **跨客户端 / 共享** | 需要支持 Agent Skills 的客户端 | 任意 MCP 客户端；SSE / Streamable HTTP 模式下可通过 URL 多人共用 |
| **维护** | 腾讯官方维护，网关通过 `upgrade_info` 提示升级 | 社区维护，官方接口变化需要跟进更新 |

**一个实测例子**：官方 `book.md` 把 `/book/getprogress` 的 `recordReadingTime` 描述为累计阅读时长，
但真实回包里该字段通常为 0，累计时长实际在 `readingTime`。严格照文档执行的模型会回答「读了 0 分钟」；
本项目在代码里做了修正（见 `tools.py` 中的 `_reading_seconds`）。

**什么时候选哪个**

- **选 weread-mcp**：经常使用；在意回答的准确性和响应速度；想在 Claude Desktop、Cursor、Cherry Studio 等多个客户端里用；
  或者需要部署成一个 URL 给多人共用。
- **选官方 skills**：偶尔用一下；不想常驻进程、不想装 Python 依赖；已经在用 Claude Code 等能执行 shell 的 Agent；
  希望由官方负责维护与升级。
- 两者可以同时安装，不会冲突，但模型可能不确定该用哪个，**建议只启用其中一个**。
  不常用时也可以在客户端里临时关闭本 MCP，省下固定的上下文开销。

## 快速开始

### 1. 获取 API Key

前往 <https://weread.qq.com/r/weread-skills> 获取 API Key（格式 `wrk-xxxxxxxx`）。
Key 绑定用户身份（vid），所有需要身份的接口会自动注入，无需传 vid。

### 2. 安装依赖

```bash
uv sync
cp .env.example .env    # 把 WEREAD_API_KEY 填进去
```

### 3. 启动（SSE）

```bash
uv run weread-mcp                          # 默认 127.0.0.1:8000
uv run weread-mcp --host 0.0.0.0 --port 8000
```

启动后：

```
MCP endpoint : http://127.0.0.1:8000/sse
health check : http://127.0.0.1:8000/healthz
```

也可以用 `uv run python -m weread_mcp` 或 `uv run main.py`。

### 4. 在 MCP 客户端里配置

支持 URL 的客户端（Claude Desktop / Claude Code 等）：

```json
{
  "mcpServers": {
    "weread": {
      "type": "sse",
      "url": "http://127.0.0.1:8000/sse"
    }
  }
}
```

Claude Code 一行搞定：

```bash
claude mcp add --transport sse weread http://127.0.0.1:8000/sse
```

只支持 stdio 的老客户端：

```json
{
  "mcpServers": {
    "weread": {
      "command": "uv",
      "args": ["--directory", "/path/to/weread-mcp", "run", "weread-mcp", "--transport", "stdio"],
      "env": { "WEREAD_API_KEY": "wrk-xxxxxxxx" }
    }
  }
}
```

## 传输方式

| 传输 | 启动参数 | 端点 | 说明 |
|------|----------|------|------|
| SSE | `--transport sse`（默认） | `GET /sse` + `POST /messages/` | 题目要求的 URL 方式，兼容性最好 |
| Streamable HTTP | `--transport streamable-http` | `/mcp` | MCP 新版标准传输，可配 `--json-response` / `--stateless` |
| stdio | `--transport stdio` | — | 本地子进程方式 |

> SSE 是 MCP 规范中的 legacy 传输，新客户端建议用 Streamable HTTP；两者本服务都支持，切换只改一个参数。

监听 `127.0.0.1` 时 SDK 会自动开启 DNS rebinding 防护；对外暴露（`--host 0.0.0.0`）时请自行放在
反向代理/鉴权网关后面，并用 `/healthz` 做健康检查。

## 多用户部署：Key 由客户端携带

服务端不配 `WEREAD_API_KEY` 时，每个客户端用请求头带自己的 Key：

```
Authorization: Bearer wrk-xxxxxxxx
# 或
X-WeRead-Api-Key: wrk-xxxxxxxx
```

请求头优先级高于服务端环境变量。不想让客户端覆盖时设 `WEREAD_ALLOW_CLIENT_API_KEY=false`。

## 配置项

全部可用环境变量或命令行参数设置，命令行优先，详见 `.env.example` 与 `weread-mcp --help`。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `WEREAD_API_KEY` | — | 服务端默认 API Key |
| `WEREAD_MCP_TRANSPORT` | `sse` | `sse` / `streamable-http` / `stdio` |
| `WEREAD_MCP_HOST` / `WEREAD_MCP_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `WEREAD_MCP_SSE_PATH` / `WEREAD_MCP_MESSAGE_PATH` | `/sse` / `/messages/` | SSE 路径 |
| `WEREAD_MCP_HTTP_PATH` | `/mcp` | Streamable HTTP 路径 |
| `WEREAD_ALLOW_CLIENT_API_KEY` | `true` | 是否允许请求头覆盖 Key |
| `WEREAD_SKILL_VERSION` | `1.0.4` | 上报给网关的 skill 版本 |
| `WEREAD_REQUEST_TIMEOUT` | `30` | 网关请求超时（秒） |
| `WEREAD_MCP_LOG_LEVEL` | `INFO` | 日志级别 |

## 项目结构

```
src/weread_mcp/
├── cli.py        # 命令行入口与传输选择
├── config.py     # 环境变量 / 参数配置
├── gateway.py    # Agent API Gateway 客户端（鉴权、参数平铺、错误归一化）
├── server.py     # MCPServer 构造、lifespan、instructions、/healthz
├── tools.py      # 19 个工具，docstring 即模型看到的口径说明
├── resources.py  # weread://skills/... 资源
└── skills/       # 官方能力文档（Apache-2.0，原样内置，见 NOTICE）
tests/
├── test_gateway.py  # 参数平铺、errcode、Key 处理
├── test_tools.py    # 内存 MCP 客户端跑通全部工具与口径计算
├── test_sse.py      # 真起 uvicorn，用 MCP 客户端通过 URL 连接
└── test_cli.py      # 参数覆盖
```

## 测试

```bash
uv run pytest
```

`test_sse.py` 会在随机端口起一个真实的 HTTP 服务，用 MCP 客户端走 SSE 连上来调用工具，
网关请求被 mock，不会真的打到微信读书。

## 说明

- 升级官方 skills 时：替换 `src/weread_mcp/skills/*.md`，并把 `WEREAD_SKILL_VERSION`
  改成新的 version（或直接改 `config.py` 里的 `DEFAULT_SKILL_VERSION`）；
  如果网关新增了接口，用 `list_gateway_apis` 查看后在 `tools.py` 里补一个工具即可。
- 本项目与腾讯官方无从属关系；内置文档版权与许可见 `NOTICE` 与 `LICENSE`。

许可证：Apache-2.0
