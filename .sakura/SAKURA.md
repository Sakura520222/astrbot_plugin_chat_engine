# 项目概述：astrbot_plugin_chat_engine

## 1. 项目简介

这是一个为 AstrBot 框架开发的聊天增强插件，旨在完全替代其内置的聊天功能，提供独立、可配置的对话上下文管理、多用户识别、人格系统及工具调用能力。

## 2. 技术栈

*   **核心语言**：Python（主逻辑）、JavaScript/HTML/CSS（WebUI前端）
*   **框架与库**：
    *   **数据库**：SQLAlchemy (ORM)、aiosqlite / aiosqlite (异步SQLite)、MySQL
    *   **Web服务**：aiohttp (用于提供独立的管理界面REST API)
    *   **异步架构**：基于 asyncio 的异步I/O设计
*   **集成目标**：AstrBot v4.25+，支持 QQ OneBot (aiocqhttp) 和 Telegram 等平台。

## 3. 项目结构

核心模块与目录职责如下：

*   `main.py`：插件入口。负责消息拦截、LLM调用编排以及协调各功能模块。
*   `db/`：独立的数据持久化层。
    *   `models.py`：定义人格、会话、工具配置等数据模型。
    *   `engine.py`：管理数据库引擎与会话。
    *   `session_repo.py`, `persona_repo.py`, `tool_config_repo.py`：提供各数据模型的CRUD操作。
*   `context/`：核心的上下文管理模块。
    *   `manager.py`：管理会话键（群聊/私聊隔离）、用户标识格式化以及触发上下文压缩。
    *   `compressor.py`：实现“轮数限制”和“Token阈值”两种上下文压缩策略。
    *   `token_counter.py`：估算文本的Token数量，为压缩策略提供依据。
*   `persona/`：独立的人格（System Prompt）管理模块。
    *   `manager.py`：处理人格的增删改查及当前活跃人格的切换。
*   `tools/`：工具调用集成模块。
    *   `scanner.py`：扫描并获取AstrBot已注册的所有可用工具（内置、插件、MCP）。
    *   `manager.py`：管理工具的启用/禁用状态。
*   `web/`：独立的Web管理面板。
    *   `server.py`：基于aiohttp的REST API服务端。
    *   `static/`：存放前端HTML、CSS和JavaScript文件。

## 4. 开发约定（推断）

*   **模块化与独立性**：插件作为一个独立单元开发，拥有自己独立的数据库MetaData，避免与AstrBot宿主框架的全局模型产生冲突。
*   **异步优先**：核心组件（Web服务、数据库操作）广泛采用异步（`async/await`）设计，以适应AstrBot的事件驱动和并发消息处理场景。
*   **配置驱动**：通过`_conf_schema.json`和`metadata.yaml`定义大量可配置项（如压缩模式、WebUI端口、用户标识格式），行为高度可定制。
*   **分层架构**：清晰地分为数据层（`db`）、业务逻辑层（`context`, `persona`, `tools`）、接口层（`main.py`消息处理, `web/` REST API）。
*   **事件钩子集成**：通过AstrBot提供的`@filter`装饰器和事件接口（如`event.should_call_llm(True)`）来拦截消息流并注入自定义逻辑，实现与宿主框架的深度集成。