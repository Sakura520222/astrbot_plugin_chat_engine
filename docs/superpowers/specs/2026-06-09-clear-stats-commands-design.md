# /clear 与 /stats 命令 + Token 用量追踪 设计文档

**日期**: 2026-06-09
**范围**: astrbot_plugin_chat_engine 插件

## 目标

新增两个内置会话命令，并同步 WebUI：

1. `/clear` —— 一键清空当前会话上下文（不归档）。
2. `/stats` —— 查看当前会话累计花费的 Token 总数。

"当前会话花费的 Token" = 自上次重置以来，所有用户可见对话轮次中 LLM 实际收发的文本 token 估算累计值。

## 核心决策（已与用户确认）

| 决策点 | 选择 |
|---|---|
| `/stats` 语义 | 累计 token（非瞬时上下文快照） |
| 计数方式 | 插件自行估算，复用现有 `TokenEstimator`（字符启发式：中文 ~0.6/字，其他 ~0.3/字）；**不**读框架 `LLMResponse.usage` |
| `/clear` 行为 | 纯清空，不归档、不生成标题，一键即清 |
| 计数归零时机 | `/clear`、`/new` 归零；`/switch`（恢复归档）恢复归档时保存的计数 |
| WebUI 同步 | 会话详情加"清空上下文"按钮 + 会话列表/详情展示 token 用量 |
| 持久化方案 | 给 `ChatSession`、`CEArchivedSession` 加列，不写迁移代码（当前仅作者一人使用，重建 DB 即可） |

## 1. 数据模型变更

文件：[db/models.py](db/models.py)

### 1.1 ChatSession（运行计数器）

新增两列，随会话行存活：

```python
prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
```

### 1.2 CEArchivedSession（归档快照）

同样新增 `prompt_tokens`、`completion_tokens` 两列，归档时写入快照，恢复时读回。

### 1.3 迁移约束

`create_all` 只建表不加列，且不写迁移逻辑。**升级需重建 DB**（删除现有 ce_*.db / 对应表后重启）。设计文档与本说明均标注此约束。

## 2. Token 计数与累计

### 2.1 计数点

文件：[main.py](main.py) `_llm_call_with_tools`

每次 `provider.text_chat` 调用（含工具循环每一轮）估算并累加：

- **prompt 端** = `count_messages_tokens(current_contexts)` + `_estimate_text(system_prompt)`
  - `current_contexts` 是实际发送给 LLM 的消息（已截断、已模态过滤），故计入的是真实发送量
  - `system_prompt` 含注入的环境信息、记忆、工具描述文本，一并计入
  - 工具 schema（func_tool 结构定义）不计 —— 属函数定义非文本，估算误差大，明确排除
- **completion 端** = `_estimate_text(response.completion_text)` + 若有 `tool_calls` 则加 `_estimate_text(json.dumps(tool_calls, ensure_ascii=False))`

### 2.2 累计载体

文件：[main.py](main.py) `_ToolCallContext`

给该 ContextVar 数据类新增两个字段（`__slots__` 与 `__init__` 同步）：

```python
self.prompt_tokens: int = 0
self.completion_tokens: int = 0
```

在 `_llm_call_with_tools` 每轮 `text_chat` 后累加到 ctx。整轮工具循环结束后，由 `handle_all_messages` 读出 ctx 的两个值，把**增量**写进会话行。

### 2.3 辅助方法

文件：[context/token_counter.py](context/token_counter.py) `TokenEstimator`

新增两个语义化方法，计数逻辑收于一处：

```python
def estimate_prompt(self, system_prompt: str, contexts: list[dict]) -> int
def estimate_completion(self, completion_text: str, tool_calls: list | None = None) -> int
```

### 2.4 不计入的范围

以下 LLM 调用不计入 `/stats`（非用户对话本体）：

- 记忆自动摘要（后台任务）
- 上下文压缩摘要（`TokenBasedCompressor._summarize`）
- 会话标题生成（`_generate_session_title`）
- 被动消息记录（未触发 LLM）

`/stats` 只反映用户可见对话轮次的花销。

## 3. 仓库层变更

文件：[db/session_repo.py](db/session_repo.py) `SessionRepository`

- `get_token_usage(session_key) -> tuple[int, int]`：读取 (prompt_tokens, completion_tokens)，行不存在返回 (0, 0)。
- `add_token_usage(session_key, prompt_delta, completion_delta)`：UPSERT，累加增量；行不存在则创建。
- `set_token_usage(session_key, prompt_tokens, completion_tokens)`：设置绝对值（用于 `/switch` 恢复归档快照）。
- `clear_session(session_key)`：清空上下文 + 计数归零（messages_json="[]", prompt_tokens=0, completion_tokens=0）。供 `/clear` 使用。
- `list_sessions`：返回项新增 `prompt_tokens`、`completion_tokens`、`total_tokens`。

文件：[db/archived_session_repo.py](db/archived_session_repo.py) `ArchivedSessionRepository`

- `archive(...)` 签名新增 `prompt_tokens: int = 0, completion_tokens: int = 0`，写入归档行。
- `get_by_id` 返回的对象携带这两个字段（ORM 自动）。

## 4. 命令实现

文件：[main.py](main.py)

### 4.1 正则与分发

在 `_SESSION_CMD_*` 正则组新增（与现有 `/new`、`/list`、`/switch` 并列）：

```python
_SESSION_CMD_CLEAR = re.compile(r"^/?clear$", re.IGNORECASE)
_SESSION_CMD_STATS = re.compile(r"^/?stats$", re.IGNORECASE)
```

`_try_handle_session_cmd` 增加两个分支，分别调用 `_cmd_clear`、`_cmd_stats`。

### 4.2 `/clear` —— `_cmd_clear(event)`

1. 权限检查：`_check_session_cmd_permission`（群聊限管理员，与 `/new` 一致）。
2. 锁内：`repo.clear_session(session_key)`（清空上下文 + 归零计数）。
3. 返回确认，含被清空的消息条数。

### 4.3 `/stats` —— `_cmd_stats(event)`

1. 无权限门槛（只读，群内任何人可查看本群会话用量）。
2. 锁内：`repo.get_token_usage(session_key)`。
3. 返回格式：

```
📊 当前会话 Token 用量
输入：1,234
输出：567
总计：1,801
```

### 4.4 `/new` 调整 —— 归档时携带计数

`_cmd_new`（[main.py:1390](main.py#L1390)）归档前读出当前计数，传入 `archive()`；归档后清空时计数一并归零（用 `clear_session` 替代 `save_context([])`，或显式 reset）。

### 4.5 `/switch` 调整 —— 恢复时读回计数

`_cmd_switch`（[main.py:1473](main.py#L1473)）：
- 归档当前会话时，连带存当前计数。
- 恢复目标归档消息后，用 `set_token_usage(session_key, target.prompt_tokens, target.completion_tokens)` 写回目标快照。

### 4.6 计数写入主流程

`handle_all_messages` 在 LLM 调用成功、上下文保存之后，读 `_tool_call_ctx` 的累计值，调用 `repo.add_token_usage(session_key, ...)`。失败/异常路径不累计。

## 5. WebUI 变更

文件：[web/server.py](web/server.py)、[web/static/app.js](web/static/app.js)、[web/static/index.html](web/static/index.html)

### 5.1 新增 API

- `POST /api/sessions/{key}/clear` → `_api_clear_session`：清空上下文 + 归零计数（对应 `/clear`）。需鉴权（与现有 session API 一致）。
- `GET /api/sessions`（`_api_list_sessions`）返回项增加 `prompt_tokens`、`completion_tokens`、`total_tokens`。
- `GET /api/sessions/{key}`（`_api_get_session`）返回项增加同上三字段。
- `POST /api/sessions/{key}/archives/{id}/restore`（`_api_restore_archive`）：恢复归档时同步恢复计数（与 `/switch` 一致）。

### 5.2 前端

- 会话列表表格新增"Token"列，显示总计（或输入/输出）。
- 会话详情区顶部展示"输入 X / 输出 Y / 总计 Z"统计条。
- 详情区增加"清空上下文"按钮（调用 `POST .../clear`），与"删除会话"区分：清空保留会话行仅重置内容与计数，删除移除整行。

## 6. 边界与错误处理

- Token 计数为估算值，WebUI/命令输出均标注"估算"。
- LLM 调用失败或返回 err 的轮次不计入。
- 计数写入失败仅记日志，不影响主对话流程（与现有 `_load_compress_save` 容错风格一致）。
- `/clear`、计数操作均在会话锁内执行，避免与并发消息竞态。
- 重建 DB 后所有计数从 0 开始；老库不升级（已知约束）。

## 7. 不做（YAGNI）

- 不读框架 `usage` 真值（保持插件自洽、跨 provider 统一）。
- 不引入 tiktoken 等真分词器依赖。
- 不写 DB 迁移代码。
- 不为标题生成/压缩/记忆摘要等辅助调用计费。
- 不做 `/clear` 二次确认（保持一键即清）。
