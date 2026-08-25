# 远程 MCP 工具（Streamable HTTP）

在**启动时**连到运维配置的远程 Model Context Protocol（MCP）服务器（**仅 Streamable HTTP**），用 `tools/list` 发现其工具，并把每个远程工具包装成一个标准的 `Tool`——`mcp_<server>__<remote>` 命名、默认 `ask`——注册进**同一个** registry。

**MCP 只是又一个 Tool Provider**：它**完全复用 Phase 3 的执行边界**（策略 → JSON Schema 校验 → 需要时的一次性 Telegram 审批 → 单工具超时 → 只记元数据的审计），**没有**第二条执行路径。换句话说，MCP 不改动 Agent 运行时、不改动 service / LLM client / Telegram 层——它只是往 registry 里多塞了一批普通工具。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/mcp_status` | 只读查看已配置服务器的状态：每台的名称、`available`/`unavailable`、发现到的工具数，以及可用工具总数。**不**发起连接 / 刷新，也不调用 LLM 或 MCP；未配置或 `ENABLE_TOOLS=false` 时显示「MCP: disabled」；**绝不**显示 URL / host / token / 头 / 工具描述 / schema / 服务器 instructions / 失败细节。 |

## 启动发现（`mcp/manager.py`）

`McpManager` 只在 `ENABLE_TOOLS=true` **且**至少配置了一台服务器时才被构造（否则不存在、永不发起任何 MCP 网络连接）。它在 `_post_init`（DB 初始化之后）里 `start()`：对每台服务器按顺序

```
建 http client(bearer 头) → Streamable HTTP 传输 → ClientSession
   → initialize()   [wait_for 超时]
   → tools/list()   [wait_for 超时]
   → 逐个包装成 McpTool（原子校验）
```

然后把这些工具 `add` 进 registry（在内置工具**之后**，因此内置仍排前）。工具 loop 每条消息都从 `registry.names()` 重新派生 advertised schema、且策略每次调用都重新解析，所以启动时新加入的 MCP 工具**自动**被通告、被门控——无需重建策略。

- **故障隔离**：一台服务器连接 / 初始化 / 列举失败，只被标记为 unavailable（带一个稳定码）并跳过；**其余服务器与内置工具照常启动**。bot **绝不**因一个可选 MCP 服务器宕机而启动失败。
- **原子发现**：某台服务器的工具**要么全要、要么全不要**——其中任一工具名非法、`input_schema` 非法、或与已注册名 / 自身兄弟重名，则**整台不注册**（`mcp_invalid_tool`）。跨服务器 / 内置重名因命名空间形式在结构上不可能，但代码仍防御性检查。
- **不重连**：健康的会话之后若中途断开，`call_tool` 会抛错、被映射成 `mcp_unavailable`；下次**进程启动**才重新发现。本阶段不做自动重连。

## 命名与工具（`mcp/wrapper.py`）

- `local_tool_name(server, remote)` → `mcp_<server>__<remote>`。`mcp_` 前缀 + `__` 分隔让两段无歧义，且因服务器名与远程名都取自 `[A-Za-z0-9_-]`，本地名本身也是 registry / 策略 / 审计都能接受的合法 `[A-Za-z0-9_-]+` 工具名。远程名段上限 90 字符，保证本地名不超 `tool_name` 列（`String(128)`）。
- `McpTool` 是一等 `Tool`：`default_permission = ask`（**无条件**——远程工具绝不因远端声称只读就自动放行；主人仍可用 `TOOL_PERMISSION_OVERRIDES` 按**命名空间本地名**把某个 pin 成 `allow`/`deny`）。
- `parameters` 是远端 `input_schema` 原样映射（默认 `{"type":"object","properties":{}}`），因此会被**既有** registry 门在发任何网络请求**之前** schema 校验。
- `description` 是固定的「Remote tool '<remote>' from the configured MCP server '<server>'」前缀 + 远端描述；**服务器 instructions 永不**出现在这里。
- `approval_summary` 固定、**不回显参数**（`<本地名> — 参数不显示`）。

## 执行与结果映射

`execute()` 只做一件事：把 `arguments` 转发给已连接 session 的 `call_tool`，并把响应映射成一个**有界、不回显**的字符串。它**不**自己做鉴权 / 审批 / 参数校验 / 超时 / 审计——那些都在 Phase 3 的工具 loop 里，对每个注册工具（MCP 或内置）一视同仁。

- 多段**文本** content 按原顺序用换行拼接（下一轮 LLM 看到的就是它）。
- 其余结果一律映射成稳定的**不回显**码：
  - `mcp_unavailable` — 传输 / 协议 / session 异常（日志只记**异常类名**与工具名，**从不**记异常正文，因其可能含端点或 token）。
  - `mcp_tool_error` — 远端返回 `is_error` 为真。
  - `mcp_unsupported_result` — 无 `is_error` 字段 / 非文本块 / 空结果。
  - `mcp_result_too_large` — 文本总长超 `MAX_MCP_TOOL_RESULT_CHARS`（**不截断、不回显**前缀字节）。
- 绝不抛裸的远端异常；即便抛了，loop 也有 `tool_execution_failed` 兜底。

## 安全边界

- **端点与 token 只来自严格启动配置**：`MCP_SERVERS`（JSON 数组，`name`/`url`/`bearer_token_env`）、`MCP_CONNECT_TIMEOUT_SECONDS`、`MAX_MCP_TOOL_RESULT_CHARS`、`MCP_ALLOW_INSECURE_HTTP`。`bearer_token_env` 是**环境变量名**（token 值在**建 client 时**从 env 读作 `Authorization: Bearer` 头，**从不**存到 config、**从不**进日志）。端点 / token **永不**受模型、聊天输入、记忆或工具参数控制。
- **URL 强校验**（启动期，`ConfigError`）：绝对 `https://` + host、无 userinfo / fragment / query；`http://` 默认拒绝（`MCP_ALLOW_INSECURE_HTTP=true` 才放行）。报错只点名服务器与字段，**从不**回显 token 值或完整 URL。
- **日志 / 审计 / `/mcp_status` 永不**泄露：端点 URL / host、`Authorization` 头或 token、工具参数、工具结果、异常正文、服务器 instructions、原始 scope / user id、图片 / base64。启动失败只记**服务器名 + 稳定码**（异常只记**类名**）。审计沿用 Phase 3：只存 `scope_hash` + 工具名 + 事件 + 稳定码 +（可选）耗时。
- **审批仍是回调、不是工具**：MCP 工具的 `ask` 审批走同一个 Telegram 回调 broker——模型无法用文本给自己批准。

## 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MCP_SERVERS` | 空 | JSON **数组**；空 = 不建 MCP 客户端、永不发起 MCP 网络连接。对象字段 `{ "name", "url", "bearer_token_env"? }`，见上。任何违规是启动期 `ConfigError`。 |
| `MCP_CONNECT_TIMEOUT_SECONDS` | `10` | 每台服务器握手（连接/initialize/tools-list）超时秒数；超时 → 该服务器 unavailable，其余照常。必须 `> 0`。 |
| `MAX_MCP_TOOL_RESULT_CHARS` | `10000` | 单个远程工具结果回传给模型的文本硬上限；超大 → `mcp_result_too_large`（不截断、不回显）。必须 `>= 1`。 |
| `MCP_ALLOW_INSECURE_HTTP` | `false` | 硬开关允许 `http://`（明文）端点；默认仅 `https`。仅用于你控制的本地/内网可信端点。 |

两个数值项在 `Config.__post_init__` 校验（`> 0` / `>= 1`），`MCP_SERVERS` 的结构在 `load_config` 里强校验。

## 限制

- **仅远程 Streamable HTTP 发现 + 调用**：只发现远程工具并转发调用；**不**做 stdio / subprocess / 本地起服务、**不**自动重连、**不**支持 resources / prompts / sampling / OAuth。
- **只取文本结果**：非文本（image / audio / resource / tool-use / structured）、空、超大、异常都回稳定码、**不回显**。结果受 `MAX_MCP_TOOL_RESULT_CHARS` 硬上限约束。
- **默认 `ask`**：远程工具默认走一次性审批（安全）；确认某远程工具确属只读无害，才可用 `TOOL_PERMISSION_OVERRIDES` 按命名空间名 pin 成 `allow`。
- **无运行时重发现**：工具集在启动时确定；服务器新增/删除工具需重启进程才反映。
- **不持久化 MCP 调用 transcript**：同 Phase 3，只持久化 user + 最终 assistant 轮；MCP 调用的元数据审计走 `tool_audit_events`（`/tool_audit` 可查）。
