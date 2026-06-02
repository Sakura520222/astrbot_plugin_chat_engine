# 变更日志

所有重要变更均记录在此文件中。

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
