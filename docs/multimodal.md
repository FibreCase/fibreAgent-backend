# 多模态输入与图片附件

Telegram 照片（可带说明文字）能到达 LLM：图片 base64 内联进请求（模型端**无需**访问 Telegram），并且**持久化**——重启后，只要那条消息仍在上下文窗口/预算内，模型依然看得到它。

## 渠道无关的内容模型

`agent/messages.py` 定义了每个未来输入渠道（Web UI、相机、…）都会规范化进来的模型：

- `AgentMessage(contents: list[ContentPart], source, metadata)`，`ContentPart` 是 `TextContent` / `ImageContent` 的 `Union`——`FileContent` / `AudioContent` / … 直接往里加即可，agent 与 converter 都不用动。
- `AgentMessage.text` 返回拼接后的文本（**被持久化**的那份）；`has_image()` / `is_empty()` 是 service 用的小帮手。
- agent 层**从不**碰 Telegram 的 `Message` / `PhotoSize` / `file_id`。

## 图片怎么进来（`telegram/media.py`）

这是**唯一**下载 Telegram 媒体的模块：`normalize_message(msg, max_bytes) -> AgentMessage`。

- 取**最大**的 `message.photo[-1]` 版本，**在内存里**下载（`PhotoSize.get_file()` → `File.download_as_bytearray()`，不落临时文件），按魔数嗅探 MIME。
- 校验：大小（超过 `MAX_IMAGE_SIZE_MB` → `MediaError` `image_too_large`，回「图片过大，暂时无法处理。」，**不**发给模型）+ MIME（`image/jpeg`/`image/png`/`image/webp`，否则 `MediaError` `unsupported_mime`）。下载失败 → `MediaError` `download_failed`。所有失败都转成用户可读消息，**不**崩后端。
- 只记 `message_id` / `content_type` / `mime_type` / `size_bytes`——**不**记字节、base64、任何 secret。
- 照片的文字在 `message.caption`（纯照片时 `message.text` 为 `None`），会被读进一个 `TextContent`。因此一张纯图片消息会持久化一个空文本 user 轮——这是**正确**的，不是 bug（图片本身见下）。

## 映射到 OpenAI（`llm/message_converter.py`）

纯文本 → 普通 `str`（Phase-1 形状不变）；带图 → `list` 的 content parts（`{"type":"text"}` / `{"type":"image_url"}`，图片为 base64 `data:` URL）。纯函数，无 SDK import。

## 持久化：内容寻址 blob 存储（`attachments/`）

`AttachmentStore` 是**渠道/协议/ORM 无关**的纯文件系统模块（不 import Telegram / OpenAI SDK / SQLAlchemy）：

- **内容寻址**：按原始字节的 SHA-256 存到 `<root>/<digest[:2]>/<digest>`。
- **原子写**：同目录临时文件 → `fsync` → rename（绝不直接写最终文件名）。
- **去重**：字节相同 = 同一个 blob（`save` 返回 `created=False`，不重写）。
- **防路径穿越**：digest 必须是 64 位小写 hex，否则拒绝。`storage_key` 只从 SHA-256 派生，**永不**来自文件名/说明文字/Telegram `file_id`。
- 读缺失/损坏 blob 抛不同、用户可读的异常（`AttachmentNotFoundError` / `AttachmentCorruptError`），调用方可以**跳过**这张图但保留文字。

数据库 `attachments` 表只存**元数据**（见 [数据库](database.md)）：`sha256`、`storage_key`、`content_type`、`mime_type`、`size_bytes`、`filename`、`position`——**不存**字节/base64/`file_id`/说明文字。

## 服务层如何用它

`AgentService` 处理带 `ImageContent` 的消息时：

1. 持久化 blob + 元数据（带**补偿**：若元数据写失败，删掉刚创建的那个孤儿 blob——未持久化的图永远不会被发送）。
2. **重新入窗**：`MAX_CONTEXT_MESSAGES` / 预算窗口**之内**的历史图片，从 store 按原 part 顺序重新作为图片交回上下文；缺失/损坏的 blob → 跳过那张图、保留文字、记一条安全 warning。
3. **当前**轮始终用内存里的字节（当前轮图片**从不**被降级）；**只有窗口截断发生在重水化之前**，所以窗口外的图**既不被读、也不被发**。
4. 重水化是**计划范围的**：Phase-2.4/2.5 planner **先**选出轮次，**然后**才读被选中轮的 blob；被降级/未选中/超窗的轮次，其 blob **永不**被读。

`attachment_store is None`（测试 / 显式关闭）时图片只在当前轮发送、**不**持久化——正是 Phase-2.2 的行为。

## `/new` 回收

`reset()` 丢弃会话后**回收** blob：快照被丢 chat 的 digest，然后只删**再没被任何 attachment 引用**的 blob——被多条消息共用（去重）的 blob 会保留。删除失败 / blob 已缺失会被记日志、**绝不**阻止新会话创建。

## 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MAX_IMAGE_SIZE_MB` | `10` | 单张 Telegram 图片的最大字节数（MB）；超过在**适配器**（service 不复查）被拒，回「图片过大，暂时无法处理。」 |
| `ATTACHMENT_STORAGE_PATH` | `./data/attachments` | 内容寻址 blob 根目录（相对工作目录，按需创建）。Docker 下默认落在 `./data` 绑定挂载内。只有 blob 字节在这，DB 只有元数据。 |

## 限制

- **仅照片**：`message.photo` 被处理；文档/贴纸/视频/音频/GIF 仍在适配器被丢弃（`ContentPart` 已为它们预留）。
- **仅本地磁盘**：无对象存储 / S3 / HTTP 文件服务 / DB BLOB / Redis / RAG / 向量库。
- **MIME 是嗅探的，不深度校验**：接受 JPEG/PNG/WebP（按魔数，带声明类型兜底）；其余安全拒绝。无图像处理（缩放、降采样、去 EXIF）。
- **无配额 / 无后台 GC**：blob 只由拥有会话的 `/new` 机会式回收；无保留策略、无大小上限、无定时清扫；没有「删单张附件」或「清空所有附件」命令。
- **窗口内 + 预算内才入窗**：历史图进入 LLM 当且仅当其轮次被 planner 选中（同时在 `MAX_CONTEXT_MESSAGES` 与 `MAX_CONTEXT_ESTIMATED_TOKENS` 之内）；否则要么不重水化、要么该轮降级为纯文本——不是错误。
- **逐图 best-effort**：缺失/损坏的 blob 只把那张图降级为纯文本（安全 warning），从不崩 turn 或塞假图。
- **后端不猜模型能力**：发标准 OpenAI 多模态请求，端点不支持时它自己拒（`http_error`，用户可读），没有按模型名判断能力的表。
