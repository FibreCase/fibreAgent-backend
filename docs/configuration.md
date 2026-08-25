# Configuration

所有配置都来自环境变量（或 `.env` 文件）。**Secret 永远不要写进代码，也不要提交 Git**（`.env` 与 `data/` 已在 `.gitignore` 中忽略）。

```bash
cp .env.example .env
# 然后编辑 .env 填入真实值
```

## 变量参考

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
| `TOOL_PERMISSION_OVERRIDES` | 逐工具权限覆盖，逗号分隔的 `<工具名>=allow\|ask\|deny`（如 `echo=deny,my_tool=ask`），默认空（用各工具自带默认值：`get_current_time` / `echo` 为 `allow`，`system_info` 当前刻意设为 `ask`，其余新工具默认 `ask`）。这是**启动期强校验**：条目缺 `=`、空工具名、空权限、非法权限值、重复工具名，或工具名含 `[A-Za-z0-9_-]` 之外的字符，都会导致启动失败（`ConfigError`），不会被静默忽略。 |
| `TOOL_APPROVAL_TIMEOUT_SECONDS` | 对 `ask` 策略工具，等待 Telegram 审批（`Approve`/`Deny`）的秒数，超时则按「审批已过期」处理，默认 `60`。必须为正数。 |
| `TOOL_TIMEOUT_SECONDS` | 单个工具调用的最长执行秒数；超时即取消该工具并回给模型「工具超时」，默认 `30`。必须为正数。 |
| `MCP_SERVERS` | 可选：远程 MCP（Streamable HTTP）服务器列表，JSON **数组**。默认空 = 不建 MCP 客户端、永不发起任何 MCP 网络连接。每个对象 `{ "name", "url", "bearer_token_env"? }`：`name` 为 `[a-z][a-z0-9_-]{0,31}` 且唯一；`url` 为绝对 `https://`（含 host，**不含** userinfo/fragment/query）；`bearer_token_env` 是**环境变量名**（不是 token 本身），其值必须非空，启动时读作 `Authorization: Bearer` 头。发现的工具命名为 `mcp_<server>__<remote>`，默认 `ask`（可用 `TOOL_PERMISSION_OVERRIDES` 按命名空间名覆盖）。**启动期强校验**，任何违规都是 `ConfigError`。 |
| `MCP_CONNECT_TIMEOUT_SECONDS` | 每个 MCP 服务器「连接 / initialize / tools-list」握手的超时秒数，超时即把该服务器标记为 unavailable（**其余服务器与内置工具照常启动**，bot 不会因一个可选 MCP 服务器宕机而启动失败）。默认 `10`，必须为正。 |
| `MAX_MCP_TOOL_RESULT_CHARS` | 单个远程 MCP 工具结果回传给模型的**文本**字符硬上限。超大的结果按稳定码 `mcp_result_too_large` 拒绝（**不截断、不回显**）。默认 `10000`，必须 `>= 1`。 |
| `MCP_ALLOW_INSECURE_HTTP` | 硬开关：允许 `http://`（明文）端点。默认 `false`（仅 `https`）；仅在你**控制**的本地/内网可信端点才设 `true`。 |
| `MAX_IMAGE_SIZE_MB` | 单张 Telegram 图片的最大字节数（MB），默认 `10`。超过则返回「图片过大，暂时无法处理。」，不会发给模型。 |
| `ATTACHMENT_STORAGE_PATH` | 持久化图片附件 blob 的根目录，默认 `./data/attachments`（相对工作目录，目录按需自动创建）。图片**字节**存在这里（按 SHA-256 内容寻址、去重、原子写入），数据库里只存元数据。Docker 下默认路径落在 `./data` 绑定挂载内，随容器持久化。 |
| `MAX_MEMORIES_PER_SCOPE` | 每个账号（scope）可保存的记忆条数上限，默认 `200`。超过时 `/remember` 返回「记忆已达上限」提示。 |
| `MAX_MEMORY_CHARS` | 单条记忆的最大字符数（去除首尾空白后），默认 `1000`。超长的 `/remember` 会被拒绝（`memory_invalid`）。 |
| `MAX_RETRIEVED_MEMORIES` | 单次检索最多返回/注入的相关记忆条数，默认 `5`。 |
| `MAX_MEMORY_ESTIMATED_TOKENS` | 注入记忆的**估算** token **子预算**（与 `MAX_CONTEXT_ESTIMATED_TOKENS` 同一套模型无关的估算单位，不是计费 token），默认 `3000`。放不进该子预算的记忆会被**跳过**（不截断），并继续尝试分数更低的记忆；必须 `<= MAX_CONTEXT_ESTIMATED_TOKENS`。 |
| `LOG_LEVEL` | 日志级别，默认 `INFO`。 |
| `LOG_COLOR` | 日志级别标签是否上色（`INFO` 绿 / `WARNING` 黄 / `ERROR` 红）。`auto`（默认）= 仅当 stdout 是终端时才上色，管道/重定向（如 `docker logs`、写文件）保持纯文本；`true` 恒上色；`false` 恒不上色。 |

> ⚠️ `OPENAI_BASE_URL` 是最容易踩坑的一项。已经用本地 HTTP server 实测验证：填 `.../v1` 时，SDK 实际请求的就是 `.../v1/chat/completions`，与你的 endpoint 完全一致。

### 只有 Docker 读的变量（应用不读）

这些只被 `docker-compose.yaml` 使用，应用本身不读，仍放在同一个 `.env` 里：

| 变量 | 说明 |
| --- | --- |
| `HOST_UID` / `HOST_GID` | 让容器以你的宿主用户身份运行，从而写绑定挂载的 `./data` 而无需 `chown`（`id -u` / `id -g`；Linux 首个用户常为 `1000`，macOS 默认用户常为 `501`）。 |
| `TZ` | 容器时区（镜像默认 UTC）。设为你的 IANA 时区（如 `Asia/Shanghai`）让 `get_current_time` 与日志时间戳对齐你的墙钟。仅影响 Docker；本地 `uv run` 直接用宿主时区。 |

## 校验规则

- 上下文预算（`MAX_CONTEXT_ESTIMATED_TOKENS` / `CONTEXT_IMAGE_ESTIMATED_TOKENS`）与记忆预算（`MAX_MEMORIES_PER_SCOPE` / `MAX_MEMORY_CHARS` / `MAX_RETRIEVED_MEMORIES` / `MAX_MEMORY_ESTIMATED_TOKENS`）都按**正整数**（`>= 1`）校验，零/负数/非整数会抛 `ConfigError`。
- 唯一的跨项不变量是 `MAX_MEMORY_ESTIMATED_TOKENS <= MAX_CONTEXT_ESTIMATED_TOKENS`，违反会抛 `ConfigError`。
- `TOOL_APPROVAL_TIMEOUT_SECONDS` / `TOOL_TIMEOUT_SECONDS` 必须为正数，否则 `ConfigError`。
- MCP 数值项：`MCP_CONNECT_TIMEOUT_SECONDS` 必须为正数、`MAX_MCP_TOOL_RESULT_CHARS` 必须 `>= 1`，否则 `ConfigError`。`MCP_SERVERS` 的**结构**在 `load_config` 里强校验（非法 JSON、非数组、非对象条目、未知字段、坏 name/url、重复 name、`bearer_token_env` 名非法或对应环境变量缺失/为空，都 `ConfigError`）——报错只点名服务器与字段，**从不**回显 token 值或完整 URL。

## System Prompt

默认读取 `config/system_prompt.txt`（文件优先）。也可以用环境变量 `SYSTEM_PROMPT` 覆盖文件（设置后忽略文件）。若两者都没有，使用一个内置兜底 prompt。优先级：`SYSTEM_PROMPT`（env）> `SYSTEM_PROMPT_PATH`（文件）> 内置兜底。

编辑 `config/system_prompt.txt` 即可调整 Agent 的语气与边界，无需改代码。
