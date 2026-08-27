# 工具与工具安全

Agent 能调用的工具通过一个 OpenAI 风格 tool-calling 循环驱动，循环插在 `AgentService` 与 `LLM Client` 之间。Phase 2.1 建了工具运行时；Phase 3 在**同一个 loop 前面**加了一道统一的执行边界（策略 → 校验 → 审批 → 超时 → 审计），把「谁允许这个工具、参数合不合法、要不要人点一下、卡多久、记不记账」从工具本身里抽出来。

## 三个只读内置工具

| 工具 | 参数 | 默认权限 | 作用 |
| --- | --- | --- | --- |
| `get_current_time` | 无 | `allow` | 当前本地时间 |
| `echo` | `{"message": str}` | `allow` | 回显参数 |
| `system_info` | 无 | **`ask`**（刻意） | 主机名 / 平台 / Python 版本 |

三者都只用 stdlib、**只读**、无 subprocess、不碰文件。`get_current_time` 和 `echo` 声明 `allow`（跑起来不打扰）；`system_info` 虽然同样只读，但**当前刻意设为 `ask`** 以便演示审批流程——在 `tools/builtin/system_info.py` 里改回 `ToolPermission.ALLOW` 即恢复免审批。任何**未来**新工具若不声明权限，一律默认 `ask`（不能裸跑）。

## 工具调用循环

`agent/tool_loop.py::run_tool_loop()` 依赖的只是一个「接受 `tools=` 的 LLM」（`Protocol`）和一个 `ToolRegistry`，**不**知道 Telegram、DB、OpenAI SDK：

1. 调 LLM（把 registry 生成的 OpenAI schema 作为 `tools` 传进去）。
2. 若结果带 `tool_calls`：把 assistant 的 tool-call 消息追加进 messages，**按名经 registry** 逐个执行，把 `tool` 结果追加回去。
3. 重复，直到某轮返回**不带** tool_calls 的文本（最终答案），或达到 `MAX_TOOL_ITERATIONS`（→ `tool_limit`，回一条通用的「工具调用次数过多」提示）。

- `ToolRegistry` 负责 `register` / `to_openai_schema()`（`{"type":"function","function":{...}}` 列表）/ `execute(name, args)`。工具抛出的异常被转成 JSON `{"error": ...}` 结果（一个坏工具不会拖垮整个 loop）；**未知**工具名会抛 `ToolNotFoundError`，loop 捕获并转成错误字符串。
- **只持久化** user 轮 + 最终 assistant 轮；中间的 `tool_calls` / `tool` 轮**不落库**（无法逐条回放），但元数据审计可用 `/tool_audit` 事后查看。
- `ENABLE_TOOLS=false` 时 `AgentService` **完全**跳过 loop：不传 tools、不做任何工具相关持久化，退回纯 Phase-1 单轮 LLM 调用。

## 工具安全执行边界

每个工具调用在 `execute` 前都走同一道门（顺序固定）：

```
解析 → 是否已注册? → 策略(allow/ask/deny) → JSON Schema 校验
     → 执行前审计(fail-closed) → [若 ask] 一次性 Telegram 人工审批
     → asyncio.wait_for(execute, 超时) → 结束审计
```

### 1. 策略（allow / ask / deny）

`ToolPermission` 三态。解析顺序：**`MCP_PERMISSIONS_FILE` 覆盖（仅 MCP 工具）> 工具自带默认 > （未知名）`ask`**。内置工具（`get_current_time` / `echo` / `system_info`）不在这个文件里，恒按其声明默认值（`allow` / `allow` / `ask`）运行。

- `allow`：直接执行。
- `deny`：直接拒绝，且该工具**不再出现在**发给 LLM 的 schema 里（`advertised_names` 会把它剔除）——模型根本看不到它。
- `ask`：需要一次人工审批（见下）。

### 2. JSON Schema 校验

用 `jsonschema`（`Draft202012Validator`）对 `function.arguments` 校验该工具声明的 `parameters` schema。**非法 / 类型错 / 多余字段直接拒绝、不执行**（`validation_failed`，回给模型一个稳定错误码）。注册期还会用 `check_schema` 校验工具**自己声明的** schema 是否合法（不合法 → 启动失败）。

### 3. 一次性 Telegram 人工审批（仅 `ask`）

由 `telegram/approval.py::TelegramApprovalBroker` 实现（唯一的 Telegram 知识来源，通过 `ToolApprovalProvider` 协议注入给渠道无关的 loop）：

- 在**原会话**里发一条 Approve / Deny 内联按钮消息；消息只含固定标题、工具名、工具的安全 `summary`（**默认不回显参数**）与过期提示——**不含**任何参数、scope、chat id、密钥。
- 每个 pending 请求绑定到「**发起者 + 原会话**」：用不可逆的 `hash_scope` 指纹比对发起者（从不持有原始 user id），并要求同一 chat。**其他用户——即使是 allow-list 里的——都收到同样的「已过期/无效」安全答复，且永远不能批准**（不泄露请求是否存在）。
- **一次性**：首个有效决定即消费；重复点击、未知 id、上个进程留下的陈旧按钮、已过期请求都得到安全的「expired/invalid」，**绝不执行**。
- **有界等待、无忙轮询**：`request_approval` 在 `asyncio.wait_for` 下 `await` 一个 `asyncio.Future`——不阻塞事件 loop、不轮询。超时（`TOOL_APPROVAL_TIMEOUT_SECONDS`）→ `approval_expired`；进程重启丢弃所有 pending（旧按钮等同未知 id）。
- 一个被阻塞的会话保持**串行**（同一 chat 的下一条消息在其 `asyncio.Lock` 上排队），另一个会话照常并行推进。

### 4. 单工具超时

`asyncio.wait_for(execute, TOOL_TIMEOUT_SECONDS)`：工具跑超时即被取消，回给模型「工具超时」（`tool_timeout`），**不会**长时间占住会话锁。

### 5. 只记元数据的 append-only 审计

`database/audit.py::RepositoryToolAuditor` 把事件写入 `tool_audit_events` 表：

- **执行前**审计（`record_pre`）是 **fail-closed**：写不进就当 `audit_unavailable`，**不执行** allow/ask 工具。
- **结束**审计（`record`）是 best-effort：写失败只记安全日志，**不会**重跑工具。
- 每条只存 `scope_hash`（不可逆哈希，原始 scope 只在内存里传输、落库前才被哈希）+ 工具名 + `event_type` + 稳定 `code` +（若有）`latency_ms`。**绝不**存参数、结果、异常正文、图片或密钥。
- 用 `/tool_audit [limit]`（默认 20、上限 50）查看，按 `scope_hash` 隔离——只能看自己账号的。

## 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ENABLE_TOOLS` | `true` | `false` 时完全退回纯对话（硬降级开关）。 |
| `MAX_TOOL_ITERATIONS` | `5` | 单条消息内 LLM↔工具最大往返；超过 → `tool_limit`。 |
| `MCP_PERMISSIONS_FILE` | 空 | 专用 MCP 工具权限文件（CWD 相对路径的 JSON **数组**，每项 `{ "tool": "mcp_<server>__<remote>", "permission": "allow\|ask\|deny\|"" }`）。**仅列 MCP 工具**，内置工具不在此文件中。由**后端维护**：启动时重同步到当前 MCP 工具集（新工具出现为未填 `""`＝默认；**已填写**的条目永远保留，即使该工具后来消失；**未填写**的消失工具条目被删去），并**热加载**（改动在下次调用即生效，无需重启）。`""`（或缺省）＝用工具默认值。未设置/空文件＝无覆盖（全部 MCP 工具默认 `ask`），非错误；**存在但损坏**的文件＝启动 `ConfigError`（坏掉的安全设置绝不被静默忽略）。 |
| `TOOL_APPROVAL_TIMEOUT_SECONDS` | `60` | `ask` 工具等待审批的秒数，超时 → `approval_expired`。必须为正。 |
| `TOOL_TIMEOUT_SECONDS` | `30` | 单个工具最长执行秒数，超时 → `tool_timeout`。必须为正。 |

## 加一个工具

一个工具是 `tools.base.Tool` 子类：

```python
class MyTool(Tool):
    name = "my_tool"
    description = "..."
    default_permission = ToolPermission.ASK   # 有副作用的默认 ask；确认只读无害才 ALLOW
    parameters = {"type": "object", "properties": {...}, "additionalProperties": False}

    async def execute(self, arguments) -> str:
        # 短、可读的字符串；失败时 raise（registry 会转成 {"error": ...} 给模型）
```

然后在 `tools/builtin/__init__.py::build_default_tools()` 里 `registry.add(MyTool())`，或把自己的 `ToolRegistry` 传给 `AgentService`。**这**就是全部改动——registry 负责在 OpenAI schema 里声明它、按名分发它。

- **不要**在任何地方写 `if name == "…"` 分支——registry 是唯一分发点。
- 想给它自定义审批文案，可覆盖 `approval_summary(arguments)`（但**默认**不回显参数；只有你确信安全才展示）。

**MCP / SSH / Docker / Pi** 都是同一模式：各是一个 `Tool`（或一个产出若干工具的小 provider），subprocess / 网络都封装在工具**内部**、绝不进 loop，并在有副作用时走 `ask` 审批。**MCP 已按此模式接入**（见下）：`mcp/` 包在启动时发现 MCP 服务器（远程 Streamable HTTP 端点，或后端 spawn 的本地 stdio 子进程）的工具并包成标准 `Tool`（`mcp_<server>__<remote>` 命名、默认 `ask`），注册进同一个 registry，因此自动复用上面**全部**执行边界（策略 / 校验 / 审批 / 超时 / 审计）。SSH / Docker / Pi 仍是待建的同类 provider。

## 限制

- 仅 3 个只读内置；无 shell / 文件读写 / 联网扫描 / SSH / Docker / 任何状态变更工具（有意为之）。
- 工具的**完整 transcript 不落库**（无 `tool_calls`/`tool` 消息持久化），无法逐条回放；只有元数据审计。
- 审批与 pending 状态是**内存态**：进程重启即丢，旧按钮等同未知 id（安全，不会误执行）。
- 参数校验发生在执行前（`jsonschema`）；但工具**内部**仍要自己防御外部输入。
