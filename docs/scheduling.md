# 定时任务（cron）

在**启动配置**里声明一个 cron 定时任务后，后端到一个后台调度循环（单 `asyncio.Task`）到点**自动**跑一次 Agent——**不需要**任何人发消息触发。这是一个**运维能力**，不是一个「聊天机器人功能」：定时任务只来自启动配置（改需重启），模型、聊天输入、工具参数都**无法**创建 / 修改 / 删除 / 触发任何定时任务。纯出向、**不**暴露任何新的入站端口，也不新增任何 DB 表或运行时依赖（cron 是纯 Python，时区用 stdlib `zoneinfo`）。

## 配置

定时任务来自环境变量（见 `.env.example`）：

- **`SCHEDULES`**：JSON **数组**，空（未设 / `[]` / 空白）= 无自动化、不建调度循环、不跑任何定时任务（与空 `MCP_SERVERS` 同构）。
- **`SCHEDULES_FILE`**（推荐多个任务时）：一个独立 JSON **数组**文件，设了则**优先于**内联 `SCHEDULES`（与 `MCP_SERVERS_FILE` / `INFRA_SSH_TARGETS_FILE` 同样的「文件优先」规则）。默认文件路径为 `config/schedules.json`。
- **`SCHEDULE_TIMEZONE`**：cron 求值用的 IANA 时区名（如 `Asia/Shanghai`）。未设则用进程本地时区（Docker 里由 `TZ` 决定）。启动期严格校验，非法值是 `ConfigError`。

每个任务对象有且只有五个字段：

```json
{
  "name": "morning-brief",
  "cron": "0 7 * * *",
  "prompt": "Summarise today's priorities and surface any blockers.",
  "identity": "telegram",
  "receiver": {
    "telegram": { "chat_id": 123456789, "user_id": 987654321 }
  }
}
```

- **`name`**：`[a-z][a-z0-9_-]{0,31}` 的小写 slug，**必须唯一**（整个 `SCHEDULES` 内）。它也是专属会话合成 id 的推导源（见下）。
- **`cron`**：**严格 5 字段**表达式，见下方 [cron 语法](#cron-语法)。
- **`prompt`**：每次运行喂给 Agent 的**固定文本**，≤ 2000 字符、非空。
- **`identity`**：`"telegram"` 或 `"qq"`——这次运行**以哪个渠道的身份执行**。它决定 (a) 记忆作用域（`telegram:<user_id>` vs `qq:<user_openid>`，即注入哪些长期记忆）与 (b) 审批路由（`telegram` 运行带 `delivery_chat_id`、审批卡投到该聊天；`qq` 运行不带，QQ 审批 broker 按 `qq:` 前缀路由）。`identity` **必须**指向一个**存在的** `receiver`。
- **`receiver`**：**至少含一个**键的对象，列出**结果要投递到的所有渠道**（一次运行，投递给每个列出的渠道）：
  - **`receiver.telegram`**（可选）：`{ "chat_id": 正整数, "user_id": 正整数 }`。`chat_id` = 结果通知投递到的聊天；`user_id` = 当 `identity=="telegram"` 时的 owner 身份（记忆作用域 + 专属会话行）。
  - **`receiver.qq`**（可选）：`{ "user_openid": "非空字符串" }`。`user_openid` = 结果通知投递到的 QQ C2C 用户；当 `identity=="qq"` 时它也是运行身份。**用 `/user_status` 命令在 QQ 里查到你自己的 `user_openid`** 来填这里。

三个典型配置：
- **仅 Telegram**（默认、与旧行为等价）：`identity="telegram"` + 只有 `receiver.telegram`。
- **仅 QQ**：`identity="qq"` + 只有 `receiver.qq`。
- **两个都收**：`identity` 选其一并**同时**给出两个 `receiver`——Agent 只跑一次，结果两个渠道都收到。

解析**严格、启动期 fail-fast**：任何非法值（未知字段、坏/重复 `name`、坏 cron、`identity` 缺失或不是 `telegram`/`qq`、`receiver` 缺失/空/含未知渠道键、`identity` 对应的 `receiver` 缺失、`receiver.telegram` 里 `chat_id`/`user_id` 非正整数、`receiver.qq` 里 `user_openid` 非非空字符串、`prompt` 空/超 2000、超过 16 个任务、`SCHEDULE_TIMEZONE` 非法、设了但缺失/空白的文件）都是 `ConfigError`，进程起不来。错误信息**只点名 `name` 与字段**，**绝不**回显 `prompt` 内容、`user_openid` 或任何密钥。

## cron 语法

严格 5 字段（`分 时 日 月 周`），纯 Python 实现（`automation/cron.py`）：

- **字段取值**：`*`、单值、`a-b`（含端点）、`*/n`、`a-b/n`、以及它们的逗号列表（可混用）。
- **月名 / 周名**：`JAN`–`DEC` / `SUN`–`SAT`，大小写不敏感（含范围如 `JAN-MAR`）。
- **周**：`0` 与 `7` **都是周日**（`7` 归一化为 `0`）。
- **日 + 周 的 Vixie OR 语义**：当 `日` 与 `周` **同时**被限制（都不是 `*`）时，**任一**匹配即触发（经典 Vixie cron 行为）；只有一个被限制时以那个为准。
- **严格拒绝**（都是 `ConfigError`，而非「静默不触发」）：字段数不是 5、`?`、`@daily` 之类简写、倒置范围（如 `0 10-4 * * *`）、越界值、空字段、未知 token。
- **日历不可能**：语法合法但日期不存在（如 `0 0 31 2 *`，二月没有 31 号）时，`next_fire` 有界搜索后返回 `None`——**不**死循环；该任务安全跳过、永不触发，并在 `/schedule_status` 显示 `never (untriggerable)`。

`next_fire` 是**严格之后**（不含当前分钟）的有界墙钟搜索。

## 每次运行：专属、全新的会话

这是本切片的**核心执行语义**，决定它「可预测、无累积、无失败残留」：

1. **准备专属场地**：用一个**保留区间的合成 `telegram_chat_id`** 复用到现有的 `conversations` 表（**不**新增表）。这个 id 由**任务名确定性推导**：`schedule_chat_id(name) = SCHEDULE_CHAT_ID_BASE + int(sha256(name)[:8], 16)`，落在 `SCHEDULE_CHAT_ID_BASE < id < SCHEDULE_CHAT_ID_MAX`（`9_000_000_000_000_000_000` 起步的一个 2³² 区间，比任何真实 Telegram chat id 高约 10⁷ 倍）。该行的 principal 列按 `identity` 填：`telegram` 身份存 `receiver.telegram.user_id`；`qq` 身份存 `qq_chat_id(user_openid)`（与交互 QQ 路径一致，两列都存合成 id）。`reset_conversation` 会**从零**建一个空会话——所以每次运行的历史都是空的。
2. **跑一次**：把固定 `prompt` 走**同一条**渠道无关的 `AgentService.process_message()`（交互路径用的那个），带 `memory_scope=spec.memory_scope()`（`telegram:<user_id>` 或 `qq:<user_openid>`）与 `delivery_chat_id=spec.approval_delivery_chat_id()`（`telegram` 身份 = 其 `chat_id`；`qq` 身份 = `None`，见下）。完整复用既有执行边界——工具 gate / 审计 / 上下文 / 记忆 / 持久化，**没有**第二条执行路径。
3. **发通知**：成功时把一条**格式化通知**（任务名 + 完整结果，长文自动分块、不带 reply 引用）投递到 `receiver` 里**每个**存在的渠道——Telegram 用 `deliver_markdown`，QQ 用一条**主动** C2C Markdown 消息（`deliver_qq_markdown`，`post_c2c_message`，不受被动回复 5 分钟窗口约束）。**任一渠道发送失败（或配了 QQ 接收方但 QQ 渠道没在跑）只按任务名告警、跳过该渠道，不阻塞其他渠道、不抛**；**空回复**则都不通知。
4. **清理（`finally`，总是）**：`delete_conversation(conversation.id)` 删掉专属会话，**不留痕**。

**不**并入你日常会话、**不**跨运行累积上下文（第二次运行拿不到第一次的历史）、**不**消耗你绑定聊天的上下文预算、**不**投递附件/图片（只发文本）。

**自愈 + 启动清扫**：`reset_conversation` 对**残留**的专属行（上次运行被杀、或任务已从配置删除而从未再跑）会先清掉再重建（自愈）；进程**启动时**（`_post_init`，`init_db` 之后、调度循环启动之前）还会跑一次 `clear_ephemeral_conversations()`，**只**删保留区间内的专属会话（真实 chat id 永不在该区间，因此交互会话绝不被触碰），兜底「被杀的运行」与「已删任务」的残留。

## 审批路由（纯增量，按 identity 分渠道）

当 prompt 触发一个 `ask` 工具时，审批卡按这次运行的 **`identity`** 路由到对应渠道的审批 broker：

- **`identity="telegram"`**：`process_message` 把 `delivery_chat_id=receiver.telegram.chat_id` 一路传到 `ApprovalRequest.metadata`，**Telegram** broker 在 `metadata` 里有合法正整数 `delivery_chat_id` 时**直接用**它投递审批卡（到该聊天，而非那个一次性专属会话——专属会话那一行并不对应真实聊天）。**(user, chat) 绑定不变**：`hash_scope(telegram:<user_id>)` + `chat_id`，fail-closed。
- **`identity="qq"`**：`delivery_chat_id=None`；scope 是 `qq:<user_openid>`，共享审批路由按 **`qq:` 前缀**把卡片交给 **QQ** broker，它按 `post_c2c_message` 发一张带按钮的主动卡片到该 `user_openid`。

交互路径（**不**传 `delivery_chat_id`）走**既有**的 conversation 解析路径，**字节级不变**。批准才执行；拒绝 / 超时 fail-closed（工具不执行）。

## 调度不变量

- **单任务、自持生命周期**：`_post_init`（`init_db` 与 MCP/infra 发现**之后**，最后一步）启动，`_post_shutdown`（审批 broker 排空**之后**、LLM/DB 关闭**之前**）停止。`start`/`stop` **幂等、绝不抛**——调度坏了也绝不打挂 bot（与 OAuth 回调服务器同约）。
- **不 catch-up**：每个任务的下次触发在启动与每次触发后都**从当前时刻重算**——停机期间错过的触发点**绝不回放**。
- **每任务单飞**：某任务上一次 run 仍在飞行（例如在等审批）时，它的下一个到点 tick **被跳过**（安全日志），并把触发时间推进——一个卡住的 LLM 回合不会堆出队列。
- **故障隔离**：每个 run 单独包裹；一个任务的 runner 异常只按**任务名 + 异常类**记录（**绝不**记异常正文，那可能带 prompt / 回复），不波及其余任务或循环。
- **可注入时钟 / sleep**：`now_fn` 与 sleep awaitable 可注入，所有测试都用假时钟驱动（不真实 sleep）；每次 sleep 上限 30s，保证 `stop()` 延迟有界、循环定期重估。

## 通知与隐私

- **成功通知**：`⏰ **定时任务：<name>**` + 完整结果，投递到 `receiver` 里**每个**存在的渠道（Telegram 聊天 +/或 QQ C2C）。这是**投递给你的内容**（允许含任务名与结果）。
- **失败通知**：`AgentError` → `⏰ **定时任务：<name>**` + 一段按类别映射的固定中文安全短语（如「模型服务暂时不可用。」），同样投递到**每个**存在的渠道——**绝不**是 prompt、异常正文或堆栈。其他（非 `AgentError`）异常**不发**通知（故障隔离由调度循环保证），只记录；某渠道发送抛错则被吞（按任务名记录，**不带** `user_openid`），**不影响**清理或其他渠道。
- **绝不外泄**：日志、审计表、`/schedule_status` **绝不**携带 prompt、回复内容、`chat_id`、`user_id`、`user_openid`、`cron`。定时运行的**工具**调用只经**既有** tool-audit 路径（`scope_hash` + 工具名 + 事件 + 结果码 + 耗时，绝不 prompt/回复）。`cron` 是唯一在 `/schedule_status` 里可见的调度属性（连同名字与下次触发时间）。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/schedule_status` | 只读显示**已配置**的每个定时任务的**名字 + cron + 下次触发时间**（在 `SCHEDULE_TIMEZONE` / 本地时区下算），以及时间基准说明。**只**显示这三项，**绝不**显示 prompt / chat_id / user_id / user_openid / identity。日历不可能的任务显示 `never (untriggerable)`。**不**触发任何运行、**不**调 LLM、**不**碰任何会话；未配置或为空时显示「Schedules: disabled (none configured)」；未授权发送者被静默忽略。 |
| `/user_status` | 显示**调用者自己的身份**：Telegram 侧返回 `user_id` + `chat_id`（填 `receiver.telegram` 用）；QQ 侧返回 `user_openid`（填 `receiver.qq` 用）。只回显给调用者本人，**绝不**进日志/审计。 |

## 记忆语义

运行以 `memory_scope` 进行（`identity="telegram"` → `telegram:<user_id>`；`identity="qq"` → `qq:<user_openid>`），因此**长期记忆检索仍生效**——命中的该身份的记忆会像交互路径一样注入这次运行。注意这是「带该身份记忆的、但历史为空的单次运行」：记忆是账号级、跨 `/new`/重启保留，而**会话历史**每次运行为空、运行后即删。

## 已知限制

- **只来自启动配置**：改定时任务需**重启**；**无**运行时管理命令（`/schedule add|rm|toggle` 延后）。
- **任务间无上下文连续性**：每次全新会话，无法引用上次运行的结果（这是刻意语义，可预测、无累积、无失败残留）。
- **不 catch-up**：停机期间错过的触发点不回放。
- **定时运行不进 `/stop`**：它不进交互 handler 的 `_IN_FLIGHT` 槽位，受其专属会话的 per-conversation lock 与工具/LLM 超时自然收敛。
- **只发文本**：不投递附件 / 图片。
- **QQ 接收方需 QQ 渠道在跑**：若某任务配了 `receiver.qq` 但 QQ 渠道未启用（没配 app id/secret、或登录失败），该 QQ 投递**按任务名告警并跳过**，其余渠道（Telegram）不受影响；反之亦然。这不是启动错误（与 QQ 渠道自身的降级行为一致）。
