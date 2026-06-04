# Chat Engine - AstrBot 聊天增强插件

完全替代 AstrBot 自带聊天功能，独立实现上下文管理、用户识别、人格系统、Tool Calls、上下文压缩和 WebUI 管理面板。

## 功能特性

### 用户识别
- 群聊和私聊中每条消息自动添加 `{{user}{昵称}({ID})}说：` 前缀
- 帮助 AI 在上下文中准确区分不同用户
- 用户标识格式可通过 WebUI 自定义

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
- 人格管理 (CRUD)
- 会话管理 (查看、删除)
- LLM 预览（查看发送给 LLM 的完整上下文、System Prompt、工具列表和 Token 估算）
- 压缩配置
- 用户标识格式配置
- 工具管理

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
| `web_port` | `8765` | WebUI 端口 |
| `db_type` | `sqlite` | 数据库类型: `sqlite` / `mysql` |
| `mysql_url` | `""` | MySQL 连接 URL |

## 架构

```
astrbot_plugin_chat_engine/
├── main.py                    # 消息拦截 + LLM 调用编排 + 文本清洗
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
├── persona/                   # 人格管理
│   └── manager.py             # CRUD + 活跃人格
├── tools/                     # 工具扫描与管理
│   ├── scanner.py             # 扫描所有已注册工具
│   └── manager.py             # 启用/禁用状态
└── web/                       # WebUI
    ├── server.py              # aiohttp REST API (含 LLM 预览)
    └── static/                # 前端 HTML/CSS/JS
```

## 技术细节

- **消息劫持**: 通过 `@filter.event_message_type(ALL, priority=9999)` 拦截所有消息
- **LLM 抑制**: 使用 `event.should_call_llm(True)` 阻止 AstrBot 默认 LLM 流程
- **命令透传**: 检测 `activated_handlers` 中的 `CommandFilter`，命令自动交给其他插件或框架处理
- **独立数据库**: 使用独立 SQLAlchemy MetaData，避免与 AstrBot 全局元数据冲突
- **Provider 复用**: 复用 AstrBot 已配置的 LLM Provider，也支持自定义配置
- **被动记录**: 未触发回复的群聊消息以 `observed` 角色存储，压缩时归入当前轮次
- **图片存储**: 图片转为 base64 data URL 发送给 LLM，文件按 sha256 去重存储，上下文中以 `image_ref` 引用节省空间
- **引用回复**: 提取 Reply 组件中的发送者和内容，自动拼接到用户消息前缀
- **会话锁**: `asyncio.Lock` 按 session_key 索引，确保同一会话的消息串行处理
- **安全截断**: 调用 LLM 前通过 `TokenEstimator` 检测总量，超出阈值自动裁剪最旧消息

## 兼容性

- AstrBot v4.25+
- 平台: QQ OneBot (aiocqhttp) / Telegram (未测试)
- 数据库: SQLite / MySQL
- Python 3.10+

## 状态

⚠️ **本插件目前处于初始开发测试阶段**，功能仍在持续完善中。如果您在使用过程中遇到问题，或有任何改进建议，欢迎通过 [Issues](../../issues) 提交反馈或直接提交 Pull Request。

## License

MIT
