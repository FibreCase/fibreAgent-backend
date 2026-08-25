# Troubleshooting

| 现象 | 排查 |
| --- | --- |
| 启动报 `ConfigError: TELEGRAM_BOT_TOKEN is not set` 等 | `.env` 没加载或变量名为空。确认 `cp .env.example .env` 且填了值；确认在**仓库根目录**运行（`.env` 从当前工作目录读取）。 |
| 启动报 `ConfigError`（工具/预算/超时/MCP 相关） | 某个安全/预算/MCP 配置不合法：`TOOL_PERMISSION_OVERRIDES` 条目缺 `=`、空工具名、空/非法权限值、重复工具名、工具名含 `[A-Za-z0-9_-]` 之外字符，某个预算/超时为 0/负数/非整数，或 `MCP_SERVERS` 结构非法（非数组、坏 name/url、重复 name、`http://` 未开 `MCP_ALLOW_INSECURE_HTTP`、`bearer_token_env` 指向的变量缺失/为空）。修好再启动——**坏掉的配置不会被静默忽略**（MCP 报错只点名服务器与字段，不回显 token/完整 URL）。 |
| Bot 完全不回复 | 确认 `TELEGRAM_ALLOWED_USER_IDS` 里确实有**你的** id（@userinfobot 查）；确认你发的是普通消息或 `/` 命令；看日志是否有 `unauthorized telegram user attempted access`。 |
| `模型请求超时，请稍后重试。` | LLM 请求超时或网络问题。查看服务端日志的 `llm request timed out` / `llm http error status=...`。可临时调大 `OPENAI_TIMEOUT`。 |
| `模型服务暂时不可用。` | LLM 返回了 HTTP 错误 / 空回复 / 连接失败。看日志里的 HTTP status；检查 `OPENAI_BASE_URL` 是否写错（常见错误是填了完整 `.../v1/chat/completions`）。 |
| 请求打到 `404` / 错误路径 | 几乎肯定是 `OPENAI_BASE_URL` 多写了 `/chat/completions`。应只填前缀（如 `.../v1`）。 |
| 工具调用一直没结果 / 卡在审批 | `ask` 工具在等你的 `Approve`/`Deny`；超过 `TOOL_APPROVAL_TIMEOUT_SECONDS` 会按「审批已过期」处理（不执行）。工具执行超过 `TOOL_TIMEOUT_SECONDS` 会按「工具超时」处理。用 `/tool_audit` 查最近一次工具的 `event_type`/`code`。MCP 工具默认也是 `ask`，同理。 |
| `/mcp_status` 显示某台 `unavailable` / `MCP: disabled` | `disabled` = 未配置 `MCP_SERVERS` 或 `ENABLE_TOOLS=false`。`unavailable` = 那台服务器在启动时连接/初始化/列举失败（或发现到非法工具被整台丢弃）——**其余服务器与内置工具不受影响**，bot 照常运行。查日志里的服务器名 + 稳定码（`mcp_connect_failed` / `mcp_initialize_failed` / `mcp_discovery_failed` / `mcp_invalid_tool`）。本阶段**不自动重连**：改好端点/token 后**重启进程**才会重新发现。 |
| 想确认持久化 | 对话历史写在 `data/agent.db`；用 [database.md](database.md) 的查询示例查看 `conversations`/`messages`，重启 backend 后数据仍在。 |

> 日志中**不会**出现 Telegram token、OpenAI API key、完整 Authorization header、服务器路径，也不默认记录完整消息内容（只记 conversation_id、message_id、长度、延迟）。工具/审批/审计路径**绝不**记录工具参数、结果、异常正文、图片、或你的原始 user id（只记不可逆 scope 哈希 + 工具名 + 稳定码）。
