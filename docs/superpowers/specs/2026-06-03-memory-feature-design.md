# Memory Feature Design

**Date:** 2026-06-03
**Status:** Approved
**Scope:** astrbot_plugin_chat_engine — 记忆功能

---

## Overview

为 Chat Engine 插件新增记忆系统，支持**长期记忆**和**短期记忆**两种类型，按 session_key 完全隔离。长期记忆通过本地向量存储（FAISS），短期记忆通过本地 JSON 文件存储。支持 LLM Tool Call 工具主动记忆、自动总结、WebUI 管理。

---

## Requirements

1. 记忆按 session_key 隔离（群聊 `"{platform}:{group_id}"`、私聊 `"{platform}:private:{sender_id}"`）
2. 使用 AstrBot 已配置的 EmbeddingProvider 进行向量化
3. 短期记忆：随会话删除而删除，不跟随上下文压缩丢弃，可配置指定轮数触发 AI 自动更新（整理并清理无用记忆）
4. 每隔配置的轮数和触发上下文压缩时自动总结，结果存入短期记忆
5. 四个 LLM Tool：save_memory、search_memory、update_memory、delete_memory
6. WebUI 管理记忆的增删改查

---

## Architecture

### 方案选择

**方案 A：独立 MemoryManager 模块**（已选定）

新增 `memory/` 模块，遵循现有 `context/`、`persona/`、`tools/` 的 Manager 模式。

### 模块结构

```
memory/
├── __init__.py              # 导出 MemoryManager
├── manager.py               # MemoryManager — 对外统一接口
├── vector_store.py          # LongTermMemoryStore — FaissVecDB 实例管理
├── short_term.py            # ShortTermMemoryStore — JSON 文件读写
├── summarizer.py            # MemorySummarizer — 自动总结逻辑
└── tools.py                 # 4个 @llm_tool 注册函数
```

### 依赖关系

```
main.py
  ├── MemoryManager.initialize()          # 传入 context, embedding_provider
  ├── MemoryManager (每个请求)
  │   ├── .get_memory_prompt(key, query)  # 返回拼接好的记忆文本
  │   ├── .on_turn_complete(key, ...)     # 轮数+1，检查触发条件
  │   └── .on_session_delete(key)         # 清理文件+向量
  ├── tools.py                            # 通过 llm_tool 装饰器注册
  │   └── 调用 plugin.memory_mgr
  └── MemorySummarizer
      └── 调用 provider.text_chat() + persona_mgr.get_system_prompt()
```

---

## Data Model

### 短期记忆（JSON 文件）

路径：`data/plugin_data/astrbot_plugin_chat_engine/memory/short_term/{session_key_sha256}.json`

```json
{
  "session_key": "aiocqhttp:private:12345",
  "memories": [
    {
      "id": "uuid-1",
      "content": "User prefers coding at night",
      "created_at": "2026-06-03T21:00:00",
      "updated_at": "2026-06-03T21:00:00",
      "source": "auto"
    }
  ],
  "turn_count": 12,
  "last_summary_turn": 10
}
```

- `source`: `"auto"` = 自动总结产生, `"tool"` = LLM 工具保存, `"manual"` = WebUI 手动添加
- `turn_count`: 当前累计轮数（用于触发判断）
- `last_summary_turn`: 上次自动总结时的轮数

### 长期记忆（FaissVecDB + metadata）

路径：`data/plugin_data/astrbot_plugin_chat_engine/memory/long_term/{session_key_sha256}/`

FAISS document metadata：

```json
{
  "session_key": "aiocqhttp:private:12345",
  "id": "uuid-2",
  "content": "User is a Python developer, prefers asyncio",
  "source": "tool",
  "created_at": "2026-06-03T21:00:00",
  "updated_at": "2026-06-03T21:00:00"
}
```

> session_key 使用 SHA256 哈希作为文件名，避免特殊字符（`:`）导致文件系统问题。

---

## Storage Limits

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `short_term_max_count` | int | 30 | 短期记忆最大条数 |
| `short_term_max_chars` | int | 200 | 每条短期记忆最大字符数 |
| `long_term_max_count` | int | 200 | 长期记忆最大条数 |
| `long_term_retrieval_top_k` | int | 5 | 每次检索返回条数（LLM 工具可覆盖） |
| `long_term_fetch_k` | int | 20 | 向量检索初始候选数 |
| `long_term_enable_rerank` | bool | true | 是否启用重排（需配置 RerankProvider） |
| `long_term_similarity_threshold` | float | 0.3 | 相似度低于此值的条目直接丢弃 |

### 降级策略

- **无 EmbeddingProvider** → 长期记忆功能整体禁用，日志告警，不影响短期记忆和正常对话
- **无 RerankProvider** → 跳过重排步骤，仅靠向量相似度排序
- `long_term_enable_rerank=false` → 手动关闭重排

---

## Module Responsibilities

### MemoryManager (`memory/manager.py`)

统一入口，协调短期/长期/总结器。

| 方法 | 说明 |
|------|------|
| `initialize()` | 获取 EmbeddingProvider、RerankProvider，初始化存储目录 |
| `get_memory_prompt(session_key, query)` | 返回格式化的记忆文本（短期全量 + 长期检索） |
| `search_long_term(session_key, query, top_k)` | 检索长期记忆（含 rerank 和阈值过滤） |
| `save_memory(session_key, content, type, source)` | 保存到短期或长期 |
| `update_memory(session_key, id, content, type)` | 更新指定记忆 |
| `delete_memory(session_key, id, type)` | 删除指定记忆 |
| `on_turn_complete(session_key, provider, persona_mgr, context_mgr)` | 轮数+1，检查自动总结触发 |
| `on_context_compressed(session_key, provider, persona_mgr, context_mgr)` | 压缩触发时调用总结 |
| `on_session_delete(session_key)` | 清理短期文件 + 长期向量目录 |
| `list_short_term(session_key)` | 列出短期记忆（WebUI 用） |
| `list_long_term(session_key)` | 列出长期记忆（WebUI 用） |

### LongTermMemoryStore (`memory/vector_store.py`)

管理 FaissVecDB 实例池（懒加载）。

| 方法 | 说明 |
|------|------|
| `get_or_create(session_key)` | 获取或创建该 session 的 FaissVecDB 实例 |
| `save(session_key, content, metadata)` | 插入一条向量 |
| `search(session_key, query, top_k, fetch_k, rerank, threshold)` | 语义检索 |
| `update(session_key, doc_id, content)` | 删除旧向量，插入新向量 |
| `delete(session_key, doc_id)` | 删除指定向量 |
| `list_all(session_key)` | 列出所有长期记忆（WebUI 用） |
| `close(session_key)` | 关闭并释放该 session 的 FaissVecDB 实例 |
| `close_all()` | 关闭所有实例 |

### ShortTermMemoryStore (`memory/short_term.py`)

JSON 文件读写、轮数计数。

| 方法 | 说明 |
|------|------|
| `load(session_key)` | 加载短期记忆数据 |
| `save_data(session_key, data)` | 保存短期记忆数据 |
| `add(session_key, content, source)` | 新增一条短期记忆 |
| `update(session_key, id, content)` | 更新指定短期记忆 |
| `delete(session_key, id)` | 删除指定短期记忆 |
| `increment_turn(session_key)` | 轮数+1 |
| `delete_session(session_key)` | 删除整个 session 文件 |
| `list_memories(session_key)` | 返回记忆列表 |

### MemorySummarizer (`memory/summarizer.py`)

调用 LLM 总结+清理短期记忆。

| 方法 | 说明 |
|------|------|
| `summarize(session_key, short_term_data, recent_context, persona_prompt)` | 执行总结 |

### Tools (`memory/tools.py`)

4个 `@llm_tool` 函数，通过 `Star` 实例的 `self` 引用访问 `plugin.memory_mgr`。

---

## LLM Tools

### save_memory

保存一条记忆。LLM 自主决定存储类型。

```
名称: save_memory
参数:
  - content(string, required): 记忆内容，简洁精炼，单条事实，200字以内
  - type(string, required): "short_term" 或 "long_term"
描述: Save a memory. Choose type based on persistence value:
      - short_term: temporary context (current topic, recent plans)
      - long_term: persistent facts (user preferences, identity, key decisions)
返回: "已保存到短期/长期记忆"
```

### search_memory

语义搜索长期记忆。LLM 可指定 top_k。

```
名称: search_memory
参数:
  - query(string, required): 搜索查询文本
  - top_k(integer, optional, default=5): 返回条数
描述: Semantic search in long-term memory. Short-term memory is always visible.
返回: 匹配的记忆列表 [{id, content, similarity}]，或 "No relevant memories found"
```

### update_memory

更新已有记忆内容。

```
名称: update_memory
参数:
  - id(string, required): 记忆 ID
  - content(string, required): 新的记忆内容
描述: Update an existing memory. Works for both short-term and long-term.
返回: "Memory updated" 或 "Memory not found"
```

### delete_memory

删除指定记忆。

```
名称: delete_memory
参数:
  - id(string, required): 记忆 ID
  - type(string, required): "short_term" 或 "long_term"
描述: Delete a specific memory.
返回: "Memory deleted" 或 "Memory not found"
```

---

## Integration Points

### main.py 改造

```
handle_all_messages 改造后（★ 为新增/修改）:
─────────────────────────────────────────────────
1. 预检查、命令检测、should_respond
2. 获取 Provider
3. 构建 session_key
4. async with session_lock:
   5. 加载上下文
   6. 格式化用户消息
   7. 获取人格 System Prompt
★  7.5. 注入短期记忆（全量）到 system_prompt
★  7.6. 检索长期记忆注入到 system_prompt（以用户消息为 query）
   8. 构建工具集和描述 → system_prompt += tool_desc
   9. 模态过滤
  10. Token 安全截断
  11. 调用 _llm_call_with_tools（工具集已含4个记忆工具）
  12. 返回结果、分段发送
★  13. append_and_save（含压缩检查）
★  13.5. on_turn_complete — 轮数+1，检查是否触发自动总结
★  13.6. on_context_compressed — 压缩发生时触发总结
```

### System Prompt 注入格式

```
{人格原始 system_prompt}

## Memories

### Short-Term Memory
- [id:uuid-1] User prefers coding at night
- [id:uuid-2] Discussing FastAPI async patterns

### Relevant Long-Term Memory
- [id:uuid-3] User is a Python developer, prefers asyncio

## 可用工具
{原有工具描述}
```

- 记忆注入在工具描述**之前**
- 短期记忆带 ID 方便 LLM 进行 update/delete
- 长期记忆带 ID 方便 LLM 通过 search_memory 获取后操作
- 无记忆时整个 Memories 段落省略

### 自动检索策略

- **Query**: 使用当前用户消息文本 (`user_text`)
- **流程**: `user_text` → FaissVecDB.search(fetch_k) → Rerank（可选）→ 阈值过滤 → top_k
- 空结果时跳过，不占 token

---

## Auto-Summarization

### 触发条件

| 触发条件 | 检测位置 | 说明 |
|----------|----------|------|
| 轮数触发 | `on_turn_complete` | `turn_count - last_summary_turn >= summary_interval` |
| 压缩触发 | `append_and_save` 后 | 压缩器实际发生了压缩（返回消息数 < 输入消息数） |

### 总结 Prompt

```
You are managing short-term memories for a conversation. Review the current
memories and recent dialogue, then decide what to keep, delete, add, or update.

## Current Short-Term Memories
{id: uuid-1} User prefers coding at night
{id: uuid-2} Yesterday discussed FastAPI async patterns

## Recent Dialogue
[user]: I've been rewriting the backend in Go lately
[assistant]: Go is great for concurrency...
[user]: But I'll keep using Python too

## Instructions
- Keep memories that are still relevant
- Delete memories that are outdated or redundant
- Add new important information from the dialogue
- Update memories when facts have changed
- Each memory must contain exactly one fact, under 200 chars

## Output Format (one operation per line)
KEEP: id1, id2, ...
DELETE: id1, id2, ...
ADD: new memory text
UPDATE: id | updated text

If no operations for a category, omit that line entirely.

Rules:
- Never explain your decisions
- Never output markdown or code fences
- Only output KEEP/DELETE/ADD/UPDATE lines
- One operation per line
```

System prompt 使用当前活跃人格。

### 解析器

```python
def parse_summary_output(text: str) -> dict:
    result: dict = {"keep": [], "delete": [], "add": [], "update": {}}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, body = line.partition(":")
        head = head.strip().upper()
        body = body.strip()
        if not body:
            continue
        if head == "KEEP":
            result["keep"].extend(
                x.strip() for x in body.split(",") if x.strip()
            )
        elif head == "DELETE":
            result["delete"].extend(
                x.strip() for x in body.split(",") if x.strip()
            )
        elif head == "ADD":
            if body:
                result["add"].append(body)
        elif head == "UPDATE":
            if "|" in body:
                mid, content = body.split("|", 1)
                result["update"][mid.strip()] = content.strip()
    # DELETE/UPDATE 冲突检测：DELETE 优先
    for mid in result["delete"]:
        result["update"].pop(mid, None)
    return result
```

### 容错

- 解析时跳过无法识别的行，不报错
- 空输出 = 保持现状，不修改
- ADD 行可出现多次（多条新增）
- LLM 返回无法解析的文本 → 日志告警，本次跳过，下次再触发
- 总结失败不阻塞消息响应（异步执行）

---

## WebUI

### API 路由

```python
GET    /api/memories/{session_key}/short          → 列出短期记忆
GET    /api/memories/{session_key}/long           → 列出长期记忆
POST   /api/memories/{session_key}/short          → 新增短期记忆
POST   /api/memories/{session_key}/long           → 新增长期记忆
PUT    /api/memories/{session_key}/short/{id}     → 编辑短期记忆
PUT    /api/memories/{session_key}/long/{id}      → 编辑长期记忆
DELETE /api/memories/{session_key}/short/{id}     → 删除短期记忆
DELETE /api/memories/{session_key}/long/{id}      → 删除长期记忆
```

会话删除时清理记忆：在现有 `_api_delete_session` 中追加 `memory_mgr.on_session_delete(key)`。

### 配置 API

新增配置项加入现有 `config_keys` 和 `allowed_keys` 列表：

| 配置项 | 类型 | 默认值 |
|--------|------|--------|
| `enable_memory` | bool | true |
| `short_term_max_count` | int | 30 |
| `short_term_max_chars` | int | 200 |
| `long_term_max_count` | int | 200 |
| `long_term_retrieval_top_k` | int | 5 |
| `long_term_fetch_k` | int | 20 |
| `long_term_enable_rerank` | bool | true |
| `long_term_similarity_threshold` | float | 0.3 |
| `memory_summary_interval` | int | 5 |
| `memory_summary_recent_turns` | int | 5 |
| `enable_auto_summary` | bool | true |

### 前端

新增 **"记忆"** Tab：
- 会话选择器（复用现有会话列表）
- 两个列表：短期记忆 / 长期记忆（左右或上下布局）
- 每条记忆支持：编辑内容、删除
- 新增按钮：手动添加短期/长期记忆
- 底部显示记忆相关配置区

---

## Key Design Decisions

| 决策 | 选择 | 理由 |
|------|------|------|
| save_memory 类型 | LLM 自主决定 | LLM 理解内容重要性，比分层策略更灵活 |
| 向量存储架构 | 每 session 独立 FaissVecDB | 完全隔离，删除会话直接删目录 |
| search_memory 范围 | 仅长期记忆 | 短期记忆已全量注入 system prompt |
| 总结输出格式 | 结构化文本（非 JSON） | LLM 输出更稳定，解析更容错 |
| 自动总结存储 | 短期记忆 | 长期记忆由 LLM 工具显式保存 |
| 文件名 | SHA256 哈希 | 避免 session_key 中的 `:` 等特殊字符 |
| FaissVecDB 加载 | 懒加载 | 避免启动时加载所有会话 |
