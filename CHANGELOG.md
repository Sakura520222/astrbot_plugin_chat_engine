# 变更日志

## [1.3.4] - 2026-06-12

### 新增
- **消息抖动 (Debounce)**: 群聊中用户快速连发多条消息时，自动缓冲并合并为一次 LLM 调用，减少冗余回复
  - 新增 `debounce/manager.py`，实现 `MessageDebouncer` 消息抖动管理器
  - 支持可配置的等待窗口时间，窗口内无新消息时触发处理
  - 支持最大缓冲消息数，缓冲区满时立即处理不等计时器
  - 支持适用范围选择: 仅群聊 / 仅私聊 / 所有会话
  - 支持两种合并模式: `concat`（直接拼接，保留发送者标识）和 `numbered`（为每条消息添加序号前缀）
  - 支持自定义消息分隔符
  - 会话级 flush 锁保护，防止计时器到期与强制 flush 并发冲突
  - WebUI 配置保存时支持热重载，无需重启插件即可启用/禁用抖动功能

### 改进
- **DB 迁移日志增强**: `db/engine.py` 中 `ALTER TABLE ADD COLUMN` 失败时不再静默跳过，改为记录 `logger.debug` 日志，便于排查迁移问题

### 配置项新增
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_message_debounce` | `false` | 启用消息抖动 |
| `debounce_window_ms` | `2000` | 抖动等待窗口（毫秒）|
| `debounce_max_messages` | `10` | 最大缓冲消息数 |
| `debounce_scope` | `group` | 适用范围: `group` / `private` / `all` |
| `debounce_merge_mode` | `concat` | 合并模式: `concat` / `numbered` |
| `debounce_separator` | `\n` | 消息分隔符 |

## [1.3.3] - 2026-06-08

### 新增
- **工具调用中间结果即时发送**: `_llm_call_with_tools` 重构为异步生成器，在工具调用周期中即时 yield 中间文本和链式结果，用户无需等待整个工具调用链完成即可看到 LLM 的中间回复
- **完整工具调用上下文保存**: 上下文保存机制增强，现在不仅保存用户消息和最终助手响应，还保存完整的工具调用周期消息（含 `tool_calls` 的 assistant 消息和工具结果），上下文更加完整准确
- **CommandDispatcher 结果捕获**: 新增 `capture_result` 参数和 `MessageEventResult` 链式返回支持，命令处理器通过 `yield` 返回的 `MessageEventResult` 对象现在能正确提取文本和捕获完整消息链

### 改进
- **ContextVar 管理工具调用上下文**: 引入 `_ToolCallContext` 类配合 `contextvars.ContextVar` 管理工具调用的中间状态（待发送队列、中间消息、最终响应），替代实例变量存储，解决 AstrBot EventBus 并发 dispatch 导致不同会话互相覆盖状态的竞态问题
- **分段发送逻辑抽取**: 新增 `_iter_text_segments` 异步生成器方法，统一中间文本和最终响应的分段 + 延迟逻辑，消除重复代码
- **`/switch` 锁策略优化**: 采用与 `/new` 一致的两阶段锁模式（锁内快照 + 验证 → 锁外标题生成 → 锁内归档 + 恢复），避免 LLM 网络调用阻塞并发消息处理
- **WebAPI 健壮性增强**:
  - 归档 API 新增 ID 参数校验和 JSON 解析错误处理，返回明确的错误信息
  - 恢复归档时先验证 JSON 完整性再执行不可逆操作，防止数据损坏
  - 删除归档 API 加入会话锁保护，防止并发竞态
- **前端 XSS 加固**: WebUI 会话和归档操作按钮改用 `data-*` 属性传递参数，替代内联模板字符串拼接，进一步降低 XSS 风险

### 修复
- **会话标题提取修复**: 修复 `_generate_archive_title` 中多模态内容（list 类型 content）的文本提取逻辑，正确处理包含图片的混合消息

## [1.3.2] - 2026-06-07

### 新增
- **多会话管理**: 支持在同一会话中创建、切换和浏览多个独立对话
  - 新增 `CEArchivedSession` 数据模型和 `ArchivedSessionRepository`，持久化存储归档会话
  - 新增会话管理命令（群聊需 @Bot，私聊直接发送）:
    - `/new`: 归档当前会话并开启新会话
    - `/list`: 查看所有归档会话列表
    - `/switch <N>`: 切换到指定归档会话
  - 新增 LLM 自动生成会话话题标题功能，归档时根据对话内容自动命名（失败时回退到时间戳格式）
  - 新增会话命令权限控制: 群聊中仅管理员可操作，私聊无限制
  - 归档/切换操作使用会话级锁保护，防止并发竞态
  - `/new` 采用「锁内快照 → 锁外标题生成 → 锁内归档」策略，避免 LLM 网络调用阻塞并发消息
- **WebUI 归档管理**: 新增归档会话的完整 WebUI 管理界面
  - 会话列表显示归档数量徽标，支持一键查看归档列表
  - 归档列表弹窗: 查看、恢复、删除归档会话
  - 归档详情弹窗: 查看归档的完整消息上下文（含图片）
  - 恢复归档时自动将当前活跃会话归档（使用时间戳标题，不调用 LLM）
  - 新增归档 REST API: `GET /archives`、`GET /archives/{id}`、`POST /archives/{id}/restore`、`DELETE /archives/{id}`

### 改进
- **上海时区时间戳**: 全局统一使用上海时区 (UTC+8) 替代 UTC 时间记录
  - 新增 `shanghai_now()`、`shanghai_now_iso()`、`SHANGHAI_TZ` 工具函数
  - 所有 `datetime.utcnow()` 和 `datetime.now(timezone.utc)` 统一替换为上海时区等效函数
  - 覆盖范围: 数据库模型、会话仓库、人格仓库、工具配置仓库、短期记忆、长期记忆、主动回复管理器、时间格式化
- **前端安全加固**: `escapeHtml` 函数增加双引号和单引号转义，防止 XSS 注入
- **前端样式优化**: CSS 注释风格统一，新增归档卡片样式和归档徽标样式

## [1.3.1] - 2026-06-06

### 新增
- **System Prompt 环境信息注入**: 新增 `_build_system_prompt_prefix` 方法，自动在 System Prompt 前注入环境信息
  - 自动注入当前时间，使用统一格式化函数 `format_current_time`
  - 群聊场景自动注入群名和 Bot 群昵称，帮助 LLM 感知所处环境
  - 群聊信息通过平台 API 获取，按会话缓存 5 分钟（TTL=300s）
  - API 调用失败时使用 fallback 值短 TTL 缓存（60s），避免持续重试
  - 缓存超过 500 条时自动清理过期条目，防止内存无限增长
- **主动回复时间注入**: 主动回复 System Prompt 同样注入当前时间，确保主动发言时 LLM 也能感知时间
- **图片查看工具**: 新增 LLM Tool `view_image`，允许 LLM 主动查看历史上下文中被替换为 `[Image]` 占位符的图片
  - LLM 可通过 `[msg:ID]` 标签定位目标消息并调用工具加载图片
  - 图片直接嵌入 tool result 的 content 中，支持多图同时加载
- **工具结果图片注入**: 工具调用产生的图片自动以多模态 content 格式嵌入 tool result，无需额外处理

### 改进
- **代码重构**: 图片处理相关逻辑优化，`_pending_images` 改为 `_last_tool_images`，语义更清晰
- **类型定义优化**: 成员卡片信息获取简化，使用 `or ""` 替代冗余的 `if/else` 结构
- **工具使用指南更新**: System Prompt 中的 Tool Usage Guide 新增 `view_image` 工具说明

### 修复
- **分段发送修复**: 超过最大分段数时，尾部段落合并使用换行符 `\n` 连接（原为空字符串），保留段落间的换行结构

## [1.3.0] - 2026-06-06

### 新增
- **命令执行功能**: LLM 可通过自然语言调用其他插件注册的命令
  - 新增 `CommandDispatcher` 类，扫描所有已注册命令并生成分发指引
  - 尊重每个命令自身的权限定义（admin / member / everyone），管理员限定命令仅管理员可执行
  - 新增 LLM 工具 `list_plugins`：列出所有提供命令的插件及其命令数量
  - 新增 LLM 工具 `list_commands`：根据插件名和关键词筛选可用命令的详细信息（描述、权限）
  - 新增 LLM 工具 `execute_command`：实际执行指定命令并将结果返回给 LLM
  - 命令执行指引自动注入 System Prompt，引导 LLM 正确使用命令工具

### 改进
- **主动发言精细化控制**: 超时触发不再是简单的二元判断
  - 新增触发概率控制，每次超时检查命中时按概率决定是否实际触发
  - 新增最大连续次数限制，连续主动回复达到上限后暂停直到用户再次发言
  - 用户发消息时自动重置连续计数
- **消息发送逻辑优化**: Handler 内部直接发送消息时能正确通知 ChatEngine，避免重复回复
- **命令分发器健壮性**: 自动跳过未激活（`activated` 为 `False`）的插件，仅分发已启用且已激活的插件命令

### 配置项新增
| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enable_command_execution` | `false` | 启用命令执行，允许 LLM 通过自然语言执行其他插件命令 |
| `proactive_timeout_probability` | `30` | 超时主动发言触发概率 (%)，值越低越不活跃，100 必定触发 |
| `proactive_timeout_max_consecutive` | `2` | 主动回复最大连续次数，达到后暂停直到用户再次发言，0 不限制 |

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
