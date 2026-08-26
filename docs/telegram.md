# Telegram 接入与命令

## 创建 Telegram Bot

1. 在 Telegram 里搜索 **@BotFather** 并发送 `/start`。
2. 发送 `/newbot`，按提示给 bot 起名字和用户名（用户名必须以 `bot` 结尾）。
3. BotFather 会返回一段 **Bot Token**（形如 `123456789:AA...`）。把它填到 `.env` 的 `TELEGRAM_BOT_TOKEN`。
4. 获取**你自己的 Telegram user id**：
   - 给 **@userinfobot** 发任意消息，它会回复你的 `id`。
   - 把它填到 `TELEGRAM_ALLOWED_USER_IDS`。
5. **必须先用 @BotFather 把 bot 设为 privacy 模式？不需要**——本项目只处理你主动发给它的消息，默认即可。

> 注意：bot 只能收到「你主动发给它」或「以 `/` 开头的命令」消息。首次使用请先发 `/start`。

## 启动

```bash
# 确保在仓库根目录，且 .venv 已 uv sync
uv run python -m fibrecase_agent_backend
# 或等价地
uv run fibrecase-agent-backend
```

> 从**仓库根目录**运行，这样它才找得到 `.env` 与 `config/system_prompt.txt`（路径相对工作目录）。

启动后：

- 自动创建 `data/agent.db`（如不存在）。
- 开始 Telegram long polling（无需公网入站/无需 webhook）。
- 看到日志 `telegram long polling started` 即表示就绪。
- 用 `Ctrl+C` 停止；会优雅关闭 LLM 客户端与数据库连接。

> long polling 的原因：你的服务器可能没有公网 HTTP 入站能力，long polling 只出站连接 Telegram。**唯一例外**：启用 MCP 用户级 OAuth 后，回调服务器会在 `OAUTH_CALLBACK_PORT`（默认 8090）上监听 `GET /oauth/callback`——该地址必须能被 OAuth provider（如 Google）访问（见 [远程 MCP 工具](mcp.md)）。不启用 OAuth 时没有任何入站监听。

## 命令参考

Bot 支持以下命令（输入 `/` 会弹出 Telegram 原生命令菜单，或发 `/help` 查看）：

| 命令 | 作用 |
| --- | --- |
| `/start` | 启动 Agent / 查看当前会话（无会话时自动创建） |
| `/new` | 开始新会话，清空本 chat 的历史上下文（**不影响**已保存的长期记忆） |
| `/context` | 查看上下文窗口状态：消息上限、已存入/本次保留条数、估算 token 预算的占用与剩余、历史图片保留/降级数量（只读预览，估算非精确 token） |
| `/remember <内容>` | 保存一条长期记忆到你的账号（跨 `/new` 与重启保留）；回显记忆 ID |
| `/memories` | 列出你保存的所有记忆（ID + 保存时间 + 内容） |
| `/forget <id>` | 删除指定 ID 的记忆；ID 不存在或不属于你时返回「未找到」 |
| `/forget all CONFIRM` | 清空你账号下的**全部**记忆（破坏性操作，必须带 `CONFIRM` 才会执行） |
| `/status` | 查看运行状态（版本、模型、会话 id、消息数） |
| `/tool_audit [limit]` | 查看**你本人**最近的工具执行审计（`limit` 默认 `20`、上限 `50`）：每条只显示时间、事件 id、工具名、事件类型、结果码与（若有）耗时；**绝不**显示工具参数、结果或异常正文。仅当前账号可见（按不可逆的 scope 哈希隔离），无数据时给出安全提示。 |
| `/mcp` | 只读查看 MCP 服务器状态（名称、`available`/`unavailable`、工具数、总数），**并**对 OAuth 服务器显示**你本人**的登录状态（已连接 / 需要认证 / 已过期 / 未配置）；别的用户的登录状态永不显示。**不**发起连接 / 刷新，也不调用 LLM / MCP；**绝不**显示 URL / host / token / 工具描述。 |
| `/mcp auth <server>` | 为**你的账号**发起第三方 OAuth 登录（如 `/mcp auth gcal`）：返回一个**内联 URL 登录按钮**（点一下即跳转，无需复制 URL）+ 一条有效期提示。凭据绑定到你的 Telegram user（不是会话），自动刷新；`/new` 与重启不影响。需要 `OAUTH_CALLBACK_BASE_URL` 与相应 provider 凭据已配置，否则给出安全提示。 |
| `/mcp_status` | 只读查看已配置的远程 MCP 服务器状态：每台的名称、`available`/`unavailable`、发现到的工具数，以及可用工具总数。**不**发起连接/刷新，也不调用 LLM 或 MCP；未配置（或 `ENABLE_TOOLS=false`）时显示「MCP: disabled」；**绝不**显示 URL/host/token/头/工具描述或服务器 instructions。 |
| `/help` | 列出本帮助 |

其它任何文字消息都会作为对话发给 Agent。
