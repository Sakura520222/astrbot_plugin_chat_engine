# 变更日志

## [1.2.0] - 2026-06-04

### 新增
- **记忆系统**:
  - 短期记忆: 会话级别，可配置最大条数和单条最大字符数
  - 长期记忆: 持久化存储，支持向量语义检索，可配置返回条数、候选数、相似度阈值
  - 自动总结: 按配置轮数自动将短期记忆总结为长期记忆，支持上下文压缩时联动触发
  - 置顶记忆: 标记为 pinned 的记忆每轮必注入 System Prompt，不受语义检索过滤
  - 会话级并发锁: 同一会话的自动总结任务串行执行，避免并发写入冲突
  - 后台异步执行: 自动总结和上下文压缩触发的总结任务在后台异步运行，不阻塞消息处理
  - 记忆工具 (LLM Tool Call): `save_memory`、`search_memory`、`update_memory`、`delete_memory`
- **主动回复**:
  - 超时主动发言: 用户未发言超过配置分钟数后，AI 主动发起对话
  - N 轮触发回复: 群聊中每收到 N 条消息（含被动消息）触发一次主动回复
  - 定时回复工具: `schedule_reply` LLM Tool，支持 LLM 主动安排延迟回复
  - 主动回复支持文本清洗与分段发送
  - 区分私聊和群聊场景的主动消息后缀
  - WebUI 会话级主动回复设置控制
- **WebUI 登录认证**: 新增登录页面，支持配置用户名和密码，保护管理面板访问
- **消息引用回复**: LLM Tool `reply_with_quote`，支持引用上下文中特定历史消息进行回复
- **上下文消息 ID 注入**: 用户/被动消息自动注入 `[msg:ID]` 标记，为引用回复提供锚点
- **历史上下文图片剥离**: 历史消息中的图片替换为 `[Image]` 文本占位符，仅当前用户消息保留图片，减少 Token 消耗

### 改进
- 记忆管理器重构为 Getter 函数动态获取 Provider，解决插件加载先于 Provider 初始化的时序问题
- 增强记忆管理和上下文压缩的日志记录与逻辑

### 修复
- 修复主动回复时轮数计数逻辑：注册会话后改用 `reset_round_count`，确保每次回复后从零开始重新计数
- 修复记忆存储默认设置，将 `pinned` 默认值设为 `"true"`
- 修复自动总结和上下文压缩触发的总结任务改为后台异步执行

### 配置项新增
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_memory` | `true` | 启用记忆功能 |
| `short_term_max_count` | `30` | 短期记忆最大条数 |
| `short_term_max_chars` | `200` | 每条短期记忆最大字符数 |
| `long_term_max_count` | `200` | 长期记忆最大条数 |
| `long_term_retrieval_top_k` | `5` | 长期记忆检索返回条数 |
| `long_term_fetch_k` | `20` | 长期记忆检索候选数 |
| `long_term_enable_rerank` | `true` | 启用长期记忆重排 |
| `long_term_similarity_threshold` | `0.3` | 长期记忆相似度阈值 |
| `memory_summary_interval` | `5` | 自动总结触发轮数 |
| `memory_summary_recent_turns` | `5` | 总结参考最近轮数 |
| `enable_auto_summary` | `true` | 启用自动总结 |
| `enable_proactive` | `false` | 启用主动回复 |
| `proactive_timeout_minutes` | `30` | 超时主动发言分钟数 |
| `proactive_round_interval` | `0` | N 轮触发回复（仅群聊） |
| `web_auth_enabled` | `false` | 启用 WebUI 登录认证 |
| `web_username` | `admin` | WebUI 登录用户名 |
| `web_password` | `""` | WebUI 登录密码 |

## [1.1.1] - 2026-06-03

### 新增
- **图片处理增强**:
  - 纯图片消息自动被动记录到上下文（无需文字触发）
  - 图片统一转换为 base64 data URL，兼容所有 Provider（OpenAI / Anthropic 等）
  - 支持提取引用消息（Reply）中的图片一并发送给 LLM
  - 新增图片文件存储服务（ImageStore），按 sha256 去重，避免重复存储
  - 数据库新增 `CEImage` 模型和 `ImageRepository`，实现图片持久化管理
- **分段模式增强**: 新增 `split_mode` 配置项，支持三种分段模式
  - `sentence`: 按标点符号分段（经典模式）
  - `newline`: 仅按换行符分段，保持每行完整
  - `smart`: 智能分段，保护对话引号文本不被劈断，纯叙述行按标点细分
- **文本清洗功能**:
  - 新增 `enable_text_clean` 配置项，对 LLM 回复进行后处理清洗
  - 支持去除 Emoji、括号及内容（动作描写/心理活动）、句尾多余字符
  - 支持自定义句尾清理正则表达式
- **引用回复上下文**: 用户消息自动附加引用消息的发送者和内容摘要，帮助 LLM 理解对话上下文
- **会话级异步锁**: 同一会话的消息串行处理，避免并发写入导致数据不一致
- **模态能力检测**: 自动从 Provider 获取模型支持的模态列表（text/image/tool_use），发送前过滤不支持的内容类型
- **WebUI LLM 预览**: 新增会话 LLM 预览 API，展示发送给 LLM 的完整上下文、System Prompt、工具列表和 Token 估算

### 改进
- 分段发送逻辑重构为三模式架构，使用 `re.finditer` 替代 `re.split`，提升匹配准确性
- Token 上下文限制自动回填优化，仅在值变化时持久化配置，避免每条消息都写磁盘
- 被动消息处理统一纳入 `_load_compress_save` 流程，去除重复的压缩/保存逻辑
- 被动记录消息新增 `message_id` 字段，支持消息溯源
- Web 服务器 JSON 序列化使用 `default=str`，避免特殊对象序列化异常
- WebUI 前端样式优化

### 修复
- 修复事件循环获取方式：`asyncio.get_event_loop()` -> `asyncio.get_running_loop()`，避免在异步上下文中获取错误的事件循环
- 修复配置保存的异步问题

### 配置项新增
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `split_mode` | `sentence` | 分段模式: `sentence` / `newline` / `smart` |
| `enable_text_clean` | `false` | 启用文本清洗 |
| `clean_emoji` | `true` | 去除 Emoji |
| `clean_brackets` | `true` | 去除括号内容 |
| `clean_trailing_chars` | `true` | 清理句尾字符 |
| `trailing_chars_pattern` | `[~～\\.。!！?？…·•\\-—_\\s]+$` | 句尾清理字符 (正则) |

## [1.1.0] - 2026-06-03

### 新增
- **被动消息记录**: 群聊中未触发回复的消息也可记录到上下文，丰富 LLM 对群聊的感知
  - 使用 `observed` 角色标记被动消息，避免压缩器将其计为独立轮次
  - 调用 LLM 前自动转为 `user` 角色，兼容 API 格式
- **Token 安全截断**: 调用 LLM 前自动检测上下文 Token 总量，超出阈值时从最旧消息开始裁剪
  - 优先裁剪被动记录的大量历史消息
  - 引入 `TokenEstimator` 进行精确 Token 估算
- **分段发送**: 将 LLM 回复按标点符号拆分为多条消息分段发送，模拟真人打字节奏
  - 支持自定义分段正则、最大分段数、发送间隔
- **工具调用增强**: 兼容 async generator 类型的插件 Tool handler，正确收集 yield 结果

### 改进
- 压缩器轮次拆分逻辑优化，`observed` 消息归入当前轮次而非独立开轮

### 配置项新增
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_passive_record` | `false` | 启用被动记录群聊消息 |
| `enable_split_send` | `false` | 启用分段发送 |
| `split_pattern` | `[。！？\n]` | 分段匹配符号 (正则) |
| `max_segments` | `5` | 最大分段数 |
| `split_delay_ms` | `800` | 分段发送间隔 (毫秒) |

## [1.0.0] - 2026-06-02

### 新增
- **深度消息劫持**: 完全替代 AstrBot 自带聊天管道，拦截所有消息
- **用户识别**: 群聊/私聊消息自动添加 `{{user}{昵称}({ID})}说：` 前缀
- **上下文管理**:
  - 群聊共享上下文，私聊独立隔离
  - 双模式压缩: 轮数限制 / Token 阈值 LLM 总结
  - Token 估算 (中文/英文混合)
- **人格管理**: 独立于 AstrBot 的 CRUD 人格系统
- **Tool Calls**:
  - 扫描所有已注册工具 (内置 + 插件 + MCP)
  - 工具描述写入 System Prompt + 原生 Function Calling
  - Tool Call 循环执行 (最多 10 轮)
- **WebUI 管理面板**:
  - 独立 aiohttp 服务
  - 人格管理 (CRUD)
  - 会话管理 (查看、删除)
  - 压缩配置
  - 用户标识格式配置
  - 工具管理 (启用/禁用)
- **数据库**: 独立 SQLAlchemy + 独立 MetaData，支持 SQLite/MySQL
- **命令透传**: 自动检测其他插件的命令处理器并跳过
- **多平台支持**: QQ OneBot (aiocqhttp) / Telegram
