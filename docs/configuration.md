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
| `MAX_CONTEXT_ESTIMATED_TOKENS` | 一次请求（system + 选中的历史 + 当前消息）的**估算** token 预算上限，默认 `200000`。这是一个**模型无关的保守估算**——不是 provider 计费 token，也不做模型专用 tokenization——与 `MAX_CONTEXT_MESSAGES` 共同约束 context。超预算时按「完整历史 turn、从新到旧」选取，必要时把历史图片降级为纯文本（不读取、不发送该图）。 |
| `CONTEXT_IMAGE_ESTIMATED_TOKENS` | 估算中每张保留在 context 中的图片的成本，默认 `2000`。 |
| `ENABLE_TOOLS` | 是否启用工具调用循环，默认 `true`。设为 `false` 时完全退回纯对话行为（不传 tools、不做任何工具相关持久化）。 |
| `MAX_TOOL_ITERATIONS` | 单条消息内 LLM↔工具的最大往返次数，默认 `20`。超过则返回一条通用的「工具调用次数过多」提示。 |
| `TOOL_APPROVAL_TIMEOUT_SECONDS` | 对 `ask` 策略工具，等待 Telegram 审批（`Approve`/`Deny`）的秒数，超时则按「审批已过期」处理，默认 `60`。必须为正数。 |
| `TOOL_TIMEOUT_SECONDS` | 单个工具调用的最长执行秒数；超时即取消该工具并回给模型「工具超时」，默认 `30`。必须为正数。 |
| `MCP_SERVERS` | 可选：MCP（**远程 Streamable HTTP** + **本地 stdio**）服务器列表，JSON **数组**。默认空 = 不建 MCP 客户端、永不发起任何 MCP 连接/子进程。每个对象含 `name`（`[a-z][a-z0-9_-]{0,31}`，唯一）+ `transport`（`"http"` 默认 \| `"stdio"`）。**http**（需 `url`）：`url` 为绝对 `https://`（含 host，**不含** userinfo/fragment/query）；`bearer_token_env` 是**环境变量名**（不是 token 本身），值须非空，启动时读作 `Authorization: Bearer` 头；`authentication` 可选 `{ "type": "none"\|"oauth", "provider"?: "google" }`——**与 `bearer_token_env` 互斥**（同设即 `ConfigError`），`oauth` 须带非空 `provider`，`none` 不得带 `provider`；http 条目**不得**带 `command`/`args`/`env`/`cwd`。**stdio**（需 `command`）：`command` 为可执行名或路径（字母/数字/`_ . / -`，**不**经 shell）；`args` 为字符串数组（**原样**传递，无 glob/`$VAR` 展开）；`env` 为「合法环境变量名 → 非空字符串」对象；`cwd` 为路径；stdio 条目**不得**带 `url` / `bearer_token_env` / `authentication`（凭据放在 `env`）。发现的工具命名为 `mcp_<server>__<remote>`，默认 `ask`（可用 `MCP_PERMISSIONS_FILE` 按命名空间名覆盖）。**启动期强校验**，任何违规都是 `ConfigError`。多服务器 / stdio 建议改用 `MCP_SERVERS_FILE`（单独 JSON 文件，优先于本内联值）。 |
| `MCP_SERVERS_FILE` | 可选（**多服务器 / stdio 的推荐写法**）：把 `MCP_SERVERS` 的同一份 JSON **数组**放进单独的文件，这里填路径（相对工作目录，如 `config/mcp_servers.json`）。**设置后优先于内联 `MCP_SERVERS`**（内联值被忽略，二者设其一即可）。文件必须是服务器对象数组（字段/规则与 `MCP_SERVERS` 完全一致）。**设置了却**不存在 / 不可读 / 为空（0 字节或纯空白）文件 → 启动期 `ConfigError`（点名路径，**绝不**静默禁用服务器）；文件里显式写 `[]` 表示「无服务器」。未设置时退回内联 `MCP_SERVERS`（行为与之前一致）。 |
| `MCP_PERMISSIONS_FILE` | 可选：MCP 工具**权限覆盖**的专用 JSON **数组**文件（相对工作目录），每项 `{ "tool": "mcp_<server>__<remote>", "permission": "allow\|ask\|deny\|"" }`。**仅列 MCP 工具**——内置工具（`get_current_time` / `echo` / `system_info`）恒按其声明默认值运行，不在此文件中。由**后端维护**：启动时重同步到当前 MCP 工具集（新工具出现为未填 `""`＝默认；**已填写**的条目永远保留，即使该工具后来消失；**未填写**的消失工具条目被删去），并**热加载**（你的改动在下次工具调用即生效，无需重启）。`""`（或字段缺省）＝用工具默认值。设置了却不存在 / 为空（0 字节或纯空白）＝无覆盖（全部 MCP 工具默认 `ask`），非错误；设置了却**存在但损坏**（非数组 / 坏条目 / 未知字段 / 非法工具名或权限 / 重复工具名）→ 启动期 `ConfigError`（坏掉的安全设置绝不被静默忽略）。 |
| `MCP_CONNECT_TIMEOUT_SECONDS` | 每个 MCP 服务器「连接 / initialize / tools-list」握手的超时秒数，超时即把该服务器标记为 unavailable（**其余服务器与内置工具照常启动**，bot 不会因一个可选 MCP 服务器宕机而启动失败）。默认 `10`，必须为正。 |
| `MAX_MCP_TOOL_RESULT_CHARS` | 单个远程 MCP 工具结果回传给模型的**文本**字符硬上限。超大的结果按稳定码 `mcp_result_too_large` 拒绝（**不截断、不回显**）。默认 `10000`，必须 `>= 1`。 |
| `MCP_ALLOW_INSECURE_HTTP` | 硬开关：允许 `http://`（明文）**http** 端点。默认 `false`（仅 `https`）；仅在你**控制**的本地/内网可信端点才设 `true`。stdio 服务器无 URL，不受此项影响。 |
| `OAUTH_CALLBACK_BASE_URL` | MCP **用户级 OAuth**（phase 4.x）的公网回调 origin。**空（默认）= OAuth 整体关闭**——不构造 provider、不启动回调服务器。非空时必须是**裸 origin**：绝对 `http(s)://` + host，**不含** userinfo / path / query / fragment / 末尾斜杠（否则启动期 `ConfigError`）。provider 的实际重定向 URI 是 `<OAUTH_CALLBACK_BASE_URL>/oauth/callback`，必须能在你的 Google Cloud OAuth 客户端里登记且**能被 Google 公网访问**（内网/localhost 需反代或隧道暴露）。 |
| `OAUTH_CALLBACK_PORT` | 最小回调 HTTP 服务器的监听端口（仅 OAuth 配置时启动，与 long polling 同处一个事件循环）。默认 `8090`，须 `1..65535`。 |
| `OAUTH_STATE_TTL_SECONDS` | 授权 `state` 的存活秒数（过期后回调作废）。默认 `600`，必须 `> 0`。 |
| `GOOGLE_OAUTH_CLIENT_ID` | google provider 的 OAuth client id。**仅在** `main.py` 的 provider 注册处从环境变量读取（全代码库唯一按 provider 名分派的地方）——不存入 config、永不进日志。未设置 = google provider 未配置，请求它的 MCP 服务器被标记为 unavailable（稳定码只进日志），bot 照常启动。 |
| `GOOGLE_OAUTH_CLIENT_SECRET` | google provider 的 client secret，env-only，同上。 |
| `GOOGLE_OAUTH_SCOPES` | OAuth scope 列表（**空白分隔**）。空（默认）= Google Calendar 只读 scope。 |
| `INFRA_SSH_TARGETS` | 可选：只读**基础设施观测**（phase 5.1，SSH）目标列表，JSON **数组**。空 = 不建 infrastructure provider、永不发起任何 SSH 连接（目标也可放在默认文件 `config/infra_ssh_targets.json`，或经 `INFRA_SSH_TARGETS_FILE` 指向另一个文件——见下行）。每个对象含 `name`（`[a-z][a-z0-9_-]{0,31}`，唯一）+ `host`（主机名或 IPv4/IPv6 字面量，**不含** user/port/path）+ `port`（int `1..65535`）+ `username` + `private_key_path` + `known_hosts_path`（两者须为**存在、可读**的文件路径——**绝对**或**相对工作目录**（如 `config/id_ncepu-pa` / `config/ssh_known_hosts`，无 `~`/`..`/符号链接），启动期校验存在以便坏掉的 secret 挂载快速失败，但内容永不读入 config/日志）+ `mounts`（disk 工具观测的绝对路径数组）+ `services`（service 工具读取的 systemd 单元名数组）。每个目标生成**三个**固定、无参工具 `infra_<target>__host_status` / `__disk_status` / `__service_status`，全部走**完整 phase-3 gate**且默认 `allow`（严格只读，与 `get_current_time` / `echo` 一样**无需每次审批**；主人仍可 pin 成 `deny`）。目标须为 Linux + systemd。**刻意**无 password/agent/forwarding/SFTP 字段（无法表达即无法启用）。最多 16 个目标、重复 name 拒绝；**启动期强校验**，违规 `ConfigError` 只点名目标/字段，**从不**回显 host、key 路径、known_hosts 路径或 mount 路径。 |
| `INFRA_SSH_TARGETS_FILE` | 可选（**多目标 / 挂载密钥的推荐写法**）：把 `INFRA_SSH_TARGETS` 的同一份 JSON **数组**放进单独的文件，这里填路径（相对工作目录，如 `config/infra_ssh_targets.json`）。**设置后优先于默认文件与内联 `INFRA_SSH_TARGETS`**（两者都被忽略，以此文件为唯一来源）。文件必须是目标对象数组（字段/规则与 `INFRA_SSH_TARGETS` 完全一致）。**设置了却**不存在 / 不可读 / 为空（0 字节或纯空白）→ 启动期 `ConfigError`（点名路径，**绝不**静默禁用 provider）；文件里显式写 `[]` 表示「无目标」。**未设置时**：若**默认文件** `config/infra_ssh_targets.json` 存在则读它（默认空/不建 provider 的场景无需任何 env var；默认文件存在但为空 → `ConfigError`），否则退回内联 `INFRA_SSH_TARGETS`（行为与之前一致）。 |
| `INFRA_SSH_CONNECT_TIMEOUT_SECONDS` | 单次批准调用里 SSH「连接/握手」的超时秒数。必须为正，且必须 `<= TOOL_TIMEOUT_SECONDS`（整次 SSH 调用运行在该次工具超时之内，更长的连接超时永远无法触发）。默认 `10`。 |
| `MAX_INFRA_TOOL_RESULT_CHARS` | 单个 infra 工具结果回传给模型的字符硬上限。超大结果按稳定码 `infra_result_too_large` 拒绝（**不截断、不回显**）。默认 `8000`，必须 `>= 1`。 |
| `ENABLE_EXEC_TOOL` | **可选 `exec` shell 工具的开关**，默认 `false`。`false` → 不注册、不广告、默认部署**零子进程**（"by design, no subprocess" 对默认部署依然成立）；`true` + `ENABLE_TOOLS=true` → 注册 `exec`（`/bin/sh -c` 完整 shell，**恒 `ask`**，每次调用需一次性人工审批、命令逐字展示）。建议**不以 root 运行**，并用 `EXEC_WORKDIR` 指向 scratch 目录。 |
| `MAX_EXEC_TOOL_RESULT_CHARS` | `exec` 单条命令 stdout / stderr 各自的**尾截断**上限，默认 `8000`。超限时保留**尾部** N 字符并在前置固定标记 `[N chars of earlier output truncated]`（**不是**报错，也不像 MCP/infra 那样按稳定码拒绝——因为这是已被批准命令的直接结果，尾部正是最常看的部分）。`>= 1`；仅在 `ENABLE_EXEC_TOOL=true` 时校验。 |
| `EXEC_WORKDIR` | `exec` 命令运行的固定目录；空（默认）= 进程当前工作目录。设置时必须是**已存在目录**，否则启动期 `ConfigError`（fail-fast）。仅在 `ENABLE_EXEC_TOOL=true` 时校验。 |
| `EXEC_POLICY_DENY_PATTERNS` | 追加到 `exec` 静态灾难性命令 denylist 的正则，JSON **字符串数组**，默认空。**add-only**：核心列表（`tools/exec_policy.py` 代码编译、恒生效）不可删，此旋钮只能追加。坏 JSON / 非数组 / 非字符串元素 / 空白元素 / 不合法正则 → 启动期 `ConfigError`（点名数组索引，**从不**回显 pattern 正文）；**始终**校验，即便 `ENABLE_EXEC_TOOL=false`。 |
| `ENABLE_EDIT_TOOL` | **可选 `edit` 文件编辑工具的开关**，默认 `false`。`false` → 不注册、不广告、默认部署**零文件写入**；`true` + `ENABLE_TOOLS=true` → 注册 `edit`（在 `EDIT_WORKDIR` 内读 / 精确替换，**恒 `ask`**，每次调用需一次性人工审批、`path`/`old_string`/`new_string` 逐字展示）。建议用 `EDIT_WORKDIR` 指定一个明确的编辑根目录。 |
| `EDIT_WORKDIR` | `edit` 的路径受限根目录。**启用时必填**且必须是**已存在目录**（否则启动期 `ConfigError`，fail-fast）。比 `EXEC_WORKDIR`（可选）更严——限定目录是 `edit` 的安全前提，强制属主显式选择编辑根；`..` 逃逸与指向外部的符号链接在此目录外被拒（任何 I/O 之前）。仅在 `ENABLE_EDIT_TOOL=true` 时校验。 |
| `MAX_EDIT_STRING_CHARS` | `edit` 的 `replace` 中 `old_string`/`new_string` 各自的长度上限，默认 `2000`。同时烧进参数 schema 的 `maxLength`——既约束模型提案，也把审批卡 `Arguments:` 块压到有界尺寸。`>= 1`；仅在 `ENABLE_EDIT_TOOL=true` 时校验。 |
| `MAX_EDIT_READ_CHARS` | `edit` 的 `read` 结果内容的**尾截断**上限，默认 `8000`。超限时保留**尾部** N 字符并前置固定标记 `[N chars of earlier output truncated]`（**不是**报错，也不按稳定码拒绝——因为这次读已被人工批准）。`>= 1`；仅在 `ENABLE_EDIT_TOOL=true` 时校验。 |
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
- MCP 数值项：`MCP_CONNECT_TIMEOUT_SECONDS` 必须为正数、`MAX_MCP_TOOL_RESULT_CHARS` 必须 `>= 1`，否则 `ConfigError`。`MCP_SERVERS` 的**结构**（从 `MCP_SERVERS_FILE` 文件——设置时优先于内联值——或内联 `MCP_SERVERS` 读取，两者共用同一套校验）在 `load_config` 里按 transport 分派强校验：http 条目（`url` 绝对 `https` / 无 userinfo+fragment+query、`authentication` 与 `bearer_token_env` 互斥、http 不得带 `command`/`args`/`env`/`cwd`），stdio 条目（`command` 必填且字符集受限、`args` 字符串数组、`env` 合法名→非空串、stdio 不得带 `url`/`bearer_token_env`/`authentication`），以及非法 JSON、非数组、非对象条目、未知字段、坏 name、非法 `transport`、重复 name——都 `ConfigError`。`MCP_SERVERS_FILE` 设置了却**不存在 / 不可读 / 为空（0 字节或空白）**同样 `ConfigError`（点名路径，绝不静默禁用服务器；文件里显式 `[]` 表示无服务器）。报错只点名服务器与字段，**从不**回显 token 值、完整 URL，或 stdio 的 `command`/`args`/`env`/`cwd`（`env` 校验报错只点名**键**，不回显值）。
- `MCP_PERMISSIONS_FILE`：设置了却**不存在 / 为空（0 字节或纯空白）**＝无覆盖，**不是**错误（后端会在启动时创建/同步它）；设置了却**存在但非空且损坏**（非法 JSON、非数组、非对象条目、缺 `tool`、未知字段、非法工具名、非法 `permission`、重复 `tool`）→ 仅当 `ENABLE_TOOLS=true` 时 `ConfigError`（坏掉的安全设置绝不静默忽略）。运行期的热加载损坏则保留上次可用策略并告警（不崩溃）。
- OAuth 项：`OAUTH_CALLBACK_BASE_URL` 非空时必须是裸 origin（绝对 `http(s)://` + host、无 userinfo/path/query/fragment/末尾斜杠），否则 `ConfigError`；`OAUTH_CALLBACK_PORT` 须 `1..65535`；`OAUTH_STATE_TTL_SECONDS` 必须 `> 0`。Google client id / secret 缺失**不是**错误——只是 google provider 未配置。
- Infra 项（phase 5.1）：`INFRA_SSH_CONNECT_TIMEOUT_SECONDS` 必须为正、`MAX_INFRA_TOOL_RESULT_CHARS` 必须 `>= 1`，且 `INFRA_SSH_CONNECT_TIMEOUT_SECONDS <= TOOL_TIMEOUT_SECONDS`（跨项不变量）——违反任一即 `ConfigError`。目标列表的**来源**（`INFRA_SSH_TARGETS_FILE` 设置时优先，否则默认文件 `config/infra_ssh_targets.json` 存在时读它，否则内联 `INFRA_SSH_TARGETS`）与 `MCP_SERVERS_FILE` 同思路：`INFRA_SSH_TARGETS_FILE` 设置了却**不存在 / 不可读 / 为空（0 字节或空白）**同样 `ConfigError`（点名路径，绝不静默禁用 provider；文件里显式 `[]` 表示无目标）；默认文件**存在但为空**同样 `ConfigError`（存在但空不得静默当作「无目标」）。解析出的**结构**在 `load_config` 里强校验：非法 JSON、非数组、非对象条目、坏/重复 `name`、坏 `host`（含 user/port/路径/空白）、非 int 或越界 `port`、坏 `username`、缺失/含 `..`/软链/目录的 key 或 known_hosts 文件（路径**绝对或相对工作目录**均可）、空的 known_hosts、坏/超长 `mounts`/`services`——都 `ConfigError`。报错只点名目标（或其索引）与字段，**从不**回显 host、key 路径、known_hosts 路径或 mount 路径（service/单元名可作为操作者自选的非秘密值出现）。
- `exec` 项（可选 shell 工具）：`MAX_EXEC_TOOL_RESULT_CHARS` 必须 `>= 1`、`EXEC_WORKDIR` 若设置必须是**已存在目录**——两者**仅当 `ENABLE_EXEC_TOOL=true`** 时校验（沿用「关闭的可选能力不强制其配置」惯例，关闭时设不设置都不影响启动）。`EXEC_POLICY_DENY_PATTERNS` **始终**强校验（即便 `ENABLE_EXEC_TOOL=false`）：非法 JSON、非数组、非字符串元素、空白元素、或不合法的正则 → `ConfigError`（点名数组**索引**，**从不**回显 pattern 正文）。
- `edit` 项（可选文件编辑工具）：`MAX_EDIT_STRING_CHARS` 与 `MAX_EDIT_READ_CHARS` 都必须 `>= 1`，且 `EDIT_WORKDIR` 必须是**已存在目录**（**必填**，缺省/空白也报错）——三者**仅当 `ENABLE_EDIT_TOOL=true`** 时校验（沿用「关闭的可选能力不强制其配置」惯例，关闭时设不设置都不影响启动）。比 `exec` 严格在 `EDIT_WORKDIR` 上：`EXEC_WORKDIR` 可缺省（=进程 cwd），而 `EDIT_WORKDIR` 因是 `edit` 路径受限的安全前提，启用即强制。

## System Prompt

默认读取 `config/system_prompt.txt`（文件优先）。也可以用环境变量 `SYSTEM_PROMPT` 覆盖文件（设置后忽略文件）。若两者都没有，使用一个内置兜底 prompt。优先级：`SYSTEM_PROMPT`（env）> `SYSTEM_PROMPT_PATH`（文件）> 内置兜底。

编辑 `config/system_prompt.txt` 即可调整 Agent 的语气与边界，无需改代码。
