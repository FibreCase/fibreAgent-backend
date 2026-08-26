# 数据库（SQLite）

- 文件：默认 `./data/agent.db`（由 `DATABASE_URL` 决定）。
- 启动时自动初始化（`CREATE TABLE IF NOT EXISTS` / `Base.metadata.create_all`），可安全重复启动。**升级不丢数据**：新版本表（`attachments`、`memories`、`tool_audit_events`、`oauth_credentials`、`oauth_authorization_states`）会在已有库上补建，无需手动 wipe、无需 Alembic。
- **一个 Telegram chat 对应一个 conversation**，`/new` 只影响该 chat，**不触碰 `memories`，也不触碰 OAuth 凭据 / 授权 state**（记忆按 `scope` 归属账号，OAuth 凭据按 Telegram user 归属，二者都与会话无关）。

## 表结构

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
| `role` | `system` / `user` / `assistant`（schema 已允许 `tool`，为工具调用预留；当前只写 user/assistant，工具中间轮**不落库**） |
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

**memories**

| 字段 | 说明 |
| --- | --- |
| `id` | 自增主键（用户可见，用于 `/forget <id>`） |
| `scope` | 账号隔离键（当前为 `telegram:<user_id>`，由适配器构造；带索引）。所有按 id 的读/删都在 SQL 里同时按 `scope + id` 过滤 |
| `content` | 记忆原文（**逐字**存储；用户显式保存的内容，可含指令性文字——注入时由固定 wrapper 标注为「参考、非指令」） |
| `normalized_content` | 检索用的规范化形式（小写化、去首尾、折叠空白），由后端计算 |
| `created_at` | 保存时间 |
| `updated_at` | 最后修改时间（检索平分时较新者优先） |
| `last_retrieved_at` | 最近一次**真正被注入**上下文的时间（未注入则保持为空） |

**tool_audit_events**（append-only 审计）

| 字段 | 说明 |
| --- | --- |
| `id` | 自增主键（事件 id，用户可见） |
| `scope_hash` | 发起者的**不可逆** scope 哈希（从不存原始 scope / user id） |
| `tool_name` | 被调用的工具名 |
| `event_type` | 事件类型，如 `requested` / `denied` / `validation_failed` / `approval_requested` / `approval_approved` / `approval_denied` / `approval_expired` / `started` / `completed` / `timed_out` / `failed` / `audit_unavailable` |
| `code` | 稳定的结果码，如 `ok` / `unknown_tool` / `tool_denied` / `invalid_arguments` / `approval_denied` / `approval_expired` / `tool_timeout` / `tool_execution_failed` / `audit_unavailable`（可空） |
| `latency_ms` | 工具耗时（无则为空） |
| `created_at` | 时间戳 |

> 审计**只**存 `scope_hash` + 工具名 + 事件 + 结果码 +（若有）耗时——**绝不**存工具参数、结果、异常正文、图片或任何密钥。用 `/tool_audit [limit]` 查看（按 `scope_hash` 隔离，仅本人可见）。

**oauth_credentials**（MCP 用户级 OAuth，phase 4.x）

| 字段 | 说明 |
| --- | --- |
| `id` | 自增主键 |
| `telegram_user_id` | 凭据归属的 Telegram user id（**唯一键的组成**，带索引）——凭据按**用户**绑定，不按 chat / 会话 |
| `provider` | OAuth provider 名（如 `google`，`String(32)`） |
| `mcp_server` | 该凭据服务的 MCP 服务器名（`String(64)`） |
| `access_token` | OAuth access token（**敏感**，只在库内，永不进日志/命令/异常文案） |
| `refresh_token` | refresh token（可空；**敏感**，同上） |
| `expires_at` | access token 过期时间（可空 = provider 未声明过期） |
| `scopes` | 授予的 scope 列表（可空） |
| `created_at` / `updated_at` | 时间戳 |

> 唯一约束 `(telegram_user_id, provider, mcp_server)`——**每用户每服务器一条活跃凭据**；重新登录是 **upsert**（覆盖，不新增行）。跨用户隔离在 SQL 层：查别的 user id 等价于「不存在」。**`/new`（reset_conversation）绝不触碰本表**；重启后凭据仍在（SQLite 文件持久化）。

**oauth_authorization_states**（OAuth 授权中的 state，phase 4.x）

| 字段 | 说明 |
| --- | --- |
| `id` | 自增主键 |
| `state` | `secrets.token_urlsafe(32)` 生成的不透明 state（**唯一**） |
| `telegram_user_id` | 发起登录的 Telegram user id（带索引） |
| `chat_id` | 发起登录的 chat（登录结果通知回这里） |
| `provider` / `mcp_server` | 本次授权针对的 provider 与 MCP 服务器 |
| `expires_at` | state 过期时间（`OAUTH_STATE_TTL_SECONDS`，默认 600s） |
| `created_at` | 时间戳 |

> 每行**单次使用**：回调处理时「查 + 删」在同一事务内完成（重放无效），过期 / 未知 / 缺 `code` 一律作废。目标 (user, provider, server) **只**取自本表记录——回调 query 里伪造的参数无法改变凭据的归属。`/new` 同样**不触碰**本表。

## 直接查看（可选）

```bash
uv run python -c "import sqlite3;print(sqlite3.connect('data/agent.db').execute('select count(*) from messages').fetchone())"
```
