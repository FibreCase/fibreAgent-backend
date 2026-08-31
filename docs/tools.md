# 工具与工具安全

Agent 能调用的工具通过一个 OpenAI 风格 tool-calling 循环驱动，循环插在 `AgentService` 与 `LLM Client` 之间。Phase 2.1 建了工具运行时；Phase 3 在**同一个 loop 前面**加了一道统一的执行边界（策略 → 校验 → 审批 → 超时 → 审计），把「谁允许这个工具、参数合不合法、要不要人点一下、卡多久、记不记账」从工具本身里抽出来。

## 内置工具

| 工具 | 参数 | 默认权限 | 作用 |
| --- | --- | --- | --- |
| `get_current_time` | 无 | `allow` | 当前本地时间 |
| `echo` | `{"message": str}` | `allow` | 回显参数 |
| `system_info` | 无 | **`ask`**（刻意） | 主机名 / 平台 / Python 版本 |
| `exec`（**可选**） | `{"command": str}` | **`ask`**（恒） | 跑一条 `/bin/sh -c` 命令，返回 `{exit_code, stdout, stderr}` |
| `file_read`（**可选**） | `{"path": str}` | `allow` | 读 `FILE_WORKDIR` 内 UTF-8 文本文件 |
| `file_ls`（**可选**） | `{"path": str}` | `allow` | 列出 `FILE_WORKDIR` 内某目录的条目 |
| `file_edit`（**可选**） | `{"path": str, "old_string": str, "new_string": str, "replace_all"?}` | **`ask`**（恒） | 在 `FILE_WORKDIR` 内把唯一出现的 `old_string` 换成 `new_string`（或 `replace_all`） |
| `file_write`（**可选**） | `{"path": str, "content": str}` | **`ask`**（恒） | 在 `FILE_WORKDIR` 内新建文件 / **整体替换**其内容（shell `>` 语义） |
| `file_append`（**可选**） | `{"path": str, "content": str}` | **`ask`**（恒） | 在 `FILE_WORKDIR` 内向文件追加内容，不存在则先新建（shell `>>` 语义） |
| `file_mv`（**可选**） | `{"source": str, "target": str}` | **`ask`**（恒） | 在 `FILE_WORKDIR` 内移动 / 重命名文件或目录（不覆盖） |
| `file_cp`（**可选**） | `{"source": str, "target": str, "recursive"?}` | **`ask`**（恒） | 在 `FILE_WORKDIR` 内复制文件或目录树（不覆盖） |
| `file_rm`（**可选**） | `{"path": str}` | **`ask`**（恒） | 删除 `FILE_WORKDIR` 内一个**普通文件**（不删目录） |
| `file_mkdir`（**可选**） | `{"path": str, "parents"?}` | **`ask`**（恒） | 在 `FILE_WORKDIR` 内新建目录 |
| `file_rmdir`（**可选**） | `{"path": str}` | **`ask`**（恒） | 删除 `FILE_WORKDIR` 内一个**空目录** |
| `file_touch`（**可选**） | `{"path": str}` | **`ask`**（恒） | 新建空文件 / 更新已存在文件的 mtime |

三个只读工具都只用 stdlib、**只读**、无 subprocess、不碰文件。`get_current_time` 和 `echo` 声明 `allow`（跑起来不打扰）；`system_info` 虽然同样只读，但**当前刻意设为 `ask`** 以便演示审批流程——在 `tools/builtin/system_info.py` 里改回 `ToolPermission.ALLOW` 即恢复免审批。任何**未来**新工具若不声明权限，一律默认 `ask`（不能裸跑）。`exec` 与 `file` 工具集是**仅有的两个状态变更本地能力**，均为**默认关闭的可选**能力；`exec` 恒 `ask`，`file` 里 `file_read` / `file_ls` 只读（`allow`）、其余**恒 `ask`**。

### `exec`（可选的 shell 工具）

**默认关闭**（`ENABLE_EXEC_TOOL=false`）——不开则不注册、不广告给模型、默认部署仍是"零子进程"。开启后它是唯一会**真正 spawn 子进程**的内置工具，也是**通用性最强**的状态变更能力（任意 shell 工作），防御纵深（全部在 `execute` 内，loop 不动）：

- **恒 `ask`**：每次调用都需一次性人工审批，命令**逐字**显示在审批卡上（模型无法自批）。`exec` 覆写了可选的 `approval_detail` 钩子，把命令以 **bash 命令块**（`$ <command>`，多行命令换行原样保留）呈现，替换默认的 `Arguments:` JSON——属主读到的是即将执行的那条 shell 行，而不是 `{"command": "…"}`；该代码块经 `approval_language` 钩子标注为 `bash`（见下），让 Telegram 按 bash 语法高亮而非猜测（该视图仍经 HTML 转义 + 长度有界，收尾时与 `Arguments:` 一并移除）。
- **静态兜底**（`tools/exec_policy.py`）：一组保守的灾难性命令正则（递归 `rm` `/`/`$HOME`、`--no-preserve-root`、fork bomb、`curl`/`wget | sh`、`dd`/`mkfs`/裸写块设备、`shutdown`/`reboot`/`halt`/`init 0/6`、`chmod 777 /`）在 **spawn 之前**拦截——**即便你刚点了批准**也生效（防审批疲劳误批）。它是**兜底不是沙箱**：无法理解意图，故意小而保守。
- **参数向量 spawn**：`create_subprocess_exec("/bin/sh", "-c", cmd, …)`，**绝不** `shell=True`。
- **进程组杀**：`start_new_session=True` 让 `sh -c` 及所有子孙同处一个进程组；超时/取消时整组 `SIGKILL`（不留孤儿）。
- **输出尾截断**：stdout / stderr 各自截到 `MAX_EXEC_TOOL_RESULT_CHARS`，超限加固定 `[N chars … truncated]` 标记（**不是**报错——因为这是已被批准命令的直接结果，尾部正是跑完最想看的部分）。非零退出码**也算成功运行**，以 JSON 返回供模型判断。
- **只回模型**：命令与 stdout/stderr **永不进日志、永不进审计表**。

> 无沙箱 / 无降权 / 无 cgroup / seccomp：一条被批准命令的爆炸半径 = bot 运行账号。建议**不以 root 运行**、用 `EXEC_WORKDIR` 指向 scratch 目录。

### `file`（可选的文件 / 目录工具集）

**默认关闭**（`ENABLE_FILE_TOOL=false`）——不开则不注册、不广告给模型、默认部署仍是"零文件写入"。`exec` 是"跑任意命令"的**通用**能力；`file` 工具集是**更窄、更可控**的一组文件 / 目录操作——模型无需拼 shell 就能安全地读文件、精确改文件、整写 / 追加、移动 / 复制 / 新建 / 删除。十一个工具（`required = [path]` 或 `[source, target]` 或 `[path, content]`、`additionalProperties: false`），全部限定在 `FILE_WORKDIR` 内：

- **`file_read`**（`allow`）：读 `path` 指向的 UTF-8 文本文件，返回其内容（尾截断到 `MAX_FILE_READ_CHARS`）。
- **`file_ls`**（`allow`）：列出某目录的直接条目（文件 + 目录，目录以 `/` 结尾），超过 `MAX_FILE_LIST_ENTRIES` 截断并置 `truncated` 标志。
- **`file_edit`**（`ask`）：把文件里唯一出现的 `old_string` 替换为 `new_string`（`old_string` 须**恰好出现一次**，除非 `replace_all`；`new_string` 可为空串 = 删除该片段）。无模糊匹配、无整文件重写。
- **`file_write`**（`ask`）：新建文件，或**整体替换**已有文件的全部内容（shell `>` 语义）。这是"不整文件写"规则里**刻意的例外**——模型可以把任意内容放进**单个**文件，但仅限 root 内、且逐次审批。
- **`file_append`**（`ask`）：向文件追加内容，不存在则先新建（shell `>>` 语义）。已有内容**保留**（不截断）。
- **`file_mv`**（`ask`）：在 root 内移动 / 重命名文件或目录；`target` 已存在则拒（不覆盖）。
- **`file_cp`**（`ask`）：复制文件或目录树（目录需 `recursive=true`）；`target` 已存在则拒（不覆盖）。
- **`file_rm`**（`ask`）：删除一个**普通文件**；指向目录时拒（删目录用 `file_rmdir`）。
- **`file_mkdir`**（`ask`）：新建目录；`parents=true` 时连带建缺失的中间目录（`mkdir -p`）。
- **`file_rmdir`**（`ask`）：删除一个**空目录**；非空时拒。
- **`file_touch`**（`ask`）：新建空文件，或更新已存在文件的 mtime。

`file_read` / `file_ls` 只读、声明 `allow`（像 `get_current_time` / `echo` 一样无需每次审批）；**其余九个恒 `ask`**——每次调用都需一次性人工审批（模型无法自批）。

防御纵深（全部在各工具 `execute` 内，loop 不动）：

- **审批视图**：`file_edit` / `file_write` / `file_append` 覆写了可选的 `approval_detail` 钩子，把改动以 **git diff 风格**呈现在 `Action:` 块里（`📄 File:` + `🔁 Operation:` 行 + `--- a/<path>` / `+++ b/<path>` 头）——`file_edit` 逐行前缀 `-`（`old_string`）/ `+`（`new_string`；空 `new_string` = 纯删除，只有 `-`）；`file_write` 把 `content` 逐行前缀 `+`（纯新增，已有内容被丢弃，对应 `>`）；`file_append` 只展示**要追加**的 `content`（逐行 `+`，**不**转储已有内容——属主可用 `file_read` 查看）——替换默认的 `Arguments:` JSON，属主像读任何 diff 一样审批这次改动（**换行/空白原样保留**）。该代码块经 `approval_language` 钩子标注为 `diff`（见下），让 Telegram 按 diff 语法高亮而非猜测。其余文件工具只覆写 `approval_summary`（一句固定用途说明，不回显参数），参数走通用 `Arguments:` JSON 块。
- **路径受限（核心安全属性）**：每个 `path` / `source` / `target` 解析（含 `..` 与**符号链接**）后必须落在 `FILE_WORKDIR` 内，否则**在任何读/写之前**即被拒（`file_path_escape`）——`../` 逃逸、指向 root 外的绝对路径、指向外部的符号链接都逃不出去，**即便你刚点了批准**也读不写不出（`file_write` / `file_append` 同样受限，`>`-式写入无法指向 root 外）。正因如此 `FILE_WORKDIR` 启用时**必填**（须为已存在目录，config 拒绝无它启动）。
- **窄动词，不删树**：`file_rm` 不删目录、`file_rmdir` 只删空目录、`file_mv` / `file_cp` 目标已存在则拒——模型无法用它**删除或重命名一整棵树**（`file_write` / `file_append` 可以替换 / 扩展**单个**文件的内容，那正是它们 `>` / `>>` 的用途，但只能指向 root 内一个文件）。
- **原子写**：`file_edit` / `file_write` 的新内容先写入**同目录** temp（`fsync` + `os.replace`），中途被杀不留半截文件（与附件存储、MCP 权限文件同一惯例）；`file_append` 先读已有内容（若有）再原子写入拼接结果，追加同样崩溃安全（要么旧内容、要么完整新内容，绝不留半截追加）。
- **输出/参数有界**：`file_read` 内容尾截断到 `MAX_FILE_READ_CHARS`（加 `[N chars … truncated]` 标记，**不是**报错）；`file_ls` 条目数超 `MAX_FILE_LIST_ENTRIES` 截断；`file_edit` 的 `old_string`/`new_string` 各自受 `MAX_FILE_STRING_CHARS` 约束，且**同时**烧进参数 schema 的 `maxLength`——约束模型提案，也约束审批卡尺寸；`file_write` / `file_append` 的 `content` 受 `MAX_FILE_CONTENT_CHARS` 约束（同样烧进 schema `maxLength`），且 `file_append` 额外对**拼接后**的文件大小校验（超 → `file_result_too_large`，防已有大文件被撑爆）。
- **只回模型**：路径、文件内容、old/new 串、write/append 的 `content`**永不进日志、永不进审计表**（审计表结构上只存名字 / 稳定码 / 延迟 + hash scope）。
- **稳定码**：`file_path_escape` / `file_not_found` / `file_not_a_file` / `file_not_a_directory` / `file_is_directory` / `file_read_failed` / `file_invalid_path` / `file_invalid_args` / `file_not_replaced` / `file_not_unique` / `file_write_failed` / `file_result_too_large` / `file_not_empty` / `file_already_exists` / `file_fs_failed`——全部**返回**（非抛出），让具体码到达模型。

> 有意**不做**：删除一整棵树（`file_rm` 只删普通文件、`file_rmdir` 只删空目录）、覆盖式移动 / 复制、任意 shell。这些更激进的操作交给 `exec`（仍逐次审批）。`file_write` / `file_append` 提供**单个文件**的整写 / 追加（受限 + 逐次审批），因此"整文件写入 / 追加"不再是空白，但**删树 / 重命名整树 / 任意 shell** 仍不在 `file` 工具集内。

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
     → 执行前审计(fail-closed) → [若 ask] 一次性人工审批（按 scope 路由到该渠道的 broker）
     → asyncio.wait_for(execute, 超时) → 结束审计
```

### 1. 策略（allow / ask / deny）

`ToolPermission` 三态。解析顺序：**`MCP_PERMISSIONS_FILE` 覆盖（仅 MCP 工具）> 工具自带默认 > （未知名）`ask`**。内置工具（`get_current_time` / `echo` / `system_info`）不在这个文件里，恒按其声明默认值（`allow` / `allow` / `ask`）运行。

- `allow`：直接执行。
- `deny`：直接拒绝，且该工具**不再出现在**发给 LLM 的 schema 里（`advertised_names` 会把它剔除）——模型根本看不到它。
- `ask`：需要一次人工审批（见下）。

### 2. JSON Schema 校验

用 `jsonschema`（`Draft202012Validator`）对 `function.arguments` 校验该工具声明的 `parameters` schema。**非法 / 类型错 / 多余字段直接拒绝、不执行**（`validation_failed`，回给模型一个稳定错误码）。注册期还会用 `check_schema` 校验工具**自己声明的** schema 是否合法（不合法 → 启动失败）。

### 3. 一次性人工审批（仅 `ask`）

审批按 **scope 前缀路由**到对应渠道的 broker（`tools/approval.py::ToolApprovalProvider` 协议，渠道无关的 tool loop 只依赖这一协议）：Telegram 回合 → `telegram/approval.py::TelegramApprovalBroker`（唯一的 Telegram 知识来源）；QQ 回合（scope 以 `qq:` 开头）→ `qq/approval.py::QQApprovalBroker`（QQ 按钮卡，见下）。两者并存于同一个 `AgentService`，由 `qq/approval.py::QQScopedApprovalRouter` 按 scope 前缀分派。

Telegram broker：

- 在**原会话**里发一条 Approve / Deny 内联按钮消息；消息含固定标题、工具名、工具的安全**用途摘要**（`What it does:` 一行——描述工具**做什么**：内置工具各给一句固定的用途描述，MCP 工具展示其 `description`（用途）；摘要**本身**绝不回显参数），**若这次调用有参数**再另有一段参数视图——默认把**已 schema 校验**的参数以易读 JSON 展示在 `Arguments:` 的 `<pre><code>` 里（无参数且无详情则整段省略；值经 HTML 转义，无法注入标签）；工具若覆写可选的 `approval_detail` 钩子（如 `file_edit`），则改以它返回的**结构化纯文本**展示在 `Action:` 块里（同样转义 + 有界、`<pre><code>` 保留换行）——与过期提示。**代码块语言标注**：`<pre>` 带一个 `class="language-…"` 属性，让 Telegram 按该语言做语法高亮而非猜测其语言——通用 `Arguments:` 块固定标 `json`，`Action:` 块用工具经 `approval_language` 钩子声明的**固定** Pygments 语言名（`file_edit` / `file_write` / `file_append` → `diff`；`exec` → `bash`）；provider 只保留 `[A-Za-z0-9_-]`、截断到 24 字符并小写，所以该值无法注入第二个 `class`/闭合标签，返回空则不加标注。**参数/详情只出现在这张给主人看的审批卡片上**，卡片本身**不含** scope、chat id、密钥；参数**从不**写入日志、审计表，或面向模型的回退文案。
- 每个 pending 请求绑定到「**发起者 + 原会话**」：用不可逆的 `hash_scope` 指纹比对发起者（从不持有原始 user id），并要求同一 chat。**其他用户——即使是 allow-list 里的——都收到同样的「已过期/无效」安全答复，且永远不能批准**（不泄露请求是否存在）。
- **一次性**：首个有效决定即消费；重复点击、未知 id、上个进程留下的陈旧按钮、已过期请求都得到安全的「expired/invalid」，**绝不执行**。
- **卡片原地收尾**：决定（批准 / 拒绝）或超时后，**同一条**消息（按 `message_id` 定位）被**原地编辑**一次——Approve / Deny 按钮（标为 **✅ Approve** / **❌ Deny**）被移除（空 `InlineKeyboardMarkup([])`，线上序列化为 `{}`，即 Bot API「移除键盘」信号；传 `None` 会被 PTB 丢掉、按钮残留），原来的「This approval is one-time and will expire shortly.」提示行被替换为一个**加粗、带 emoji 的状态词**——`<b>✅ Approved.</b>` / `<b>❌ Denied.</b>` / `<b>⏰ Expired (no decision in time).</b>`（不加 `Status:` 前缀），**`Arguments:` / `Action:` 段也一并移除**（按钮已消失，收尾卡片只保留标题、工具名、用途摘要与状态词），不再另发一条跟进消息。收尾是 best-effort：编辑失败绝不改变已决定的结果、不抛异常、不发消息。
- **有界等待、无忙轮询**：`request_approval` 在 `asyncio.wait_for` 下 `await` 一个 `asyncio.Future`——不阻塞事件 loop、不轮询。超时（`TOOL_APPROVAL_TIMEOUT_SECONDS`）→ `approval_expired`；进程重启丢弃所有 pending（旧按钮等同未知 id）。
- 一个被阻塞的会话保持**串行**（同一 chat 的下一条消息在其 `asyncio.Lock` 上排队），另一个会话照常并行推进。

QQ broker（`qq/approval.py::QQApprovalBroker`）——同一 `ToolApprovalProvider` 协议、同一「一次性 / 绑定发起人 / 有界等待 / 重启丢弃 pending」语义，只把「审批 UI」换成 QQ 的**按钮交互**：
- **主动 C2C 审批卡**：用一条**主动消息**（`post_c2c_message`、`msg_type=2` Markdown、**不**带 `msg_id`/`msg_seq`，故**不**受 5 分钟被动回复窗口约束、也**不**与回合答案分块的 `(msg_id, msg_seq)` 去重冲突）发一张带 `keyboard` 的卡——单行两个 `action.type=1` **回调**按钮（`✅ 批准` / `❌ 拒绝`，`permission` 全员可见、`visited_label` 标注已点），按钮 `data` 只存 `v1:<request_id>:<a|d>`（**不含**参数或密钥）。卡面**只含**固定标题、`**工具：**`、`**用途：**`（`summary`）、`**操作：**`（`detail` 代码块，或参数 JSON 视图）、一次性过期提示——**不含**原始 openid、chat id、密钥。
- **交互回调**：用户点击 → QQ 推 `INTERACTION_CREATE`（Intent 位 `1<<26`，事件 `type=11`）→ broker 比对点击者 `user_openid` 的 `hash_scope` 指纹与请求发起人（**他人/未知/已过期/重复点击一律安全拒绝**、不泄露请求存在性），并**在 3 s 内** `PUT /interactions/{id}` 回 `code`（0 成功 / 1 失败；不回则客户端转圈）。决定唤醒等待中的 `asyncio.wait_for`（超时 `TOOL_APPROVAL_TIMEOUT_SECONDS` → EXPIRED）；`/stop` 取消回合时 pending 一并失效。
- **隐私**：与 Telegram 一致——参数/详情**只**出现在给主人看的卡上，**绝不**入日志、审计表或面向模型的文案；日志只记 `hash_scope` 指纹、工具名、决定码与异常类名。

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
| `MAX_TOOL_ITERATIONS` | `20` | 单条消息内 LLM↔工具最大往返；超过 → `tool_limit`。 |
| `MCP_PERMISSIONS_FILE` | 空 | 专用 MCP 工具权限文件（CWD 相对路径的 JSON **数组**，每项 `{ "tool": "mcp_<server>__<remote>", "permission": "allow\|ask\|deny\|"" }`）。**仅列 MCP 工具**，内置工具不在此文件中。由**后端维护**：启动时重同步到当前 MCP 工具集（新工具出现为未填 `""`＝默认；**已填写**的条目永远保留，即使该工具后来消失；**未填写**的消失工具条目被删去），并**热加载**（改动在下次调用即生效，无需重启）。`""`（或缺省）＝用工具默认值。未设置/空文件＝无覆盖（全部 MCP 工具默认 `ask`），非错误；**存在但损坏**的文件＝启动 `ConfigError`（坏掉的安全设置绝不被静默忽略）。 |
| `TOOL_APPROVAL_TIMEOUT_SECONDS` | `60` | `ask` 工具等待审批的秒数，超时 → `approval_expired`。必须为正。 |
| `TOOL_TIMEOUT_SECONDS` | `30` | 单个工具最长执行秒数，超时 → `tool_timeout`。必须为正。 |
| `ENABLE_EXEC_TOOL` | `false` | **可选 `exec` shell 工具的开关**。`false`（默认）→ 不注册、不广告、默认部署零子进程；`true` + `ENABLE_TOOLS=true` → 注册 `exec`（恒 `ask`）。 |
| `MAX_EXEC_TOOL_RESULT_CHARS` | `8000` | `exec` 单条命令 stdout/stderr 的尾截断上限（超限加 `[N chars … truncated]` 标记，**不是**报错）。`>= 1`；仅在开启时校验。 |
| `EXEC_WORKDIR` | 空（=进程 cwd） | `exec` 命令运行的固定目录；设置时须为**已存在目录**（否则启动 `ConfigError`）。仅在开启时校验。 |
| `EXEC_POLICY_DENY_PATTERNS` | 空 | 追加到静态灾难性命令 denylist 的正则（JSON 字符串数组，**add-only**，核心列表不可删）。坏 JSON / 非字符串元素 / 不合法正则 = 启动 `ConfigError`（**始终**校验，即便工具关闭）。 |
| `ENABLE_FILE_TOOL` | `false` | **可选 `file` 文件工具集的开关**。`false`（默认）→ 不注册、不广告、默认部署零文件写入；`true` + `ENABLE_TOOLS=true` → 注册十一个 `file_*` 工具（`file_read` / `file_ls` 为 `allow`，其余恒 `ask`）。 |
| `FILE_WORKDIR` | 无 | `file` 工具集的路径受限根目录；**启用时必填**且须为**已存在目录**（否则启动 `ConfigError`）——这是该工具集的安全前提，强制属主显式选择文件根。仅在开启时校验。 |
| `MAX_FILE_STRING_CHARS` | `2000` | `file_edit` 的 `old_string`/`new_string` 各自长度上限；同时烧进参数 schema 的 `maxLength`（约束模型提案 + 审批卡尺寸）。`>= 1`；仅在开启时校验。 |
| `MAX_FILE_READ_CHARS` | `8000` | `file_read` 结果内容的**尾截断**上限（超限加 `[N chars … truncated]` 标记，**不是**报错）。`>= 1`；仅在开启时校验。 |
| `MAX_FILE_LIST_ENTRIES` | `1000` | `file_ls` 单次返回的最大条目数；超出截断并置 `truncated` 标志。`>= 1`；仅在开启时校验。 |
| `MAX_FILE_CONTENT_CHARS` | `20000` | `file_write` / `file_append` 的 `content` 长度上限；同时烧进参数 schema 的 `maxLength`。`file_append` 额外用它校验**拼接后**文件大小（超 → `file_result_too_large`）。`>= 1`；仅在开启时校验。 |

## 加一个工具

一个工具是 `tools.base.Tool` 子类：

```python
class MyTool(Tool):
    name = "my_tool"
    description = "..."
    default_permission = ToolPermission.ASK   # 有副作用的默认 ask；确认只读无害才 ALLOW
    parameters = {"type": "object", "properties": {...}, "additionalProperties": False}

    def approval_summary(self, arguments) -> str:
        # 可选：审批卡片「What it does:」一行的用途描述。描述工具**做什么**，
        # **绝不回显** arguments——卡片会另用参数视图单独展示参数。
        # 内置工具与 MCP 工具都已各自提供用途行；不覆盖则用「只点名工具」的通用兜底。
        return "My tool does X."

    def approval_detail(self, arguments) -> str | None:
        # 可选：当原始 JSON 参数不易读时，返回一段**忠实**的纯文本参数视图（可含换行），
        # 卡片用它**替换** `Arguments:` JSON、改以 `Action:` 块展示。必须是纯文本（无标记）
        # ——provider 会 HTML 转义 + 长度有界并放进 <pre><code>（保留换行）；**逐字**展示
        # 真实参数值，不要有损改写。默认返回 None → 用通用 JSON 块（MCP/echo/… 均不覆盖）。
        # 已用它覆写的内置工具：`exec`（命令以 `$ <command>` bash 块呈现）、
        # `file_edit` / `file_write` / `file_append`（内容以 git diff 风格呈现：
        # `-`/`+` 逐行、`--- a/` / `+++ b/` 头）。
        return None

    def approval_language(self, arguments) -> str | None:
        # 可选：`Action:` 代码块的语言标注（**Pygments 语言名**，如 `diff`/`bash`/`json`），
        # Telegram 据此做语法高亮而非猜测。必须是工具固定的词表（**不从参数内容推导**），
        # provider 会清洗成安全的 `class="language-…"` 属性；返回 None → 代码块不加标注。
        # `file_edit` / `file_write` / `file_append` 返回 `diff`、`exec` 返回 `bash`；通用 `Arguments:` 块固定标 `json`。
        return None

    async def execute(self, arguments) -> str:
        # 短、可读的字符串；失败时 raise（registry 会转成 {"error": ...} 给模型）
```

然后在 `tools/builtin/__init__.py::build_default_tools()` 里 `registry.add(MyTool())`，或把自己的 `ToolRegistry` 传给 `AgentService`。**这**就是全部改动——registry 负责在 OpenAI schema 里声明它、按名分发它。

- **不要**在任何地方写 `if name == "…"` 分支——registry 是唯一分发点。
- 想给它自定义审批文案，可覆盖 `approval_summary(arguments)`——写工具**做什么**（用途），**绝不回显 `arguments`**（默认也不回显；内置工具与 MCP 工具都已提供用途行）。

**MCP / SSH / Docker / Pi / `exec` / `file`** 都是同一模式：各是一个 `Tool`（或一个产出若干工具的小 provider），subprocess / 网络 / 文件 I/O 都封装在工具**内部**、绝不进 loop，并在有副作用时走 `ask` 审批。**MCP 已按此模式接入**（见下）：`mcp/` 包在启动时发现 MCP 服务器（远程 Streamable HTTP 端点，或后端 spawn 的本地 stdio 子进程）的工具并包成标准 `Tool`（`mcp_<server>__<remote>` 命名、默认 `ask`），注册进同一个 registry，因此自动复用上面**全部**执行边界（策略 / 校验 / 审批 / 超时 / 审计）。**只读 SSH 观测（phase 5.1）也已按此模式接入**（见下）。**可选的 `exec`**（`ENABLE_EXEC_TOOL=true`）是本库第一个真正 spawn 子进程的内置工具——subprocess 同样封装在工具内部、恒 `ask`（见上）。**可选的 `file` 工具集**（`ENABLE_FILE_TOOL=true`）是第二个状态变更本地能力——一组产出十一个 `file_*` 工具的小 provider，文件 I/O 封装在工具内部、路径受限（`file_read` / `file_ls` 只读 `allow`、其余恒 `ask`，见上）。Docker / Pi 仍是待建的同类 provider。

## 只读基础设施观测（SSH，phase 5.1）

`infrastructure/` 包是又一个 **Tool Provider**（与 MCP 同类）：对每个运维配置的 SSH 目标，产出**三个固定、无参、只读**工具——`infra_<target>__host_status` / `__disk_status` / `__service_status`（主机 / 已配置挂载点磁盘 / 已配置 systemd 服务 状态）。目标须为 **Linux + systemd**。

- **与内置工具、MCP 工具同一 registry、同一 gate**：三个工具都声明 `default_permission = allow`（严格只读，与 `get_current_time` / `echo` 一样**无需每次审批**；主人仍可 pin 成 `deny`），因此每次被调用都走完整执行边界（策略 → JSON Schema 校验 → fail-closed 预审计 → 单工具超时 → 审计；因默认 `allow`，一次性审批步被跳过）。模型**无法**指任何 host / path / service / command——工具无参，远程命令是**代码常量**（模板），唯一插值的是启动期已严格校验并 shell 引号转义的 `mounts` / `services`。
- **连接是短命的、密钥锁定**：`asyncssh` **惰性**导入（仅在被调用的工具真要连接时），每个被调用的工具开一条**主机密钥固定**（`known_hosts=显式文件`，永不 `None`/自动接受）、**仅密钥**（`client_keys=[私钥]`、SSH agent 关闭、密码/键盘交互关闭）的连接，命令跑完即关闭（即便被取消也关闭）。无持久连接、无自动重连。
- **输出被解析、绝不回显**：stdout 走严格解析器（缺字段 / 重复字段 / 非法数字 / 多余字段 / 记录集不符 → 解析失败）；任何失败——连接/认证/主机密钥失败、非零退出、stderr 有输出、畸形/空/超大的结果——都映射到三个稳定、不回显的码：`infra_unavailable` / `infra_invalid_response` / `infra_result_too_large`。目标的 host、私钥路径、known_hosts 路径、用户名、mount 路径、命令、stdout/stderr **绝不**回给模型、进日志或审计表（告警只带工具名 + 稳定码 + 异常**类**，永不带异常正文）。
- **启动不触网**：构建工具是纯字符串工作，不校验连接；SSH 只在被调用的工具里发生。`/infra_status` 只读显示**已配置**的目标名 + 其三个工具名 + 总数（read-only），**不**连接、**不**探活、**不**调 LLM，且明确「不显示任何可达性结论」。host / port / 用户名 / key 路径 / known_hosts 路径 / mount / service / command **绝不**出现在其输出里。
- **默认关闭**：无目标（`INFRA_SSH_TARGETS` 空，且默认文件 `config/infra_ssh_targets.json` 不存在、`INFRA_SSH_TARGETS_FILE` 未设置）或 `ENABLE_TOOLS=false` 时不建 provider、永不发起 SSH 连接、`asyncssh` 甚至不被导入。

详见 [配置](configuration.md) 的 `INFRA_SSH_TARGETS` / `INFRA_SSH_CONNECT_TIMEOUT_SECONDS` / `MAX_INFRA_TOOL_RESULT_CHARS`。

## 限制

- 本地默认（开启即注册）3 个只读工具（`get_current_time` / `echo` / `system_info`）+ **两个默认关闭的可选状态变更本地能力**：`exec`（`ENABLE_EXEC_TOOL=true`；恒 `ask`、静态 denylist 兜底、进程组杀、输出尾截断，命令与 stdout/stderr 永不入日志/审计表）与 `file` 工具集（`ENABLE_FILE_TOOL=true`；十一个 `file_*` 工具——`file_read` / `file_ls` 只读 `allow`、其余九个恒 `ask`，`FILE_WORKDIR` 路径受限防 `../` 与符号链接逃逸、精确替换 / 单文件整写·追加 / 窄动词不删树、原子写，路径/文件内容/old/new 串与 write·append 的 `content` 永不入日志/审计表）。**SSH 只读观测（phase 5.1）** 是固定的 host / disk / service 三个无参工具（严格只读，默认 `allow`、无需每次审批），**不含** shell、任意命令/路径/主机/服务、写操作、持久连接或自动重连；**Docker / Pi 等其它状态变更工具仍未建**（有意为之）——`exec` 已覆盖通用 shell 工作、`file` 工具集已覆盖受限的文件/目录操作，但 `exec` **无沙箱**（爆炸半径 = bot 运行账号）、`file` 被限在 `FILE_WORKDIR` 内。
- 工具的**完整 transcript 不落库**（无 `tool_calls`/`tool` 消息持久化），无法逐条回放；只有元数据审计。
- 审批与 pending 状态是**内存态**：进程重启即丢，旧按钮等同未知 id（安全，不会误执行）。
- 参数校验发生在执行前（`jsonschema`）；但工具**内部**仍要自己防御外部输入。
