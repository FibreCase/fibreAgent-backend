# Agent Backend

一个运行在你自己服务器上的**最小可用的个人 AI Agent Backend**：在 Telegram 里和一个基于 OpenAI 兼容模型的 Agent 对话，**对话历史持久化**——重启后上下文不丢失；Agent 可以调用少量安全的内置工具来回答问题，也支持**图片输入**——收到的图片会持久化（内容寻址 blob，按 SHA-256 去重），并在后续历史上下文（含重启后）里重新作为图片交给模型。

> **重要：当前内置工具仅限只读/无害操作**：`get_current_time`（当前时间）、`echo`（回显）、`system_info`（主机名/平台/Python 版本）。
> 它**不能**执行命令、控制设备、读写文件、联网扫描。如果用户要求超出工具能力的操作，它会明确说明「当前尚未配置相应工具」。

架构上它不是「一个 Telegram chatbot」，而是从第一版就分层，Tool loop 也已经插在 `Agent Service` 和 `LLM Client` 之间：

```
Telegram Adapter   →   Agent Service   →   Tool Loop   →   LLM Client   →   Persistent Conversation
（适配器）            （渠道无关的核心）    （工具循环）    （OpenAI 兼容）   （SQLite）
                                  ↑
                          Tool Registry（get_current_time / echo / system_info）
```

要扩展能力，只需往 Tool Registry 里加一个实现 `Tool` 接口的工具（未来 MCP/SSH/Docker/Pi 都是这样接入的 Tool Provider），**不用重写 Telegram 层**。

---

## 1. 项目简介

- 通过 **Telegram Bot**（long polling）与 Agent 对话。
- 使用远程 **OpenAI 兼容**（Chat Completions API）的 LLM 生成回复。
- 对话历史持久化到 **SQLite**，重启后可完整恢复 conversation context。
- 支持**工具调用**：内置 `get_current_time` / `echo` / `system_info` 三个只读安全工具，可开启/关闭。
- 支持**图片输入**：把 Telegram 照片（可带说明文字）发给 Agent，它会把图片（base64 内联，模型端无需访问 Telegram）连同文字一起交给模型；图片大小受 `MAX_IMAGE_SIZE_MB` 限制。
- **图片持久化**：收到的图片以内容寻址 blob 落盘（`ATTACHMENT_STORAGE_PATH`，默认 `./data/attachments`，按 SHA-256 去重、原子写入），并在后续历史上下文里重新作为图片交给模型——**重启后**，只要那条消息仍在 `MAX_CONTEXT_MESSAGES` 窗口内，模型依然看得到它。`/new` 会清理不再被任何消息引用的图片（被多条消息共用的去重 blob 不会误删）。
- **Markdown 自动渲染**：模型的 `**加粗**`、`*斜体*`、`~~删除线~~`、`` `代码` ``、` ``` ` 代码块、链接、标题会转成 Telegram HTML 显示（不再是字面的 `**`/`` ` ``）；若某段无法解析会自动回退为纯文本，回复永不丢失。
- 仅允许你配置的 Telegram User ID 使用，其他人静默拒绝。
- 无 Web UI、无 MCP、无危险/状态变更类工具、无外部依赖数据库。

---

## 2. Architecture

```
                        ┌─────────────────────────────────────────┐
   Telegram  ──poll──▶  │  telegram/bot.py  (Adapter)             │
   (long polling)       │  · 鉴权 (allow-list)                    │
                        │  · /start /new /context /help /status     │
                        │  · typing 保活                          │
                        │  · Markdown→HTML 渲染 + 分块发送          │
                        └───────────────┬─────────────────────────┘
                                        │  只调用 AgentService
                                        ▼
                        ┌─────────────────────────────────────────┐
                        │  agent/service.py  (AgentService)       │
                        │  · per-conversation asyncio.Lock        │
                        │  · 组装 context (system + 最近 N 条)      │
                        │  · 保存 user / assistant 消息            │
                        └───────────────┬─────────────┬───────────┘
                                        │             │
                              ┌────────────────┐   ┌────────────────────────┐
                              │ llm/client.py  │   │ database/repository.py │
                              │ (OpenAIClient) │   │ SQLAlchemy 2.x async   │
                              │ OpenAI SDK     │   │ aiosqlite / SQLite     │
                              └────────────────┘   └────────────────────────┘
```

关键分层约束：

- **Telegram 层从不直接调用 OpenAI SDK**，只调用 `AgentService.process_message()`。
- **Agent Service 渠道无关**：未来接 Web UI / Discord / HTTP API 都复用同一个 `AgentService`。
- **只有 `llm/client.py` 知道 OpenAI 协议**；只有 `database/` 知道 ORM/SQL。
- 四个模块（Telegram / Agent / LLM / Database）之间低耦合。

---

## 3. Requirements

- **Python 3.13+**（本项目已在 Python 3.14 上验证）。
- **[uv](https://docs.astral.sh/uv/)**（用于管理依赖与虚拟环境）。
- 一个 **OpenAI 兼容**的 LLM API endpoint + API key（如 OpenAI 本身、本地模型服务或第三方中转）。
- 一个 Telegram Bot token。

不需要安装任何数据库服务——SQLite 文件在启动时自动创建。

---

## 4. Installation

```bash
# 在仓库根目录
uv sync                 # 安装运行时 + 开发依赖到 .venv
```

验证测试可运行（全部用 mock，不会真的调用你的 LLM）：

```bash
uv run pytest -q
```

---

## 5. Configuration

所有配置都来自环境变量（或 `.env` 文件）。**Secret 永远不要写进代码，也不要提交 Git**（`.env` 与 `data/` 已在 `.gitignore` 中忽略）。

```bash
cp .env.example .env
# 然后编辑 .env 填入真实值
```

| 变量 | 说明 |
| --- | --- |
| `OPENAI_BASE_URL` | **API 前缀**。你的 endpoint 形如 `https://<host>/v1/chat/completions`，但 OpenAI SDK 会自动追加 `/chat/completions`，所以这里只填前缀（如 `https://<host>/v1`，**不要**填完整 URL）。**必填。** |
| `OPENAI_API_KEY` | LLM API key（**必填，仅来自环境变量**）。 |
| `OPENAI_MODEL` | 模型名（**必填**，填你的服务端支持的模型名）。 |
| `OPENAI_TIMEOUT` | 单次请求超时（秒），默认 `120`。 |
| `TELEGRAM_BOT_TOKEN` | Bot token（**必填，仅来自环境变量**）。 |
| `TELEGRAM_ALLOWED_USER_IDS` | 允许的 Telegram user id，逗号分隔，如 `123456789,987654321`。其他人会被静默拒绝（仅记服务端日志）。 |
| `DATABASE_URL` | SQLite 连接串，默认 `sqlite+aiosqlite:///./data/agent.db`。父目录会自动创建。 |
| `SYSTEM_PROMPT_PATH` | system prompt 文件路径，默认 `config/system_prompt.txt`。 |
| `SYSTEM_PROMPT` | 可选：内联 system prompt，**若设置则覆盖文件**。 |
| `MAX_CONTEXT_MESSAGES` | context 中携带的**最近 N 条消息**（消息数，不是 token 数），默认 `50`，另加一条 system 消息。 |
| `MAX_CONTEXT_ESTIMATED_TOKENS` | 一次请求（system + 选中的历史 + 当前消息）的**估算** token 预算上限，默认 `24000`。这是一个**模型无关的保守估算**——不是 provider 计费 token，也不做模型专用 tokenization——与 `MAX_CONTEXT_MESSAGES` 共同约束 context。超预算时按「完整历史 turn、从新到旧」选取，必要时把历史图片降级为纯文本（不读取、不发送该图）。 |
| `CONTEXT_IMAGE_ESTIMATED_TOKENS` | 估算中每张保留在 context 中的图片的成本，默认 `2000`。 |
| `ENABLE_TOOLS` | 是否启用工具调用循环，默认 `true`。设为 `false` 时完全退回纯对话行为（不传 tools、不做任何工具相关持久化）。 |
| `MAX_TOOL_ITERATIONS` | 单条消息内 LLM↔工具的最大往返次数，默认 `5`。超过则返回一条通用的「工具调用次数过多」提示。 |
| `MAX_IMAGE_SIZE_MB` | 单张 Telegram 图片的最大字节数（MB），默认 `10`。超过则返回「图片过大，暂时无法处理。」，不会发给模型。 |
| `ATTACHMENT_STORAGE_PATH` | 持久化图片附件 blob 的根目录，默认 `./data/attachments`（相对工作目录，目录按需自动创建）。图片**字节**存在这里（按 SHA-256 内容寻址、去重、原子写入），数据库里只存元数据。Docker 下默认路径落在 `./data` 绑定挂载内，随容器持久化。 |
| `LOG_LEVEL` | 日志级别，默认 `INFO`。 |

> ⚠️ `OPENAI_BASE_URL` 是最容易踩坑的一项。已经用本地 HTTP server 实测验证：填 `.../v1` 时，SDK 实际请求的就是 `.../v1/chat/completions`，与你的 endpoint 完全一致。

---

## 6. Telegram Bot 创建方法

1. 在 Telegram 里搜索 **@BotFather** 并发送 `/start`。
2. 发送 `/newbot`，按提示给 bot 起名字和用户名（用户名必须以 `bot` 结尾）。
3. BotFather 会返回一段 **Bot Token**（形如 `123456789:AA...`）。把它填到 `.env` 的 `TELEGRAM_BOT_TOKEN`。
4. 获取**你自己的 Telegram user id**：
   - 给 **@userinfobot** 发任意消息，它会回复你的 `id`。
   - 把它填到 `TELEGRAM_ALLOWED_USER_IDS`。
5. **必须先用 @BotFather 把 bot 设为 privacy 模式？不需要**——本项目只处理你主动发给它的消息，默认即可。

> 注意：bot 只能收到「你主动发给它」或「以 `/` 开头的命令」消息。首次使用请先发 `/start`。

---

## 7. 启动方法

```bash
# 确保在仓库根目录，且 .venv 已 uv sync
uv run python -m fibrecase_agent_backend
# 或等价地
uv run fibrecase-agent-backend
```

启动后：

- 自动创建 `data/agent.db`（如不存在）。
- 开始 Telegram long polling（无需公网入站/无需 webhook）。
- 看到日志 `telegram long polling started` 即表示就绪。
- 用 `Ctrl+C` 停止；会优雅关闭 LLM 客户端与数据库连接。

> long polling 的原因：你的服务器可能没有公网 HTTP 入站能力，long polling 只出站连接 Telegram。

### 可用命令

Bot 支持以下命令（输入 `/` 会弹出 Telegram 原生命令菜单，或发 `/help` 查看）：

| 命令 | 作用 |
| --- | --- |
| `/start` | 启动 Agent / 查看当前会话（无会话时自动创建） |
| `/new` | 开始新会话，清空本 chat 的历史上下文 |
| `/context` | 查看上下文窗口状态：消息上限、已存入/本次保留条数、估算 token 预算的占用与剩余、历史图片保留/降级数量（只读预览，估算非精确 token） |
| `/status` | 查看运行状态（版本、模型、会话 id、消息数） |
| `/help` | 列出本帮助 |

其它任何文字消息都会作为对话发给 Agent。

---

## 8. System Prompt 配置

默认读取 `config/system_prompt.txt`（文件优先）。也可以改用环境变量 `SYSTEM_PROMPT` 覆盖文件（设置后忽略文件）。若两者都没有，使用一个内置兜底 prompt。

编辑 `config/system_prompt.txt` 即可调整 Agent 的语气与边界，无需改代码。

---

## 9. SQLite 数据库说明

- 文件：默认 `./data/agent.db`（由 `DATABASE_URL` 决定）。
- 启动时自动初始化（`CREATE TABLE IF NOT EXISTS`），可安全重复启动。
- 表结构：

  **conversations**
  | 字段 | 说明 |
  | --- | --- |
  | `id` | 自增主键（`AUTOINCREMENT`，reset 后必为新的更大 id） |
  | `telegram_chat_id` | Telegram chat 唯一 id（一个 chat 对应一个 conversation） |
  | `telegram_user_id` | 创建该 conversation 的 Telegram user id |
  | `created_at` / `updated_at` | 时间戳 |

  **messages**
  | 字段 | 说明 |
  | --- | --- |
  | `id` | 自增主键 |
  | `conversation_id` | 外键 → conversations.id（级联删除） |
  | `role` | `system` / `user` / `assistant`（schema 已允许 `tool`，为工具调用预留；当前只写 user/assistant） |
  | `content` | 消息文本（**纯文本**；图片字节不存这里，见 `attachments`） |
  | `created_at` | 时间戳 |

  **attachments**
  | 字段 | 说明 |
  | --- | --- |
  | `id` | 自增主键 |
  | `message_id` | 外键 → messages.id（级联删除：删消息即删其附件元数据） |
  | `sha256` | 图片内容 SHA-256（内容寻址键；同一图片去重为同一 blob） |
  | `storage_key` | blob 在 `ATTACHMENT_STORAGE_PATH` 下的相对路径（`<前2位>/<完整hash>`） |
  | `content_type` | `image`（目前只写图片；字段为未来的 File/Audio/Video 预留） |
  | `mime_type` | 如 `image/jpeg` / `image/png` / `image/webp`（按魔数嗅探） |
  | `size_bytes` | 字节数 |
  | `filename` | 原始文件名（可空） |
  | `position` | 该图在消息内容中的顺序（让「图+说明」重启后按原顺序还原） |
  | `created_at` | 时间戳 |

  > 图片**字节**不存数据库，存 `ATTACHMENT_STORAGE_PATH` 下的内容寻址 blob（`<root>/<hash[:2]>/<hash>`，按 SHA-256 去重、原子写入）。数据库里只有这些元数据行，用 `sha256` 指回 blob。

- **一个 Telegram chat 对应一个 conversation**，`/new` 只影响该 chat。
- 用命令行直接查看（可选）：
  ```bash
  uv run python -c "import sqlite3;print(sqlite3.connect('data/agent.db').execute('select count(*) from messages').fetchone())"
  ```

---

## 10. Troubleshooting

| 现象 | 排查 |
| --- | --- |
| 启动报 `ConfigError: TELEGRAM_BOT_TOKEN is not set` 等 | `.env` 没加载或变量名为空。确认 `cp .env.example .env` 且填了值；确认在**仓库根目录**运行（`.env` 从当前工作目录读取）。 |
| Bot 完全不回复 | 确认 `TELEGRAM_ALLOWED_USER_IDS` 里确实有**你的** id（@userinfobot 查）；确认你发的是普通消息或 `/` 命令；看日志是否有 `unauthorized telegram user attempted access`。 |
| `模型请求超时，请稍后重试。` | LLM 请求超时或网络问题。查看服务端日志的 `llm request timed out` / `llm http error status=...`。可临时调大 `OPENAI_TIMEOUT`。 |
| `模型服务暂时不可用。` | LLM 返回了 HTTP 错误 / 空回复 / 连接失败。看日志里的 HTTP status；检查 `OPENAI_BASE_URL` 是否写错（常见错误是填了完整 `.../v1/chat/completions`）。 |
| 请求打到 `404` / 错误路径 | 几乎肯定是 `OPENAI_BASE_URL` 多写了 `/chat/completions`。应只填前缀（如 `.../v1`）。 |
| 想确认持久化 | 对话历史写在 `data/agent.db`；用第 9 节的查询示例查看 `conversations`/`messages`，重启 backend 后数据仍在。 |

> 日志中**不会**出现 Telegram token、OpenAI API key、完整 Authorization header、服务器路径，也不默认记录完整消息内容（只记 conversation_id、message_id、长度、延迟）。

---

## 11. Docker 部署

仓库自带 `Dockerfile` 与 `docker-compose.yaml`。镜像构建走 `uv`，安装的是 `uv.lock` 里锁定的**精确**依赖版本；运行时以非 root 用户（uid 10001）启动。

**关键点：** 本服务只发起**出站**连接（Telegram long polling + LLM API），**没有任何入站端口**，所以 compose 里**不声明 `ports`**——这是正常的，不是漏配。

### 用 Docker Compose

```bash
cp .env.example .env                   # 与本地 uv run 共用同一个 .env（.env 已被 gitignore）
docker compose up -d --build           # 构建并后台运行
docker compose logs -f                 # 看日志；出现 "agent backend initialised" 即就绪
docker compose down                    # 停止（数据卷保留）
```

### 持久化与配置

- **同一个 `.env`**：Docker 与本地 `uv run fibrecase-agent-backend` **共用同一个 `.env`**（均以 `.env.example` 为模板）。容器通过 compose 的 `env_file: .env`（或 `docker run --env-file .env`）在**运行时**读取它，`.env` **不会**被打进镜像（`.dockerignore` 已排除）。改配置只改这一处文件，两种启动方式一致。
- **路径无需为容器单独改**：`.env` 里 `DATABASE_URL` 与 `SYSTEM_PROMPT_PATH` 都是**相对路径**，容器内 `WORKDIR=/app`，所以它们解析到 `/app/data/agent.db` 与 `/app/config/system_prompt.txt`，与本地运行的相对语义一致。
- **数据持久化 + 目录权限**：SQLite 库在容器内 `/app/data/agent.db`。compose 用**绑定挂载**把它落到仓库下的 `./data/`（`./data:/app/data`），**与本地 `uv run` 共用同一个 `data/`**——同一个库文件、同一套宿主权限。
  - **为什么不 chown**：绑定挂载会用**宿主目录自身的属主/权限**覆盖镜像里的 `/app/data`。若 `data/` 是 `1000:1000 755`，而容器以 uid 10001 运行，容器用户落在 *other* 位（只读），首次建库会 `EACCES`。**解法：让容器以宿主用户身份运行**——compose 里 `user: "${HOST_UID:-1000}:${HOST_GID:-1000}"`，在 `.env` 里设 `HOST_UID`/`HOST_GID`（`id -u`/`id -g`；Linux 首个用户常为 1000，macOS 默认用户常为 501）。这样 `data/` 保持你默认的 `755` 就能被容器写入（因为是 *owner*），你在宿主上也照样能读库，无需任何 `chown`。
  - 设好 `HOST_UID/HOST_GID` 后直接 `docker compose up -d --build` 即可；`data/` 若不存在，`create_engine` 会自动建（首次可能由 root 建为空目录，属主随你而变，一般无碍）。
  - 若你坚持容器用固定的专用 uid（如 10001）而不想以宿主用户跑，那就改用**命名卷** `-v agent-data:/app/data`：Docker 首次使用会按镜像里 `/app/data` 的属主初始化，省去宿主 chown；代价是查库要走 `docker volume inspect` / 辅助容器，不能直接 `ls ./data`。
- **系统提示词**：镜像里已内置 `config/system_prompt.txt`（`WORKDIR=/app`，与 `.env` 中 `SYSTEM_PROMPT_PATH` 的相对路径一致）。想临时改用别的文件而不重建镜像，可在 compose 里加一行挂载（文件内已注明）。
- **时区**：`python:slim` 镜像默认是 **UTC**，所以容器里的 `get_current_time`（`datetime.now()`）和日志时间戳会跟你的墙钟差几个小时。compose 通过 `environment: TZ=${TZ:-Asia/Shanghai}` 注入时区（镜像自带 `tzdata`）。把 `.env` 里 `TZ` 改成你的 IANA 时区（如 `Asia/Shanghai`）即可与宿主一致。**用 `TZ` 而不是挂载 `/etc/localtime`**，因为 macOS 上根本没有 `/etc/localtime` 可挂，Linux 上挂载也只会影响 `TZ=...` 未设置时的兜底。仅影响 Docker；本地 `uv run` 直接用宿主时区，无需设置。
- **优雅停机**：`docker stop` / `compose down` 发 SIGTERM，PTB 会捕获并触发 `post_shutdown`（关闭 LLM 客户端与数据库连接），不会丢正在写的 SQLite 数据。

---

## 12. 当前开发状态

一个已在 Telegram 上跑通的最小个人 Agent backend：长轮询适配、渠道无关的核心服务、OpenAI 兼容 LLM 客户端、SQLite 持久化对话，外加**工具调用**与**图片输入/持久化**。当前能做的，以及有意不做的事，都收敛在这一节。

**能做的**
- **工具调用**：`agent/tool_loop.py` 在 `Agent Service` 与 `LLM Client` 之间循环执行工具——拿到模型返回的 `tool_calls` 就按名经 registry 执行、把 `tool` 结果回灌 messages，直到模型给出最终文本（或用尽 `MAX_TOOL_ITERATIONS`）。`tools/` 提供 `Tool` 接口 + `ToolRegistry`（注册 / 生成 OpenAI schema / 按名执行）+ 三个只读内置工具。加新工具只需实现 `tools.base.Tool` 并在 `tools/builtin/__init__.py::build_default_tools()` 里 `registry.add(…)`——**不要**在别处写 `if name == "…"` 分支，registry 是唯一分发点。
- **图片输入 + 持久化**：Telegram 照片（可带说明文字）经 `telegram/media.py` 规范化为渠道无关的 `AgentMessage`，图片 base64 内联交给模型；收到的图片以内容寻址 blob 落盘并在后续历史里重新入窗（见第 9 节 `attachments` 表）。
- **上下文预算管理**：除了按 `MAX_CONTEXT_MESSAGES` 计消息数，还有一道**模型无关的估算 token 预算**（`MAX_CONTEXT_ESTIMATED_TOKENS`）。每次请求前按「完整历史 turn、从新到旧」选取历史，使请求同时满足消息数与估算预算；某个历史 turn 的图片放不进去时，该 turn 降级为纯文本（其图片**不读取、不发送**），而不跳过去挑更旧的内容。当前请求本身永远保留、其图片不降级；若仅 system + 当前请求就已超预算，则不调用 LLM，回一条「请缩短文字或减少图片」的安全提示。
- **降级开关**：`ENABLE_TOOLS=false` 时完全退回纯对话（不传 tools、不做工具相关持久化）；图片处理与工具开关相互独立。

**有意不做 / 限制**
- **仅 3 个只读工具**（`get_current_time` / `echo` / `system_info`）：无 shell 执行、文件读写、联网扫描、SSH/Docker 或任何状态变更类工具；工具参数不做 schema 校验、无权限审批、单个工具无独立超时；tool 往返不落库、无法事后回放/审计。
- **图片仅本地磁盘、仅照片**：blob 只落 `ATTACHMENT_STORAGE_PATH`，无配额/后台 GC/单附件删除（`/new` 是唯一回收点）；文档/贴纸/视频/音频仍被丢弃（`ContentPart` 已为它们预留）。
- **估算而非精确 token**：`MAX_CONTEXT_ESTIMATED_TOKENS` 是一个保守、确定、模型无关的**估算**，用于相对选择与保护，不等于 provider 的计费 token；若实际 endpoint 仍报上下文超长，会走既有的安全 `http_error` 提示，本阶段不做重试或自动探测模型窗口。
- **未涉及**：MCP、SSH/Docker/Pi、Web search/RAG、向量库、Redis/PostgreSQL、Web 前端、OAuth、多 Agent、autonomous loop、cron/scheduler、memory summarization、语音/TTS/STT。

**下一步（建议顺序）**
- **长期记忆（Memory）**：在已建好的预算化 context 之上，做显式、可控的长期记忆检索（而非把长对话硬塞进窗口）。
- **新 `ContentPart` 类型**（File / Audio / Video / Sticker）：复用同一套附件存储与重入窗机制，只需补对应的下载分支与 OpenAI 映射。
- 新的工具能力（如 MCP）以 **Tool Provider** 接入同一个 `Tool`/`ToolRegistry` 接口，不改动 service / LLM client / Telegram 层；有副作用的工具上线前必须先加权限审批。
