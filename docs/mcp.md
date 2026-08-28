# MCP 工具（Streamable HTTP + stdio）

在**启动时**连到运维配置的 Model Context Protocol（MCP）服务器——**远程 Streamable HTTP** 端点，或**本地 stdio 进程**（由后端 spawn 一个子进程、走它的 stdin/stdout）——用 `tools/list` 发现其工具，并把每个远程工具包装成一个标准的 `Tool`——`mcp_<server>__<remote>` 命名、默认 `ask`——注册进**同一个** registry。

**MCP 只是又一个 Tool Provider**：它**完全复用 Phase 3 的执行边界**（策略 → JSON Schema 校验 → 需要时的一次性 Telegram 审批 → 单工具超时 → 只记元数据的审计），**没有**第二条执行路径。换句话说，MCP 不改动 Agent 运行时、不改动 service / LLM client / Telegram 层——它只是往 registry 里多塞了一批普通工具。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/mcp_status` | 只读查看已配置服务器的状态：每台的名称、`available`/`unavailable`、发现到的工具数，以及可用工具总数。**不**发起连接 / 刷新，也不调用 LLM 或 MCP；未配置或 `ENABLE_TOOLS=false` 时显示「MCP: disabled」；**绝不**显示 URL / host / token / 头 / 工具描述 / schema / 服务器 instructions / 失败细节。 |
| `/mcp` | 只读查看 MCP 服务器状态（同 `/mcp_status`），**并且**对 OAuth 服务器显示**你本人**的登录状态（connected / 需要认证 / 未配置 / 已过期等）；别的用户的登录状态**永不**显示。 |
| `/mcp auth <server>` | 为**你的账号**发起第三方 OAuth 登录：返回一个**内联 URL 按钮**（点一下即跳登录页，**从不**让你复制 URL）+ 一条有效期提示。发起的是 authorization-code flow（`state` 单次使用、带 TTL）。 |

## 用户级 OAuth（phase 4.x）

给 MCP 服务器配上**用户级 OAuth** 后（`MCP_SERVERS` 条目里 `"authentication": {"type": "oauth", "provider": "google"}`，与 `bearer_token_env` **互斥**），凭据按 **Telegram user** 绑定，而不是会话 / chat：

- **登录流**：`/mcp auth gcal` → `OAuthManager.initiate` 生成 `state`（`secrets.token_urlsafe(32)`，存库、绑定 (user, chat, provider, server)、TTL 默认 600s）→ 把 provider 授权 URL 以**内联按钮**发出 → provider 302 回 `GET <OAUTH_CALLBACK_BASE_URL>/oauth/callback?state=…&code=…` → 回调服务器（starlette 应用，跑在 PTB 自己的事件循环里，`OAUTH_CALLBACK_PORT` 默认 8090）**消费 state（单次、原子）→ 换 token → 凭据落库 → Telegram 通知结果**（成功 / 拒绝 / 无效 / 过期，都是固定文案）。
- **state 安全**：`state` 单次使用（消费即删，重放无效）、过期作废、未知作废、缺 `code` 作废；**目标 (user, provider, server) 来自库里的 pending 记录**——伪造 query 参数无法把凭据绑到别的用户 / provider / 服务器（spec §28 wrong-user / wrong-provider / wrong-server）。
- **凭据**：`oauth_credentials` 表，唯一键 `(telegram_user_id, provider, mcp_server)`——**每用户每服务器一条活跃凭据**，重新登录是 upsert 不是新增。跨用户隔离：别人的凭据对查找不可区分于「不存在」。`/new`（`reset_conversation`）**绝不**触碰凭据或 pending state；**重启**后凭据仍在（SQLite 持久化）。
- **自动刷新**：MCP 工具执行时由 `McpOAuthAuth`（`httpx2.Auth`，经 contextvar 拿到当前 principal）向 manager 要一个有效 access token：未过期直接用；临近/已过期则用 refresh token 刷新——provider 返回新 refresh token 就**持久化轮换后的**，不返回就**保留旧的**；**刷新失败不删凭据**（状态显示「已过期，重新登录」，重登是唯一恢复路径）。并发刷新有 per-(user, server) 锁，只刷一次。
- **与 MCP client 的最小集成点**（**仅 http 传输**）：oauth 类型服务器建 http client 时注入 `auth=McpOAuthAuth`；工具 loop 在 `tool.execute()` 周围 set/reset `active_principal`（`telegram:<user_id>`），所以**同一个** registry / gate / 审批 / 审计对 OAuth 服务器与 bearer 服务器一视同仁。启动握手（无 principal）不发 Authorization 头。现有 bearer / 无鉴权服务器完全不受影响。（stdio 服务器没有 HTTP 请求可带头，因此**不支持** bearer / OAuth——需要的凭据放在子进程的 `env` 里。）
- **回调服务器**：只有 `GET /oauth/callback` 一条路由 + 其余固定 404；**不**监听在 PTB 之外——它作为任务跑在 long polling 的同一个事件循环里，随 bot 启停。日志**绝不**记 access_token / refresh_token / authorization code / client_secret / 完整回调 URL（含 query）。
- **provider 无关**：`mcp/auth/provider.py` 是 `OAuthProvider` 抽象（`authorization_url` / `exchange_code` / `refresh_token`）；`main.py` 的 `_build_provider` 是**全代码库唯一**按 provider 名分派的地方（`google` 读 `GOOGLE_OAUTH_CLIENT_ID/SECRET/SCOPES`，仅在**该处**从 env 读、从不存到 config）。加新 provider = 实现 ABC + 在该处加一个分支，manager / 存储 / 回调 / 命令零改动。

## 启动发现（`mcp/manager.py`）

`McpManager` 只在 `ENABLE_TOOLS=true` **且**至少配置了一台服务器时才被构造（否则不存在、永不发起任何 MCP 网络连接 / 子进程）。它在 `_post_init`（DB 初始化之后）里 `start()`：对每台服务器按顺序（**按 transport 分派**，两者产出同一种 `(read, write)` 流，后面的 session/初始化/发现完全共用）

```
http:  建 http client(bearer 头) → Streamable HTTP 传输 ┐
stdio: spawn 子进程(command/args/env/cwd)  → stdio 传输   ┤ → ClientSession
                                                         │    → initialize()   [wait_for 超时]
                                                         ┘    → tools/list()   [wait_for 超时]
                                                                  → 逐个包装成 McpTool（原子校验）
```

然后把这些工具 `add` 进 registry（在内置工具**之后**，因此内置仍排前）。工具 loop 每条消息都从 `registry.names()` 重新派生 advertised schema、且策略每次调用都重新解析，所以启动时新加入的 MCP 工具**自动**被通告、被门控——无需重建策略。

- **故障隔离**：一台服务器连接（stdio 的 spawn 失败也算）/ 初始化 / 列举失败，只被标记为 unavailable（带一个稳定码）并跳过；**其余服务器与内置工具照常启动**。bot **绝不**因一个可选 MCP 服务器（或一个起不来的 stdio 子进程）而启动失败。stdio 子进程的生命周期由 SDK 的 `stdio_client` 上下文管理收尾（关 stdin → 等退出 → 必要时按进程组 SIGTERM/SIGKILL），manager 只需按现有方式 unwind 它的 `AsyncExitStack`。
- **原子发现**：某台服务器的工具**要么全要、要么全不要**——其中任一工具名非法、`input_schema` 非法、或与已注册名 / 自身兄弟重名，则**整台不注册**（`mcp_invalid_tool`）。跨服务器 / 内置重名因命名空间形式在结构上不可能，但代码仍防御性检查。
- **不重连**：健康的会话之后若中途断开（stdio 子进程中途退出也算），`call_tool` 会抛错、被映射成 `mcp_unavailable`；下次**进程启动**才重新发现。本阶段不做自动重连。

## 命名与工具（`mcp/wrapper.py`）

- `local_tool_name(server, remote)` → `mcp_<server>__<remote>`。`mcp_` 前缀 + `__` 分隔让两段无歧义，且因服务器名与远程名都取自 `[A-Za-z0-9_-]`，本地名本身也是 registry / 策略 / 审计都能接受的合法 `[A-Za-z0-9_-]+` 工具名。远程名段上限 90 字符，保证本地名不超 `tool_name` 列（`String(128)`）。
- `McpTool` 是一等 `Tool`：`default_permission = ask`（**无条件**——远程工具绝不因远端声称只读就自动放行；主人仍可在 `MCP_PERMISSIONS_FILE` 里按**命名空间本地名**把某个 pin 成 `allow`/`deny`）。
- `parameters` 是远端 `input_schema` 原样映射（默认 `{"type":"object","properties":{}}`），因此会被**既有** registry 门在发任何网络请求**之前** schema 校验。
- `description` 是固定的「`(🌐Remote)`」标记前缀 + 远端描述；**服务器 instructions 永不**出现在这里（也不含服务器名 / 远程名——那两段已在本地工具名 `mcp_<server>__<remote>` 里）。
- `approval_summary` 只返回该工具的 `description`（**用途**——即上面那条前缀 + 远端描述），**绝不回显（远端）参数**。审批卡片在 `What it does:` 一行展示它；（若这次调用有参数）卡片另有一段 `Arguments:`，把**已 schema 校验**的参数以易读 JSON 展示在 `<pre><code>` 里（无参数则整段省略）。参数**只**出现在这张给主人看的审批卡片上——**从不**写入日志、审计表，或面向模型的回退文案。

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

- **端点 / 进程与 token 只来自严格启动配置**：`MCP_SERVERS`（JSON 数组，`name` + `transport` + http 的 `url`/`bearer_token_env`/`authentication` 或 stdio 的 `command`/`args`/`env`/`cwd`）、`MCP_CONNECT_TIMEOUT_SECONDS`、`MAX_MCP_TOOL_RESULT_CHARS`、`MCP_ALLOW_INSECURE_HTTP`。http 的 `bearer_token_env` 是**环境变量名**（token 值在**建 client 时**从 env 读作 `Authorization: Bearer` 头，**从不**存到 config、**从不**进日志）。端点 / token / 子进程参数**永不**受模型、聊天输入、记忆或工具参数控制。
- **传输字段强校验**（启动期，`ConfigError`）：
  - http：绝对 `https://` + host、无 userinfo / fragment / query；`http://` 默认拒绝（`MCP_ALLOW_INSECURE_HTTP=true` 才放行）。
  - stdio：`command` 必填（可执行名或路径，字母/数字/`_ . / -`，无空白与元字符——**不**经 shell 执行，故不做 glob / `$VAR` 展开）；`args` 为字符串数组（**原样**传子进程）；`env` 为「合法环境变量名 → 非空字符串」对象；`cwd` 为路径。
  - 互斥：http 条目**不得**带 `command`/`args`/`env`/`cwd`；stdio 条目**不得**带 `url` / `bearer_token_env` / `authentication`（spawn 出来的进程没有请求可带头，凭据放在其 `env`）。
  - 报错只点名服务器与字段，**从不**回显 token 值或完整 URL；stdio 的 `env` 校验报错只点名**键**，不回显值。
- **日志 / 审计 / `/mcp_status` 永不**泄露：端点 URL / host、`Authorization` 头或 token、**stdio 的 command / args / env / cwd**、工具参数、工具结果、异常正文、服务器 instructions、原始 scope / user id、图片 / base64。启动失败只记**服务器名 + 稳定码**（异常只记**类名**）。审计沿用 Phase 3：只存 `scope_hash` + 工具名 + 事件 + 稳定码 +（可选）耗时。
- **审批仍是回调、不是工具**：MCP 工具的 `ask` 审批走同一个 Telegram 回调 broker——模型无法用文本给自己批准。
- **OAuth 凭据永不**出现在日志 / 审计 / 命令输出 / 异常文案里：access_token、refresh_token、authorization code、client_secret、`Authorization` 头、完整回调 URL（含 query）都不行。回调错误是**固定文案**，不带 provider 错误正文（可能含 token 或端点）。凭据表只存 token 本身与元数据，`/mcp` 只显示状态分类（connected / 需要认证 / 已过期 / 未配置），**不**显示 token、scope 原文或 provider 细节。

## 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MCP_SERVERS` | 空 | JSON **数组**（**或**从 `MCP_SERVERS_FILE` 文件读取，见下）；空 = 不建 MCP 客户端、永不发起 MCP 连接 / 子进程。对象字段 `{ "name", "transport"? , <http: "url", "bearer_token_env"? , "authentication"? > 或 <stdio: "command", "args"? , "env"? , "cwd"? > }`，见上；`authentication` 与 `bearer_token_env` 互斥（http 专属），stdio 不得带 `url`/鉴权字段。任何违规是启动期 `ConfigError`。 |
| `MCP_SERVERS_FILE` | 空 | **多服务器 / stdio 的推荐写法**：把同一份 JSON **数组**放进单独文件（如 `config/mcp_servers.json`），这里填路径（相对工作目录）。**设置时优先于内联 `MCP_SERVERS`**（内联被忽略）。文件必须是非空 JSON 数组（字段/规则与 `MCP_SERVERS` 完全一致）；设置了却**不存在 / 不可读 / 为空（0 字节或空白）** → 启动期 `ConfigError`（点名路径，绝不静默禁用服务器；文件里显式 `[]` 表示无服务器）。 |
| `MCP_PERMISSIONS_FILE` | 空 | 专用 MCP 工具权限文件（CWD 相对路径的 JSON **数组**，每项 `{ "tool": "mcp_<server>__<remote>", "permission": "allow\|ask\|deny\|"" }`）。**仅列 MCP 工具**——内置工具恒按其声明默认值运行、不在此文件中。由**后端维护**：启动时重同步到当前 MCP 工具集（新工具出现为未填 `""`＝默认；**已填写**的条目永远保留，即使该工具后来消失；**未填写**的消失工具条目被删去），并**热加载**（改动在下次工具调用即生效，无需重启）。`""`（或缺省）＝用工具默认值。设置了却不存在 / 为空＝无覆盖（全部 MCP 工具默认 `ask`），非错误；设置了却**存在但损坏**（非数组 / 坏条目 / 未知字段 / 非法工具名或权限 / 重复工具名）→ 启动期 `ConfigError`。 |
| `MCP_CONNECT_TIMEOUT_SECONDS` | `10` | 每台服务器握手（连接/initialize/tools-list）超时秒数；超时 → 该服务器 unavailable，其余照常。必须 `> 0`。 |
| `MAX_MCP_TOOL_RESULT_CHARS` | `10000` | 单个远程工具结果回传给模型的文本硬上限；超大 → `mcp_result_too_large`（不截断、不回显）。必须 `>= 1`。 |
| `MCP_ALLOW_INSECURE_HTTP` | `false` | 硬开关允许 `http://`（明文）**http** 端点；默认仅 `https`。仅用于你控制的本地/内网可信端点。（stdio 无 URL，不受此项影响。） |
| `OAUTH_CALLBACK_BASE_URL` | 空 | 空 = OAuth 整体关闭（不建 provider、不起回调服务器）。非空必须是**裸 origin**：绝对 `http(s)://` + host、无 userinfo / path / query / fragment / 末尾斜杠，否则启动期 `ConfigError`。 |
| `OAUTH_CALLBACK_PORT` | `8090` | 回调 HTTP 服务器监听端口（仅 OAuth 配置时启动）。`1..65535`。 |
| `OAUTH_STATE_TTL_SECONDS` | `600` | 授权 state 存活秒数，过期后回调作废。必须 `> 0`。 |
| `GOOGLE_OAUTH_CLIENT_ID` | 空 | google provider 的 client id。**只在** `main.py` 的 provider 注册处从 env 读（该处是唯一按 provider 名分派的地方），不存 config、不进日志。 |
| `GOOGLE_OAUTH_CLIENT_SECRET` | 空 | google provider 的 client secret，同上，env-only。 |
| `GOOGLE_OAUTH_SCOPES` | 空 | OAuth scope 列表（**空白分隔**）。空 = Google Calendar 只读 scope。 |

两个数值项在 `Config.__post_init__` 校验（`> 0` / `>= 1`）；`MCP_SERVERS` 的结构（含 `authentication`）在 `load_config` 里强校验——从 `MCP_SERVERS_FILE` 文件（设置时优先于内联 `MCP_SERVERS`）或内联 `MCP_SERVERS` 读取，共用同一套校验；`MCP_SERVERS_FILE` 设置了却不存在 / 不可读 / 为空则是启动期 `ConfigError`。

## 限制

- **两种传输：远程 Streamable HTTP + 本地 stdio**：http 连远程端点；stdio 由后端 spawn 一个**运维配置的**子进程（`command`/`args`/`env`/`cwd`，**不**经 shell，`args` 原样传递）并走其 stdin/stdout。stdio 子进程由 SDK 的 `stdio_client` 上下文收尾；**不**自动重连（stdio 进程中途退出 → `mcp_unavailable`，重启才重发现）；**不**支持 resources / prompts / sampling（仅工具调用）。
- **OAuth 仅用户级、单 provider 起步**：凭据按 Telegram user 绑定，**不**支持群组 / 共享 / 多账号 / 账号切换 / Web 面板；本阶段内置 provider 仅 `google`（加新 provider 见上「provider 无关」）。
- **只取文本结果**：非文本（image / audio / resource / tool-use / structured）、空、超大、异常都回稳定码、**不回显**。结果受 `MAX_MCP_TOOL_RESULT_CHARS` 硬上限约束。
- **默认 `ask`**：远程工具默认走一次性审批（安全）；确认某远程工具确属只读无害，才可在 `MCP_PERMISSIONS_FILE` 里按命名空间名 pin 成 `allow`。
- **无运行时重发现**：工具集在启动时确定；服务器新增/删除工具需重启进程才反映。
- **不持久化 MCP 调用 transcript**：同 Phase 3，只持久化 user + 最终 assistant 轮；MCP 调用的元数据审计走 `tool_audit_events`（`/tool_audit` 可查）。
- **刷新失败不自动重登**：access token 刷新失败时凭据**保留**（不删）、状态显示已过期——恢复路径是用户重新 `/mcp auth`，后端绝不悄悄重发登录链接。
