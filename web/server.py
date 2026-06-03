"""Independent aiohttp Web server for the Chat Engine plugin management UI."""

import json
import traceback
from copy import deepcopy
from pathlib import Path

from aiohttp import web

from astrbot.api import logger


class ChatWebServer:
    """独立 Web 服务器 — 提供 Chat Engine 管理 API 和前端页面"""

    def __init__(self, plugin, port: int = 8765):
        self.plugin = plugin
        self.port = port
        self.app = web.Application()
        self.runner = None
        self.site = None
        self.static_dir = Path(__file__).parent / "static"
        self._setup_routes()
        self._setup_cors()

    def _setup_cors(self):
        """设置 CORS 中间件"""

        @web.middleware
        async def cors_middleware(request, handler):
            if request.method == "OPTIONS":
                response = web.Response()
            else:
                try:
                    response = await handler(request)
                except web.HTTPException as ex:
                    response = ex
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PUT, DELETE, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            return response

        self.app.middlewares.append(cors_middleware)

    def _setup_routes(self):
        """注册所有 API 路由"""
        #  人格管理
        self.app.router.add_get("/api/personas", self._api_list_personas)
        self.app.router.add_post("/api/personas", self._api_create_persona)
        self.app.router.add_put("/api/personas/{id}", self._api_update_persona)
        self.app.router.add_delete("/api/personas/{id}", self._api_delete_persona)
        self.app.router.add_post(
            "/api/personas/{id}/set_default", self._api_set_default_persona
        )

        #  会话管理
        self.app.router.add_get("/api/sessions", self._api_list_sessions)
        self.app.router.add_get(
            "/api/sessions/{key:.*}/llm-preview", self._api_llm_preview
        )
        self.app.router.add_get("/api/sessions/{key:.*}", self._api_get_session)
        self.app.router.add_delete("/api/sessions/{key:.*}", self._api_delete_session)

        #  配置管理
        self.app.router.add_get("/api/config", self._api_get_config)
        self.app.router.add_put("/api/config", self._api_update_config)

        #  工具管理
        self.app.router.add_get("/api/tools", self._api_list_tools)
        self.app.router.add_post("/api/tools/refresh", self._api_refresh_tools)
        self.app.router.add_post("/api/tools/{name}/enable", self._api_enable_tool)
        self.app.router.add_post("/api/tools/{name}/disable", self._api_disable_tool)

        #  记忆管理
        self.app.router.add_get(
            "/api/memories/{key:.*}/short", self._api_list_short_term_memories
        )
        self.app.router.add_get(
            "/api/memories/{key:.*}/long", self._api_list_long_term_memories
        )
        self.app.router.add_post(
            "/api/memories/{key:.*}/short", self._api_add_short_term_memory
        )
        self.app.router.add_post(
            "/api/memories/{key:.*}/long", self._api_add_long_term_memory
        )
        self.app.router.add_put(
            "/api/memories/{key:.*}/short/{id}", self._api_update_short_term_memory
        )
        self.app.router.add_put(
            "/api/memories/{key:.*}/long/{id}", self._api_update_long_term_memory
        )
        self.app.router.add_delete(
            "/api/memories/{key:.*}/short/{id}", self._api_delete_short_term_memory
        )
        self.app.router.add_delete(
            "/api/memories/{key:.*}/long/{id}", self._api_delete_long_term_memory
        )

        #  前端页面
        self.app.router.add_get("/", self._serve_index)
        self.app.router.add_get("/{filename}", self._serve_static)

    async def start(self):
        """启动 Web 服务器"""
        try:
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, "0.0.0.0", self.port)
            await self.site.start()
            logger.info(f"[ChatEngine] WebUI 已启动: http://0.0.0.0:{self.port}")
        except Exception as e:
            logger.error(f"[ChatEngine] WebUI 启动失败: {e}")

    async def stop(self):
        """停止 Web 服务器"""
        if self.runner:
            await self.runner.cleanup()
            logger.info("[ChatEngine] WebUI 已关闭")

    # 人格 API

    async def _api_list_personas(self, request: web.Request) -> web.Response:
        personas = await self.plugin.persona_mgr.list_personas()
        return web.json_response(
            {
                "personas": [
                    {
                        "id": p.id,
                        "name": p.name,
                        "system_prompt": p.system_prompt,
                        "is_default": p.is_default,
                        "created_at": p.created_at.isoformat()
                        if p.created_at
                        else None,
                        "updated_at": p.updated_at.isoformat()
                        if p.updated_at
                        else None,
                    }
                    for p in personas
                ]
            }
        )

    async def _api_create_persona(self, request: web.Request) -> web.Response:
        data = await request.json()
        name = data.get("name", "").strip()
        if not name:
            return web.json_response({"error": "名称不能为空"}, status=400)

        system_prompt = data.get("system_prompt", "")
        is_default = data.get("is_default", False)

        try:
            persona = await self.plugin.persona_mgr.create_persona(
                name, system_prompt, is_default
            )
            return web.json_response(
                {
                    "persona": {
                        "id": persona.id,
                        "name": persona.name,
                        "system_prompt": persona.system_prompt,
                        "is_default": persona.is_default,
                    }
                }
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def _api_update_persona(self, request: web.Request) -> web.Response:
        persona_id = int(request.match_info["id"])
        data = await request.json()

        kwargs = {}
        if "name" in data:
            kwargs["name"] = data["name"]
        if "system_prompt" in data:
            kwargs["system_prompt"] = data["system_prompt"]
        if "is_default" in data:
            kwargs["is_default"] = data["is_default"]

        persona = await self.plugin.persona_mgr.update_persona(persona_id, **kwargs)
        if persona is None:
            return web.json_response({"error": "人格不存在"}, status=404)

        return web.json_response(
            {
                "persona": {
                    "id": persona.id,
                    "name": persona.name,
                    "system_prompt": persona.system_prompt,
                    "is_default": persona.is_default,
                }
            }
        )

    async def _api_delete_persona(self, request: web.Request) -> web.Response:
        persona_id = int(request.match_info["id"])
        ok = await self.plugin.persona_mgr.delete_persona(persona_id)
        if not ok:
            return web.json_response({"error": "人格不存在"}, status=404)
        return web.json_response({"ok": True})

    async def _api_set_default_persona(self, request: web.Request) -> web.Response:
        persona_id = int(request.match_info["id"])
        ok = await self.plugin.persona_mgr.set_default(persona_id)
        if not ok:
            return web.json_response({"error": "人格不存在"}, status=404)
        return web.json_response({"ok": True})

    # 会话 API

    async def _api_list_sessions(self, request: web.Request) -> web.Response:
        page = int(request.query.get("page", 1))
        page_size = int(request.query.get("page_size", 50))
        sessions, total = await self.plugin.context_mgr.repo.list_sessions(
            page, page_size
        )
        return web.json_response(
            {
                "sessions": sessions,
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )

    async def _api_get_session(self, request: web.Request) -> web.Response:
        session_key = request.match_info["key"]
        messages = await self.plugin.context_mgr.load_context(session_key)
        return web.json_response(
            {
                "session_key": session_key,
                "messages": messages,
            }
        )

    async def _api_delete_session(self, request: web.Request) -> web.Response:
        session_key = request.match_info["key"]
        ok = await self.plugin.context_mgr.repo.delete_session(session_key)
        if not ok:
            return web.json_response({"error": "会话不存在"}, status=404)
        # 记忆不随会话删除，保留以供后续会话复用
        return web.json_response({"ok": True})

    async def _api_llm_preview(self, request: web.Request) -> web.Response:
        """模拟构建 LLM 调用上下文，返回完整预览数据。

        展示加载后经过所有处理（observed→user、图片解析、模态过滤等）
        的最终上下文，以及 system prompt、工具列表和 token 估算。
        """
        try:
            from astrbot.core.provider.modalities import (
                sanitize_contexts_by_modalities,
            )

            session_key = request.match_info["key"]

            # 加载并解析上下文
            raw_messages = await self.plugin.context_mgr.load_context(session_key)

            # observed → user（模拟 handle_all_messages 的逻辑）
            contexts = []
            for msg in raw_messages:
                if msg.get("role") == "observed":
                    contexts.append({**msg, "role": "user"})
                else:
                    contexts.append(msg)

            # 模态过滤
            modalities = []
            provider = None
            try:
                provider = self.plugin.context.get_using_provider()
                modalities = await self.plugin.context_mgr.get_modalities(provider)
            except Exception:
                pass

            filtered_contexts = contexts
            stats = None
            if modalities:
                filtered_contexts, stats = sanitize_contexts_by_modalities(
                    deepcopy(contexts), modalities
                )

            # System prompt
            system_prompt = ""
            try:
                system_prompt = await self.plugin.persona_mgr.get_system_prompt()
            except Exception:
                pass

            # Token 估算
            from ..context.token_counter import TokenEstimator

            estimator = TokenEstimator()
            estimated_tokens = estimator.count_messages_tokens(filtered_contexts)
            if system_prompt:
                estimated_tokens += estimator._estimate_text(system_prompt)

            # 工具列表
            tool_names = []
            try:
                tool_names = sorted(await self.plugin.tool_mgr.get_enabled_names())
            except Exception:
                pass

            # 模态过滤摘要
            filter_summary = None
            if stats and stats.changed:
                filter_summary = {
                    "fixed_image_blocks": stats.fixed_image_blocks,
                    "fixed_audio_blocks": stats.fixed_audio_blocks,
                    "fixed_tool_messages": stats.fixed_tool_messages,
                    "removed_tool_calls": stats.removed_tool_calls,
                }

            result = {
                "session_key": session_key,
                "provider": (
                    provider.meta().id
                    if provider and hasattr(provider, "meta")
                    else None
                ),
                "modalities": modalities,
                "system_prompt": system_prompt,
                "system_prompt_length": len(system_prompt),
                "contexts": filtered_contexts,
                "context_count": len(filtered_contexts),
                "tools": tool_names,
                "tool_count": len(tool_names),
                "estimated_tokens": estimated_tokens,
                "filter_summary": filter_summary,
            }
            body = json.dumps(result, ensure_ascii=False, default=str)
            return web.Response(body=body, content_type="application/json")
        except Exception as e:
            logger.error(f"LLM 预览异常: {e}\n{traceback.format_exc()}")
            return web.Response(
                body=json.dumps({"error": str(e)}, ensure_ascii=False, default=str),
                content_type="application/json",
                status=500,
            )

    # 配置 API

    async def _api_get_config(self, request: web.Request) -> web.Response:
        config_keys = [
            "compression_mode",
            "max_turns",
            "token_threshold_ratio",
            "keep_recent_turns",
            "fallback_max_context_tokens",
            "user_id_format",
            "require_at_in_group",
            "web_port",
            "enable_tool_calls",
            "max_tool_rounds",
            "db_type",
            "mysql_url",
            "enable_passive_record",
            "enable_split_send",
            "split_mode",
            "split_pattern",
            "max_segments",
            "split_delay_ms",
            "enable_text_clean",
            "clean_emoji",
            "clean_brackets",
            "clean_trailing_chars",
            "trailing_chars_pattern",
            "enable_memory",
            "short_term_max_count",
            "short_term_max_chars",
            "long_term_max_count",
            "long_term_retrieval_top_k",
            "long_term_fetch_k",
            "long_term_enable_rerank",
            "long_term_similarity_threshold",
            "memory_summary_interval",
            "memory_summary_recent_turns",
            "enable_auto_summary",
        ]
        config_data = {}
        for key in config_keys:
            config_data[key] = self.plugin.config.get(key)
        return web.json_response(config_data)

    async def _api_update_config(self, request: web.Request) -> web.Response:
        data = await request.json()
        allowed_keys = [
            "compression_mode",
            "max_turns",
            "token_threshold_ratio",
            "keep_recent_turns",
            "fallback_max_context_tokens",
            "user_id_format",
            "require_at_in_group",
            "enable_tool_calls",
            "max_tool_rounds",
            "enable_passive_record",
            "enable_split_send",
            "split_mode",
            "split_pattern",
            "max_segments",
            "split_delay_ms",
            "enable_text_clean",
            "clean_emoji",
            "clean_brackets",
            "clean_trailing_chars",
            "trailing_chars_pattern",
            "enable_memory",
            "short_term_max_count",
            "short_term_max_chars",
            "long_term_max_count",
            "long_term_retrieval_top_k",
            "long_term_fetch_k",
            "long_term_enable_rerank",
            "long_term_similarity_threshold",
            "memory_summary_interval",
            "memory_summary_recent_turns",
            "enable_auto_summary",
        ]
        for key in allowed_keys:
            if key in data:
                self.plugin.config[key] = data[key]

        # 保存配置
        try:
            self.plugin.config.save_config()
        except Exception:
            pass

        # 重载压缩器
        self.plugin.context_mgr.config = self.plugin.config
        self.plugin.context_mgr.reload_compressor()

        return web.json_response({"ok": True})

    # 工具 API

    async def _api_list_tools(self, request: web.Request) -> web.Response:
        tools = await self.plugin.tool_mgr.get_all_tools_info()
        return web.json_response({"tools": tools})

    async def _api_refresh_tools(self, request: web.Request) -> web.Response:
        tools = await self.plugin.tool_mgr.refresh_tools()
        return web.json_response({"tools": tools, "count": len(tools)})

    async def _api_enable_tool(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        await self.plugin.tool_mgr.enable_tool(name)
        return web.json_response({"ok": True})

    async def _api_disable_tool(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        await self.plugin.tool_mgr.disable_tool(name)
        return web.json_response({"ok": True})

    # 记忆 API

    def _get_memory_mgr(self):
        """获取记忆管理器，未初始化时返回 None"""
        return getattr(self.plugin, "memory_mgr", None)

    async def _api_list_short_term_memories(
        self, request: web.Request
    ) -> web.Response:
        mgr = self._get_memory_mgr()
        if not mgr:
            return web.json_response({"memories": []})
        session_key = request.match_info["key"]
        try:
            memories = await mgr.list_short_term(session_key)
            return web.json_response({"memories": memories})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _api_list_long_term_memories(
        self, request: web.Request
    ) -> web.Response:
        mgr = self._get_memory_mgr()
        if not mgr:
            return web.json_response({"memories": []})
        session_key = request.match_info["key"]
        try:
            memories = await mgr.list_long_term(session_key)
            return web.json_response({"memories": memories})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _api_add_short_term_memory(
        self, request: web.Request
    ) -> web.Response:
        mgr = self._get_memory_mgr()
        if not mgr:
            return web.json_response({"error": "记忆系统未启用"}, status=400)
        session_key = request.match_info["key"]
        data = await request.json()
        content = data.get("content", "").strip()
        if not content:
            return web.json_response({"error": "内容不能为空"}, status=400)
        try:
            mid = await mgr.save_memory(
                session_key, content, "short_term", source="manual"
            )
            return web.json_response({"ok": True, "id": mid})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _api_add_long_term_memory(
        self, request: web.Request
    ) -> web.Response:
        mgr = self._get_memory_mgr()
        if not mgr:
            return web.json_response({"error": "记忆系统未启用"}, status=400)
        session_key = request.match_info["key"]
        data = await request.json()
        content = data.get("content", "").strip()
        if not content:
            return web.json_response({"error": "内容不能为空"}, status=400)
        try:
            mid = await mgr.save_memory(
                session_key, content, "long_term", source="manual"
            )
            return web.json_response({"ok": True, "id": mid})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _api_update_short_term_memory(
        self, request: web.Request
    ) -> web.Response:
        mgr = self._get_memory_mgr()
        if not mgr:
            return web.json_response({"error": "记忆系统未启用"}, status=400)
        session_key = request.match_info["key"]
        mem_id = request.match_info["id"]
        data = await request.json()
        content = data.get("content", "").strip()
        if not content:
            return web.json_response({"error": "内容不能为空"}, status=400)
        try:
            ok = await mgr.update_memory(session_key, mem_id, content)
            return web.json_response({"ok": ok})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _api_update_long_term_memory(
        self, request: web.Request
    ) -> web.Response:
        mgr = self._get_memory_mgr()
        if not mgr:
            return web.json_response({"error": "记忆系统未启用"}, status=400)
        session_key = request.match_info["key"]
        mem_id = request.match_info["id"]
        data = await request.json()
        content = data.get("content", "").strip()
        if not content:
            return web.json_response({"error": "内容不能为空"}, status=400)
        try:
            ok = await mgr.update_memory(session_key, mem_id, content)
            return web.json_response({"ok": ok})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _api_delete_short_term_memory(
        self, request: web.Request
    ) -> web.Response:
        mgr = self._get_memory_mgr()
        if not mgr:
            return web.json_response({"error": "记忆系统未启用"}, status=400)
        session_key = request.match_info["key"]
        mem_id = request.match_info["id"]
        try:
            ok = await mgr.delete_memory(session_key, mem_id, "short_term")
            if not ok:
                return web.json_response({"error": "记忆不存在"}, status=404)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _api_delete_long_term_memory(
        self, request: web.Request
    ) -> web.Response:
        mgr = self._get_memory_mgr()
        if not mgr:
            return web.json_response({"error": "记忆系统未启用"}, status=400)
        session_key = request.match_info["key"]
        mem_id = request.match_info["id"]
        try:
            ok = await mgr.delete_memory(session_key, mem_id, "long_term")
            if not ok:
                return web.json_response({"error": "记忆不存在"}, status=404)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # 静态文件

    async def _serve_index(self, request: web.Request) -> web.Response:
        return await self._serve_file("index.html")

    async def _serve_static(self, request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        return await self._serve_file(filename)

    async def _serve_file(self, filename: str) -> web.Response:
        filepath = self.static_dir / filename
        if not filepath.exists():
            return web.Response(status=404, text="Not Found")

        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }
        suffix = filepath.suffix
        content_type = content_types.get(suffix, "application/octet-stream")

        return web.FileResponse(filepath, headers={"Content-Type": content_type})
