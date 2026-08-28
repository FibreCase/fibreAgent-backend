# Architecture

这个 backend 不是「一个 Telegram chatbot」，而是从第一版就分层：Telegram 只是一层薄薄的适配器，真正的核心是**渠道无关**的 `AgentService`，工具循环插在核心和 LLM 客户端之间。任何未来的输入渠道（Web UI / Discord / HTTP API）都复用同一个核心，**不改 Telegram 层**。

```
Telegram Adapter   →   Agent Service   →   Tool Loop   →   LLM Client   →   Persistent Conversation
（适配器）            （渠道无关的核心）    （工具循环）    （OpenAI 兼容）   （SQLite）
                                  ↑
                          Tool Registry（get_current_time / echo / system_info）
```

## 分层图

```
                        ┌─────────────────────────────────────────┐
   Telegram  ──poll──▶  │  telegram/bot.py  (Adapter)             │
   (long polling)       │  · 鉴权 (allow-list)                    │
                        │  · /start /new /context /help /status   │
                        │  · /remember /memories /forget /tool_audit│
                        │  · /mcp_status                            │
                        │  · typing 保活                          │
                        │  · Markdown→HTML 渲染 + 分块发送          │
                        └───────────────┬─────────────────────────┘
                                        │  只调用 AgentService
                                        ▼
                        ┌─────────────────────────────────────────┐
                        │  agent/service.py  (AgentService)       │
                        │  · per-conversation asyncio.Lock        │
                        │  · 组装 context (system + 记忆 + 历史)     │
                        │  · 驱动 tool loop / 单次 LLM 调用         │
                        │  · 保存 user / assistant 消息 + 图片 blob  │
                        └───────────────┬─────────────┬───────────┘
                                        │             │
                              ┌────────────────┐   ┌────────────────────────┐
                              │ llm/client.py  │   │ database/repository.py │
                              │ (OpenAIClient) │   │ SQLAlchemy 2.x async   │
                              │ OpenAI SDK     │   │ aiosqlite / SQLite     │
                              └────────────────┘   └────────────────────────┘
```

## 关键分层约束

- **Telegram 层从不直接调用 OpenAI SDK**，只调用 `AgentService.process_message()`。
- **Agent Service 渠道无关**：未来接 Web UI / Discord / HTTP API 都复用同一个 `AgentService`。
- **只有 `llm/client.py` 知道 OpenAI 协议**；只有 `database/` 知道 ORM/SQL。
- `attachments/` 与 `memory/` 两个包**不含**任何 Telegram / OpenAI SDK / ORM 依赖（纯 Python + 文件系统）；`mcp/` 同样不含 Telegram / OpenAI SDK / ORM（只依赖 MCP SDK + 其 HTTP client / stdio 子进程）。
- 模块之间低耦合：Telegram / Agent / Tool / LLM / Database 各自单一职责。

## 模块地图

| 模块 | 单一职责 | 依赖边界 |
| --- | --- | --- |
| `telegram/bot.py` | 唯一的 Telegram 知识来源：鉴权、命令、渲染、发送 | 只调 `AgentService`，不碰 OpenAI SDK |
| `telegram/media.py` | 唯一的 Telegram 媒体下载来源：照片 → `AgentMessage` | 不碰 OpenAI SDK / DB |
| `telegram/markdown.py` | 模型 Markdown → Telegram HTML（含分块、400 回退） | 纯函数 |
| `agent/messages.py` | 渠道无关内容模型：`AgentMessage` + `TextContent`/`ImageContent` | 不碰 Telegram 类型 |
| `agent/context.py` | 唯一的上下文选择者（纯 Python，无 I/O）：估算 + `plan_context()` | 无 Telegram/SDK/ORM/文件系统 |
| `agent/service.py` | 渠道无关核心：锁、持久化、记忆检索、驱动 tool loop | 调 LLM、DB、附件、记忆 |
| `agent/tool_loop.py` | LLM ↔ 工具循环（渠道/协议无关） | 只依赖 LLM 协议 + `ToolRegistry` |
| `tools/` | 工具：`Tool` 接口 / `ToolRegistry` / 三个只读内置 + 可选 `exec`（默认关闭）+ 策略/校验/审批/审计 | 无 Telegram/DB（审计经注入） |
| `mcp/` | MCP 工具 provider（Streamable HTTP + stdio）：启动发现 + 包装成标准 `Tool` | 无 Telegram/DB/OpenAI SDK（只依赖 MCP SDK + HTTP client / stdio 子进程） |
| `attachments/` | 内容寻址 blob 存储（SHA-256、原子写、去重、防路径穿越） | 纯文件系统 |
| `memory/` | 记忆文本规范化 + 词法排序（纯 Python、无 I/O） | 无 Telegram/SDK/ORM |
| `llm/client.py` | 唯一的 OpenAI SDK 知识来源 | 无 Telegram |
| `database/` | ORM + repository（唯一碰 SQL 的层） | 无 Telegram / OpenAI |

## 如何扩展

### 加一个工具

工具是一个 `tools.base.Tool` 子类：设置 `name`、`description`、JSON-schema 的 `parameters`，实现 `async execute(arguments) -> str`。然后注册——在 `tools/builtin/__init__.py::build_default_tools()` 里 `registry.add(...)`，或把自己的 `ToolRegistry` 传给 `AgentService`（`main` 在 `ENABLE_TOOLS=true` 时从 `build_default_tools()` 构建）。**这**就是全部改动：registry 负责在 OpenAI schema 里声明它、按名分发它。

- **不要**在任何地方写 `if name == "…"` 分支——registry 是唯一分发点。
- 工具结果必须是**短**的、人/模型可读的字符串；失败时 `raise`（registry 会转成 `{"error": ...}` 给模型看）。
- 新工具默认 `ask`（需要审批）；只有确认无害/只读的才声明 `allow`。详见 [工具与工具安全](tools.md)。

**MCP 已按此模式接入**（`mcp/` 包，Streamable HTTP + stdio）：启动时发现远程端点或后端 spawn 的本地 stdio 子进程的工具并包成标准 `Tool`（`mcp_<server>__<remote>`、默认 `ask`），注册进同一个 registry，从而完全复用执行边界——见 [远程 MCP 工具](mcp.md)。**只读 SSH 观测（phase 5.1，`infrastructure/` 包）也已按此模式接入**（每个目标三个固定、无参、只读工具，见 [工具与工具安全](tools.md)）。**可选的 `exec` shell 工具**（`ENABLE_EXEC_TOOL=true` 才注册）是本库**第一个真正 spawn 子进程**的能力，同样在工具**内部**封装 subprocess（`/bin/sh -c`、参数向量、进程组杀、静态 denylist 兜底）、恒 `ask`、绝不进 loop——见 [工具与工具安全](tools.md)。未来的 **Docker / Pi** 仍是同一个模式：各是一个 `Tool`（或一个产出若干工具的小 provider），subprocess/网络都封装在工具**内部**、绝不进 loop，并在有副作用时走审批。

### 加一种多模态输入

基于 `AgentMessage` / `ContentPart` 基础加：一个新的 `ContentPart` 子类型（`FileContent`/`AudioContent`/…）+ `telegram/media.py` 里一个来源分支 + `llm/message_converter.py` 里一个渲染分支，其余（agent、tool loop、service）**都不动**。持久化、SHA-256 去重、原子写、重启重入窗、`/new` 回收已经对任意 `attachments.content_type` 通用。详见 [多模态与附件](multimodal.md)。

### RAG / Web 检索

作为工具接入（一个 `search` 工具），让模型决定何时调用；检索逻辑放在 loop 之外。

## 不变量（新增功能时必须守住）

- 不在 agent service 里写 Telegram 逻辑；OpenAI SDK 只出现在 `llm/client.py`。
- 日志**绝不**泄露 secret、消息正文、图片字节/base64；记忆路径只记短 scope 哈希 + id，从不记原始 scope / 内容。
- `attachments/` 与 `memory/` 保持无 Telegram / OpenAI SDK / ORM 依赖。
- 每个按 id 的记忆读/删都在 SQL 里按 `scope + id` 过滤（不泄露存在性）。
- `ENABLE_TOOLS=false` 仍是一次完整的 Phase-1 降级。
- 从不按模型名编码模型能力。
