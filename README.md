# FibreCase's Agent Backend

![Poster](.repo_fiiles/img/poster.jpg)

一个运行在你自己服务器上的**最小可用的个人 AI Agent Backend**：在 Telegram 里和一个基于 OpenAI 兼容模型的 Agent 对话，**对话历史持久化**——重启后上下文不丢失；Agent 可以调用少量安全的内置工具来回答问题，也支持**图片输入**（收到的图片持久化、重启后仍在上下文里作为图片交给模型），还能用 `/remember` **显式保存长期记忆**（账号隔离、跨 `/new` 与重启保留）。

> **默认部署只做只读/无害操作**：内置的 `get_current_time` / `echo` / `system_info` 都是只读、无副作用；默认**不** spawn 子进程、**不**写文件、**不**联网扫描。两个**默认关闭（opt-in）**的状态变更能力 `exec`（跑 shell 命令）与 `file` 工具集（`FILE_WORKDIR` 内做文件/目录操作，`file_read` / `file_ls` 只读、`allow`，其余 `ask`）需显式开启；除只读的 `file_read` / `file_ls` 外**每次调用都需人工审批**（`ask`）——安全细节见 [工具与工具安全](docs/tools.md)。

## 架构

它不是「一个 Telegram chatbot」，而是从第一版就分层——Telegram 只是一层薄适配器，核心是**渠道无关**的 `AgentService`，工具循环插在核心和 LLM 客户端之间。未来接 Web UI / Discord / API 都复用同一个核心，**不改 Telegram 层**。

```
Telegram Adapter   →   Agent Service   →   Tool Loop   →   LLM Client   →   Persistent Conversation
（适配器）            （渠道无关的核心）    （工具循环）    （OpenAI 兼容）   （SQLite）
                                  ↑
                          Tool Registry（get_current_time / echo / system_info）
```

- 通过 **Telegram Bot**（long polling）对话；用 **OpenAI 兼容** LLM 生成回复。
- **工具调用 + 工具安全**：每次调用都过一道统一边界——`allow`/`ask`/`deny` 策略 → JSON Schema 校验 → 需要时的一次性人工审批 → 单工具超时 → 只记元数据的 append-only 审计（`/tool_audit` 查看，绝不显示参数/结果）。
- **远程 MCP 工具**（Streamable HTTP + stdio 本地进程）：启动时把发现的工具注册进同一个 registry（`mcp_<server>__<remote>`、默认 `ask`），**完全复用**上面的安全边界——MCP 只是又一个工具 provider；支持 **per-user OAuth**（`/mcp auth`，凭据绑定到你的账号、自动刷新，`/new` 与重启不影响登录态）。`/mcp_status` 只读查看状态。
- **只读基础设施观测（SSH）**：对运维配置的目标各产出三个**固定、无参、只读**工具（主机 / 磁盘 / systemd 服务状态，默认 `allow` 无需审批），同样复用上面的安全边界——模型无法指定 host/path/service。`/infra_status` 只读查看（不连接、不探活）。
- **定时任务（cron）**：在启动配置里声明一个 cron 任务后，后端到点**自动**跑一次 Agent（专属、全新的会话，运行后删除、不留痕），完成后向绑定聊天发一条格式化通知；**完全复用** `process_message` 的既有安全边界（审批卡经 `delivery_chat_id` 路由到你的聊天），**只来自启动配置**（改需重启、不暴露新端口）。`/schedule_status` 只读查看（不触发运行、不显示 prompt）。
- **图片输入 + 持久化**：照片（可带说明）base64 交给模型，并以内容寻址 blob 落盘（SHA-256 去重、原子写），重启后仍在窗口内时重新入窗。
- **上下文预算管理**：消息数窗口之上再有一道模型无关的估算 token 预算，必要时把历史图片降级为纯文本，绝不让超长请求打爆端点。
- **显式长期记忆**：`/remember` 显式保存，纯词法检索，跨 `/new` 与重启保留，作为一条明确标注的参考消息注入（不是自动摘要、不是 RAG）。
- **Markdown 自动渲染**：模型回复的加粗/斜体/代码块/链接转成 Telegram HTML，无法解析时回退纯文本，回复永不丢失；最终回答以 Reply 引用你发的那条消息（长回复只在首段引用一次）。
- 仅允许你配置的 Telegram User ID 使用，其他人静默拒绝。

## 快速开始

**要求**：Python 3.13+（已在 3.14 验证）、[uv](https://docs.astral.sh/uv/)、一个 OpenAI 兼容 LLM endpoint + key、一个 Telegram Bot token。无需装数据库——SQLite 文件启动时自动创建。

```bash
uv sync                 # 安装运行时 + 开发依赖到 .venv
cp .env.example .env    # 然后编辑 .env 填入真实值
uv run python -m fibrecase_agent_backend
```

- **创建 Bot 与拿到自己的 user id**：见 [Telegram 接入](docs/telegram.md)。
- **配置项全表**（`OPENAI_BASE_URL` 前缀规则、工具/预算/记忆/超时等所有变量）：见 [Configuration](docs/configuration.md)。
- **跑 Docker**：见 [Docker 部署](docs/deployment.md)。

看到日志 `telegram long polling started` 即就绪，发 `/start` 开始对话。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/start` | 启动 Agent / 查看当前会话 |
| `/new` | 开始新会话，清空本 chat 历史（**不影响**长期记忆） |
| `/stop` | 打断本 chat 正在生成/执行工具的回复；被中断的那一轮会发一条引用原消息的 Reply「⛔️ **Interrupted.**」，无进行中回复时回复「Nothing to stop.」。只停生成、不清会话/记忆、只影响本 chat |
| `/context` | 只读预览当前上下文窗口：消息数、估算 token 占用/剩余、历史图片保留/降级数 |
| `/remember <内容>` | 保存一条长期记忆到你的账号（跨 `/new` 与重启）；回显 ID |
| `/memories` | 列出你保存的所有记忆 |
| `/forget <id>` | 删除指定记忆；不存在或不属于你 → 未找到 |
| `/forget all CONFIRM` | 清空你账号下全部记忆（破坏性，必须带 `CONFIRM`） |
| `/status` | 查看运行状态（版本、模型、会话 id、消息数） |
| `/tool_audit [limit]` | 查看你本人的工具执行审计（只显工具名/事件/结果码/耗时） |
| `/mcp` | 只读查看 MCP 服务器状态，并显示**你本人**在各 OAuth 服务器上的登录状态 |
| `/mcp auth <server>` | 为你的账号发起第三方 OAuth 登录（如 `gcal`），返回一个**登录按钮**（无需复制 URL）；凭据绑定到你的账号、自动刷新 |
| `/mcp_status` | 只读查看已配置远程 MCP 服务器状态（available/unavailable、工具数、总数）；不发起连接、不显示 URL/token |
| `/infra_status` | 只读查看已配置的 SSH 观测目标及其三个工具（read-only）；不连接、不探活 |
| `/schedule_status` | 只读查看已配置定时任务的名字 + cron + 下次触发时间；不显示 prompt/chat_id、不触发运行 |
| `/help` | 列出帮助 |

其它任何文字消息都会作为对话发给 Agent。完整说明见 [Telegram 接入与命令](docs/telegram.md)。

## 文档

详细技术细节都在 [`docs/`](docs/)：

| 文档 | 内容 |
| --- | --- |
| [Architecture](docs/architecture.md) | 分层、模块地图、扩展方式（工具 / 多模态 / RAG）、不变量 |
| [Configuration](docs/configuration.md) | 全部环境变量参考、校验规则、system prompt |
| [Telegram 接入](docs/telegram.md) | Bot 创建、启动、完整命令参考 |
| [数据库](docs/database.md) | `conversations` / `messages` / `attachments` / `memories` / `tool_audit_events` / `oauth_credentials` / `oauth_authorization_states` 表结构 |
| [工具与工具安全](docs/tools.md) | 工具循环、allow/ask/deny、Schema 校验、一次性审批、超时、审计、加新工具 |
| [定时任务](docs/scheduling.md) | cron 语法、专属会话与保留区间、审批路由、调度不变量、`/schedule_status` |
| [远程 MCP 工具](docs/mcp.md) | Streamable HTTP + stdio 启动发现、命名空间、默认 `ask`、复用工具安全边界、`/mcp_status` |
| [多模态与附件](docs/multimodal.md) | 图片输入、内容寻址 blob 存储、重入窗、`/new` 回收 |
| [上下文管理](docs/context-management.md) | 估算器、turn 粒度选取、降级规则 |
| [长期记忆](docs/memory.md) | `/remember`、词法检索、注入方式、安全边界 |
| [Docker 部署](docs/deployment.md) | compose、共享 `.env`、绑定挂载与宿主用户权限、时区、CI |
| [Troubleshooting](docs/troubleshooting.md) | 常见现象与排查 |

## 当前开发状态

已在 Telegram 上跑通、全部通过测试的最小个人 Agent backend。核心能力：工具调用 + 工具安全、远程 MCP 工具（Streamable HTTP + stdio，默认 `ask`）+ **MCP 用户级 OAuth**、**只读基础设施观测（SSH）**、**定时任务（cron）**（启动配置声明、到点自动跑一次、专属全新会话、复用既有安全边界）、**可选的 `exec` shell 工具**与**可选的 `file` 文件工具集**（两者均**默认关闭**；`file` 里 `file_read` / `file_ls` 只读免审批，其余每次调用恒需人工审批）、图片输入/持久化、上下文预算管理、显式长期记忆、Markdown 渲染。

> **默认部署零子进程、零文件写入、零联网扫描**——两个状态变更能力 `exec` / `file` 是 opt-in 的（`ENABLE_EXEC_TOOL` / `ENABLE_FILE_TOOL`），且各自带静态兜底（`exec` 的危险命令 denylist、`file` 的 `FILE_WORKDIR` 路径受限）与逐次审批。**完整的能力清单、安全说明、有意不做 / 限制与下一步，见 [当前开发状态](docs/status.md)；`exec` / `file` 的安全细节见 [工具与工具安全](docs/tools.md)。**
