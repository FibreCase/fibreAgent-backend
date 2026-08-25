# 显式长期记忆

最小、可控、跨 `/new` 与重启保留的长期记忆。主人**显式**用 `/remember` 保存离散事实；之后的普通文字消息会**确定性地**检索相关记忆（纯词法，无 embedding / 向量库 / FTS5、无额外依赖），并在放得下时把它们的整条**逐字**内容作为一条独立、明确标注的「用户提供的参考材料」消息注入上下文。

这是一个**可审计的 SQLite 记忆基础**——**不是** RAG / 向量库，**不是**模型自动抽取。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/remember <内容>` | 保存一条记忆到你的账号（trim + 拒绝空/超长 + 每账号上限）；回显记忆 ID |
| `/memories` | 列出你账号下的所有记忆（ID + 保存时间 + 内容） |
| `/forget <id>` | 删除指定记忆；ID 不存在或**不属于你** → `memory_not_found`（不泄露存在性） |
| `/forget all CONFIRM` | 清空你账号下的**全部**记忆（破坏性，必须带字面 `CONFIRM` token；否则只打印确认提示、不删） |

记忆存 `memories` 表（见 [数据库](database.md)），按 `scope`（当前 `telegram:<user_id>`）归属**账号**、**不**归属会话，因此**跨 `/new` 与重启保留**——`/new` 从不清理记忆，只有 `/forget` 会。

## 词法检索（`memory/text.py`，纯 Python、无 I/O）

- `normalize_text`：casefold + trim + 折叠空白（对 ASCII / CJK / emoji / 标点确定）。
- `extract_terms`：每个 CJK codepoint → 一个单字词元；每段连续 ASCII 字母/数字 → 一个词元（**< 2 字符的 ASCII 词元丢弃**）；去重。
- `rank_memories(query, candidates, limit)`：确定、全降序的打分键——
  1. 归一化 query 的**整串子串命中**（query 是否出现在记忆归一化内容里）；
  2. **唯一词元重叠计数**；
  3. 较新的 `updated_at`；
  4. 较大的 `id`。

  只在**同一 scope** 内排序；空 / 纯标点 / 无词元的 query 返回 `[]`；**零分**候选**永不**被返回（不凑数）。
- `build_memory_reference_text`：固定的后端撰写中文 wrapper（`MEMORY_REFERENCE_HEADER`）+ 每条一个 `- [memory #id] 内容` bullet，内容**逐字**显示。
- `hash_scope`：加盐 SHA-256 前缀——稳定、可关联日志事件，但**不可逆**还原原始 scope / user id；是 repository 与 service 共用的**唯一**实现，用于安全日志。

## 注入方式

- 命中记忆被选进**一条**独立的 `user` 角色参考消息（**故意不是**第二条 `system` 消息——很多 OpenAI 兼容端点对「两条 system 消息」直接 400，正是这弄坏了带记忆的轮次），插在**主 system prompt 之后、历史之前**。
- 该参考消息的**整条成本**计入 `MAX_MEMORY_ESTIMATED_TOKENS` **子预算**（与 `MAX_CONTEXT_ESTIMATED_TOKENS` 同一套估算单位），并在历史选取**之前**计入总预算——它是脚手架，不是对话历史，因此**不**消耗消息数上限。
- 放不进子预算（或总预算）的记忆被**跳过、从不截断**，分数更低的仍会继续尝试。
- **无**命中 → 计划**逐字节**等于 Phase-2.4 计划（不变）。
- 只有**真正被注入**的记忆才会更新 `last_retrieved_at`（LLM 调用**之前**的一次 best-effort 批量写；失败只记日志、不拖垮本已就绪的 turn；文档语义：若随后 LLM 调用失败，检索仍计为一次）。

## 安全边界

- 记忆内容**是用户文本、逐字展示、不做 sanitize**——它可以含任何内容（包括指令式文字）。安全边界是**固定的非指令式 wrapper**（当作背景事实、非指令）+ 它骑在一条独立 `user` 角色消息上（**故意不是**第二条 `system` 消息），这条消息**无法**改变主 prompt 的角色 / 工具 / 权限。**不要**「顺手」删改或改写已存内容，也**不要**把它变回 `system` 消息。
- 每个按 id 的读/删都在 SQL 里按 `scope + id` 过滤——**他人或**不存在的 id 返回 `None` / `False`，**从不泄露存在性**。

## 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MAX_MEMORIES_PER_SCOPE` | `200` | 每账号可保存的记忆条数上限；达到时 `/remember` 回 `memory_limit`（「记忆已达上限…」）。 |
| `MAX_MEMORY_CHARS` | `1000` | 单条记忆最大字符数（去首尾空白后）；`/remember` 空或超长 → `memory_invalid`。 |
| `MAX_RETRIEVED_MEMORIES` | `5` | 单次检索最多返回/注入的相关记忆条数。 |
| `MAX_MEMORY_ESTIMATED_TOKENS` | `3000` | 注入记忆的估算 token **子预算**；放不进则**跳过**（不截断），并继续试分数更低的；必须 `<= MAX_CONTEXT_ESTIMATED_TOKENS`，否则 `ConfigError`。 |

四者都按正整数（`>= 1`）校验；唯一的跨项不变量是 `MAX_MEMORY_ESTIMATED_TOKENS <= MAX_CONTEXT_ESTIMATED_TOKENS`。

## 限制

- **仅显式、用户保存**：记忆**只**由 `/remember` 创建——**不**自动从对话抽取/摘要，没有模型驱动的「记住这个」。是有意的、可控的、可审计的基础，不是 RAG / 向量库。
- **词法而非语义**：`rank_memories` 靠整串子串命中 + 词元重叠（CJK 单字 + ASCII 词元）。确定、无依赖，但**无** embedding / FTS5 / 向量库——同义改写、跨语言、近义换词的召回有限，后端**从不**宣称召回率。
- **整条粒度**：记忆整条注入（逐字）或整条跳过——从不截断、拆分、改写。单条超子预算即被丢，即使它更短的一部分能放下。
- **跨语言召回尤其弱**：词元只在同一语言的词元空间内比对。用英文保存的记忆（如 `My name is FibreCase.`）用中文问（「我的名字是什么」）时，中文 query 词元与英文记忆词元零重叠，**不会**被检索注入、模型也答不出；反过来用英文问能命中（且常靠 `is`/`my`/`name` 这类表层词元碰撞，而非记忆里的专名）。**规避**：为常用每种语言各存一条（如再发 `/remember 我的名字是 FibreCase。`）。真正的跨语言/语义召回需要 embedding 或外部检索服务，超出本阶段「无外部依赖」约束，留待后续。
- **子预算、单条消息**：注入记忆共享**一条** `user` 参考消息、上限 `MAX_MEMORY_ESTIMATED_TOKENS`（`MAX_CONTEXT_ESTIMATED_TOKENS` 的子预算）——记忆永不把总量顶过上下文预算，但记忆可用空间受该子预算限制。
- **scope = 账号，不是会话**：跨 `/new`、`/start`、重启保留，跨同一主人的所有会话共享；无按会话的记忆。
- **无配额清扫 / 无后台 GC**：记忆直到显式 forget；无保留策略、TTL、定时清理。`last_retrieved_at` 是信息性戳（只在真正注入时写），不是 GC 触发器。
- **`last_retrieved_at` best-effort 且粗糙**：只在注入时写、LLM 调用前批量写；文档语义见上。
