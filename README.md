# Chat Engine - AstrBot 聊天增强插件

完全替代 AstrBot 自带聊天功能，独立实现上下文管理、用户识别、人格系统、Tool Calls、上下文压缩、记忆系统和 WebUI 管理面板。

## 功能特性

### 用户识别
- 群聊和私聊中每条消息自动添加 `{{user}{昵称}({ID})}说：` 前缀
- 帮助 AI 在上下文中准确区分不同用户
- 用户标识格式可通过 WebUI 自定义

### 环境信息感知
- 自动在 System Prompt 前注入当前时间，帮助 LLM 感知时间上下文
- 群聊场景自动注入群名和 Bot 群昵称
- 群聊信息按会话缓存（5 分钟 TTL），API 失败时使用短 TTL fallback 策略
- 主动回复同样注入时间信息

### 上下文管理
- **群聊共享**: 同一群内所有人共享上下文
- **私聊隔离**: 每个用户拥有独立上下文
- **被动消息记录**: 群聊中未触发回复的消息也可记录到上下文，丰富 LLM 对群聊的感知
- **图片处理**: 纯图片消息自动被动记录，图片转为 base64 data URL 兼容所有 Provider，支持提取引用消息中的图片
- **引用回复上下文**: 用户消息自动附加引用消息摘要，帮助 LLM 理解对话上下文
- **会话级锁保护**: 同一会话消息串行处理，避免并发写入冲突
- **Token 安全截断**: 调用 LLM 前自动检测 Token 总量，超出阈值时从最旧消息开始裁剪
- **上下文压缩**: 双模式支持
  - 轮数限制: 超过限制直接丢弃最早的消息
  - Token 阈值: 达到模型上下文的 N% 时自动 LLM 总结压缩
- **消息 ID 注入**: 用户和被动消息自动注入 `[msg:ID]` 标记，为引用回复提供锚点
- **历史图片剥离**: 历史消息中的图片替换为 `[Image]` 文本占位符，仅当前消息保留图片，减少 Token 消耗

### 记忆系统
- **短期记忆**: 会话级别临时记忆，可配置最大条数和单条最大字符数
- **长期记忆**: 持久化存储，支持向量语义检索（Embedding + 可选 Rerank），可配置返回条数、候选数和相似度阈值
- **自动总结**: 按配置轮数自动将短期记忆总结为长期记忆，上下文压缩时联动触发
- **置顶记忆**: 标记为 pinned 的记忆每轮必注入 System Prompt，不受语义检索过滤
- **会话级并发锁**: 同一会话的自动总结任务串行执行，避免并发写入冲突
- **LLM 记忆工具**: `save_memory`、`search_memory`、`update_memory`、`delete_memory`，LLM 可主动记忆和管理

### 主动回复
- **超时主动发言**: 用户未发言超过配置分钟数后，AI 主动发起对话
  - 支持触发概率控制，按概率决定是否实际触发，避免过于频繁
  - 支持最大连续次数限制，连续主动回复达到上限后暂停直到用户再次发言
- **N 轮触发回复**: 群聊中每收到 N 条消息（含被动消息）触发一次主动回复
- **定时回复**: `schedule_reply` LLM Tool，支持 LLM 主动安排延迟回复（提醒、跟进等）
- **消息引用回复**: `reply_with_quote` LLM Tool，支持引用上下文中特定历史消息进行回复
- **图片查看**: `view_image` LLM Tool，支持 LLM 主动查看历史上下文中被替换为 `[Image]` 占位符的图片
- 主动回复支持文本清洗与分段发送
- 区分私聊和群聊场景的主动消息后缀
- WebUI 会话级主动回复设置控制

### 命令执行
- LLM 可通过自然语言调用其他插件注册的命令
- 自动扫描所有已注册命令并生成结构化指引注入 System Prompt
- 尊重每个命令自身的权限定义（admin / member / everyone）
- LLM 工具 `list_plugins`：列出所有提供命令的插件
- LLM 工具 `list_commands`：按插件名和关键词筛选可用命令
- LLM 工具 `execute_command`：实际执行指定命令并返回结果

### 人格管理
- 完全独立于 AstrBot 自带的人格系统
- 支持创建、编辑、删除、切换人格
- 每个人格有独立的 System Prompt
- 通过 WebUI 管理

### Tool Calls
- 自动扫描 AstrBot 所有已注册工具（内置 + 插件 + MCP）
- 工具描述写入 System Prompt（增强 AI 理解）
- 同时使用原生 Function Calling
- 兼容 async generator 类型的插件 Tool handler
- 工具调用产生的图片自动以多模态格式嵌入 tool result
- 通过 WebUI 启用/禁用工具

### 分段发送
- 将 LLM 回复按标点符号拆分为多条消息分段发送
- 模拟真人打字节奏
- 支持三种分段模式: `sentence`（按标点）、`newline`（按换行）、`smart`（智能分段，保护对话引号）
- 支持自定义分段正则、最大分段数、发送间隔

### 文本清洗
- 对 LLM 回复进行后处理清洗，去除不需要的内容
- 支持去除 Emoji 表情符号
- 支持去除括号及内容（动作描写、心理活动等）
- 支持清理句尾多余字符，可自定义正则

### WebUI 管理面板
- 独立 aiohttp 服务，可配置端口
- 登录认证: 支持配置用户名和密码，保护管理面板访问
- 人格管理 (CRUD)
- 会话管理 (查看、删除)
- LLM 预览（查看发送给 LLM 的完整上下文、System Prompt、工具列表和 Token 估算）
- 压缩配置
- 用户标识格式配置
- 工具管理
- 记忆管理（查看、搜索）
- 主动回复会话设置

## 安装

1. 将 `astrbot_plugin_chat_engine` 文件夹放入 AstrBot 的 `data/plugins/` 目录
2. 安装依赖:
   ```bash
   pip install sqlalchemy aiosqlite aiohttp
   ```
3. 在 AstrBot 管理面板中启用插件
4. 配置插件参数

## 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `compression_mode` | `turn_limit` | 压缩模式: `turn_limit` / `token` |
| `max_turns` | `50` | 最大保留轮数 (轮数模式) |
| `token_threshold_ratio` | `0.8` | Token 触发阈值比例 |
| `keep_recent_turns` | `5` | 压缩后保留轮数 |
| `fallback_max_context_tokens` | `128000` | 模型最大上下文 Token，自动从 Provider 获取，通常无需手动修改 |
| `user_id_format` | `{{user}{NAME}({ID})}说：` | 用户标识格式 |
| `require_at_in_group` | `true` | 群聊是否需要 @Bot |
| `enable_tool_calls` | `true` | 启用原生 Function Calling |
| `max_tool_rounds` | `10` | 最大工具调用轮数 |
| `enable_passive_record` | `false` | 启用被动记录群聊消息 |
| `enable_split_send` | `false` | 启用分段发送 |
| `split_mode` | `sentence` | 分段模式: `sentence` / `newline` / `smart` |
| `split_pattern` | `[。！？\n]` | 分段匹配符号 (正则) |
| `max_segments` | `5` | 最大分段数 |
| `split_delay_ms` | `800` | 分段发送间隔 (毫秒) |
| `enable_text_clean` | `false` | 启用文本清洗 |
| `clean_emoji` | `true` | 去除 Emoji |
| `clean_brackets` | `true` | 去除括号内容 |
| `clean_trailing_chars` | `true` | 清理句尾字符 |
| `trailing_chars_pattern` | `[~～\\.。!！?？…·•\\-—_\\s]+$` | 句尾清理字符 (正则) |
| `enable_memory` | `true` | 启用记忆功能 |
| `short_term_max_count` | `30` | 短期记忆最大条数 |
| `short_term_max_chars` | `200` | 每条短期记忆最大字符数 |
| `long_term_max_count` | `200` | 长期记忆最大条数 |
| `long_term_retrieval_top_k` | `5` | 长期记忆检索返回条数 |
| `long_term_fetch_k` | `20` | 长期记忆检索候选数 |
| `long_term_enable_rerank` | `true` | 启用长期记忆重排 |
| `long_term_similarity_threshold` | `0.3` | 长期记忆相似度阈值 (0.0-1.0) |
| `memory_summary_interval` | `5` | 自动总结触发轮数 |
| `memory_summary_recent_turns` | `5` | 总结参考最近轮数 |
| `enable_auto_summary` | `true` | 启用自动总结 |
| `enable_proactive` | `false` | 启用主动回复 |
| `proactive_timeout_minutes` | `30` | 超时主动发言分钟数 |
| `proactive_timeout_probability` | `30` | 超时主动发言触发概率 (%)，100 必定触发 |
| `proactive_timeout_max_consecutive` | `2` | 主动回复最大连续次数，0 不限制 |
| `proactive_round_interval` | `0` | N 轮触发回复（仅群聊，0 禁用） |
| `enable_command_execution` | `false` | 启用命令执行，LLM 可执行其他插件命令 |
| `web_port` | `8765` | WebUI 端口 |
| `web_auth_enabled` | `false` | 启用 WebUI 登录认证 |
| `web_username` | `admin` | WebUI 登录用户名 |
| `web_password` | `""` | WebUI 登录密码 |
| `db_type` | `sqlite` | 数据库类型: `sqlite` / `mysql` |
| `mysql_url` | `""` | MySQL 连接 URL |

## 架构

```
astrbot_plugin_chat_engine/
├── main.py                    # 消息拦截 + LLM 调用编排 + 文本清洗 + 记忆/主动回复工具
├── db/                        # 独立数据库层 (SQLAlchemy)
│   ├── models.py              # 数据模型 (独立 MetaData)
│   ├── engine.py              # 数据库引擎
│   ├── session_repo.py        # 会话 CRUD
│   ├── persona_repo.py        # 人格 CRUD
│   ├── tool_config_repo.py    # 工具配置
│   ├── image_repo.py          # 图片 CRUD (sha256 去重)
│   └── image_store.py         # 图片文件存储服务
├── context/                   # 上下文管理
│   ├── manager.py             # 会话 Key / 用户标识 / 压缩触发 / 被动记录 / 会话锁 / 图片解析
│   ├── compressor.py          # 双模式压缩器
│   └── token_counter.py       # Token 估算
├── memory/                    # 记忆系统
│   ├── manager.py             # 记忆管理器 (短期/长期/自动总结/并发锁)
│   ├── short_term.py          # 短期记忆存储
│   ├── vector_store.py        # 长期记忆向量存储 (Embedding + 语义检索)
│   ├── summarizer.py          # 记忆总结器 (LLM 总结)
│   └── tools.py               # 记忆 LLM 工具 (save/search/update/delete)
├── proactive/                 # 主动回复
│   └── manager.py             # 超时发言 / N 轮触发 / 定时回复 / 消息清洗分段
├── persona/                   # 人格管理
│   └── manager.py             # CRUD + 活跃人格
├── tools/                     # 工具扫描与管理
│   ├── scanner.py             # 扫描所有已注册工具
│   ├── manager.py             # 启用/禁用状态
│   └── command_dispatcher.py  # 命令扫描与分发 (LLM 自然语言执行插件命令)
├── utils/                     # 工具函数
│   └── __init__.py            # 通用工具函数 (时间格式化等)
└── web/                       # WebUI
    ├── server.py              # aiohttp REST API (含 LLM 预览 + 记忆管理 + 登录认证)
    └── static/                # 前端 HTML/CSS/JS
```

## 技术细节

- **消息劫持**: 通过 `@filter.event_message_type(ALL, priority=9999)` 拦截所有消息
- **LLM 抑制**: 使用 `event.should_call_llm(True)` 阻止 AstrBot 默认 LLM 流程
- **命令透传**: 检测 `activated_handlers` 中的 `CommandFilter`，命令自动交给其他插件或框架处理
- **独立数据库**: 使用独立 SQLAlchemy MetaData，避免与 AstrBot 全局元数据冲突
- **Provider 复用**: 复用 AstrBot 已配置的 LLM Provider，也支持自定义配置
- **环境信息注入**: System Prompt 前自动注入当前时间和群聊环境信息（群名、Bot 昵称），群聊信息按会话缓存并支持 fallback 短 TTL 策略
- **被动记录**: 未触发回复的群聊消息以 `observed` 角色存储，压缩时归入当前轮次
- **图片存储**: 图片转为 base64 data URL 发送给 LLM，文件按 sha256 去重存储，上下文中以 `image_ref` 引用节省空间
- **历史图片剥离**: 历史上下文中的图片替换为 `[Image]` 文本占位符，仅当前用户消息保留图片，减少 Token 消耗
- **图片查看工具**: LLM 可通过 `view_image` 工具按消息 ID 主动加载历史图片，图片以多模态格式嵌入 tool result
- **引用回复**: 提取 Reply 组件中的发送者和内容，自动拼接到用户消息前缀
- **会话锁**: `asyncio.Lock` 按 session_key 索引，确保同一会话的消息串行处理
- **安全截断**: 调用 LLM 前通过 `TokenEstimator` 检测总量，超出阈值自动裁剪最旧消息
- **消息 ID 注入**: 用户/被动消息内容前自动注入 `[msg:ID]` 标记，为 `reply_with_quote` 工具提供锚点
- **记忆向量检索**: 长期记忆通过 Embedding 向量化存储，支持语义检索和可选 Rerank 重排
- **记忆并发控制**: 自动总结使用会话级 `asyncio.Lock`，后台异步执行不阻塞消息处理
- **动态 Provider 获取**: 记忆管理器通过 Getter 函数在运行时动态获取 Provider，解决加载时序问题
- **命令执行分发**: `CommandDispatcher` 扫描所有已注册命令，生成结构化指引注入 System Prompt，LLM 通过工具调用触发实际执行；自动跳过未激活插件，尊重命令权限定义

## 兼容性

- AstrBot v4.25+
- 平台: QQ OneBot (aiocqhttp) / Telegram (未测试)
- 数据库: SQLite / MySQL
- Python 3.10+

## 状态

本插件目前处于活跃开发阶段，功能仍在持续完善中。如果您在使用过程中遇到问题，或有任何改进建议，欢迎通过 [Issues](../../issues) 提交反馈或直接提交 Pull Request。

## License

MIT
