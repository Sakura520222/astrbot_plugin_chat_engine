"""Chat Engine 插件 Web API — 通过 AstrBot 插件 Pages bridge 暴露。

handler 通过 register_web_api 注册,请求经 Dashboard 同源转发,
认证继承 Dashboard JWT。请求读取用 astrbot.api.web.request 代理,
响应用 dict / error_response。
"""

import json
import traceback

from astrbot.api import logger
from astrbot.api.web import error_response, request

from ..utils import shanghai_now_iso
from ..utils.config import cfg_bool


class ChatWebAPI:
    """插件 Pages Web API — 提供管理端点的注册与全部 handler"""

    # 与 _conf_schema.json 保持一致的全量配置键（GET/PUT 共用，避免两处维护）
    _CONFIG_KEYS = [
        # 上下文与压缩
        "compression_mode",
        "max_turns",
        "token_threshold_ratio",
        "keep_recent_turns",
        "fallback_max_context_tokens",
        "user_id_format",
        "require_at_in_group",
        # 工具与命令
        "enable_tool_calls",
        "max_tool_rounds",
        "broadcast_llm_hooks",
        "enable_command_execution",
        # 数据库
        "db_type",
        "mysql_url",
        # 被动记录
        "enable_passive_record",
        # 分段发送
        "enable_split_send",
        "split_mode",
        "split_pattern",
        "max_segments",
        "split_delay_ms",
        # 文本清洗
        "enable_text_clean",
        "clean_emoji",
        "clean_brackets",
        "clean_trailing_chars",
        "trailing_chars_pattern",
        # 记忆
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
        # 主动回复
        "enable_proactive",
        "proactive_timeout_minutes",
        "proactive_timeout_probability",
        "proactive_timeout_max_consecutive",
        "proactive_round_interval",
        "proactive_ai_judge_interval",
        "proactive_ai_judge_cooldown",
        "proactive_ai_judge_context_messages",
        "proactive_ai_judge_window_ms",
        # 消息抖动
        "enable_message_debounce",
        "debounce_window_ms",
        "debounce_max_messages",
        "debounce_scope",
        "debounce_merge_mode",
        "debounce_separator",
        "debounce_absorb_passive",
        # 并发消息引导
        "enable_inflight_guidance",
        "max_inflight_guidance_messages",
        "max_inflight_followup_rounds",
        # 画图 (OpenAI 兼容)
        "enable_image_generation",
        "image_gen_api_base",
        "image_gen_api_key",
        "image_gen_model",
        "image_gen_size",
        "image_gen_quality",
        "image_gen_timeout",
        "image_gen_admin_only",
        "image_gen_quota_dimension",
        "image_gen_daily_quota",
    ]

    # 敏感凭证：GET 不回显真实值（仅返回是否已设置），PUT 空值表示不修改
    _SENSITIVE_KEYS = frozenset({"image_gen_api_key"})

    def __init__(self, plugin):
        self.plugin = plugin

    def _get_memory_mgr(self):
        """获取记忆管理器，未初始化时返回 None"""
        return getattr(self.plugin, "memory_mgr", None)

    def _get_proactive_mgr(self):
        """获取主动回复管理器"""
        return getattr(self.plugin, "proactive_mgr", None)

    # --- 图片配额 API ---

    async def list_image_quota(self):
        """列出指定日期(默认今日)所有画图/改图配额用量记录。

        返回当前配额相关配置(daily_quota / dimension / admin_only)供前端展示。
        """
        date = request.query.get("date") or shanghai_now_iso()[:10]
        try:
            records = await self.plugin.db.image_quota_repo.list_today(date)
        except Exception as e:
            return error_response(str(e), status_code=500)

        items = []
        for r in records:
            key = r.quota_key
            if key.startswith("user:"):
                dimension, identifier = "user", key[len("user:") :]
            elif key.startswith("session:"):
                dimension, identifier = "session", key[len("session:") :]
            else:
                dimension, identifier = "unknown", key
            items.append(
                {
                    "quota_key": key,
                    "dimension": dimension,
                    "identifier": identifier,
                    "date": r.date,
                    "used_count": r.used_count,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
            )
        return {
            "date": date,
            "items": items,
            "daily_quota": self.plugin.config.get("image_gen_daily_quota", 10),
            "dimension": self.plugin.config.get("image_gen_quota_dimension", "user"),
            "admin_only": cfg_bool(self.plugin.config, "image_gen_admin_only", True),
        }

    async def reset_image_quota(self):
        """重置指定 quota_key 当日配额(删行,用量归零)。

        请求体: {"quota_key": "user:123", "date": "2026-07-07"(可选,默认今日)}
        """
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        quota_key = str(data.get("quota_key", "")).strip()
        if not quota_key:
            return error_response("quota_key 不能为空", status_code=400)
        date = str(data.get("date", "")).strip() or shanghai_now_iso()[:10]
        try:
            ok = await self.plugin.db.image_quota_repo.reset(quota_key, date)
        except Exception as e:
            return error_response(str(e), status_code=500)
        if not ok:
            return error_response("指定记录不存在", status_code=404)
        return {"ok": True}

    # --- 人格 API ---

    async def list_personas(self):
        personas = await self.plugin.persona_mgr.list_personas()
        return {
            "personas": [
                {
                    "id": p.id,
                    "name": p.name,
                    "system_prompt": p.system_prompt,
                    "is_default": p.is_default,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                    "updated_at": p.updated_at.isoformat() if p.updated_at else None,
                }
                for p in personas
            ]
        }

    async def create_persona(self):
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        name = data.get("name", "").strip()
        if not name:
            return error_response("名称不能为空", status_code=400)

        system_prompt = data.get("system_prompt", "")
        is_default = data.get("is_default", False)

        try:
            persona = await self.plugin.persona_mgr.create_persona(
                name, system_prompt, is_default
            )
            return {
                "persona": {
                    "id": persona.id,
                    "name": persona.name,
                    "system_prompt": persona.system_prompt,
                    "is_default": persona.is_default,
                }
            }
        except Exception as e:
            return error_response(str(e), status_code=400)

    async def update_persona(self, id):
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        try:
            persona_id = int(id)
        except (ValueError, TypeError):
            return error_response("无效的人格 ID", status_code=400)

        kwargs = {}
        if "name" in data:
            kwargs["name"] = data["name"]
        if "system_prompt" in data:
            kwargs["system_prompt"] = data["system_prompt"]
        if "is_default" in data:
            kwargs["is_default"] = data["is_default"]

        persona = await self.plugin.persona_mgr.update_persona(persona_id, **kwargs)
        if persona is None:
            return error_response("人格不存在", status_code=404)

        return {
            "persona": {
                "id": persona.id,
                "name": persona.name,
                "system_prompt": persona.system_prompt,
                "is_default": persona.is_default,
            }
        }

    async def delete_persona(self, id):
        try:
            persona_id = int(id)
        except (ValueError, TypeError):
            return error_response("无效的人格 ID", status_code=400)
        ok = await self.plugin.persona_mgr.delete_persona(persona_id)
        if not ok:
            return error_response("人格不存在", status_code=404)
        return {"ok": True}

    async def set_default_persona(self, id):
        try:
            persona_id = int(id)
        except (ValueError, TypeError):
            return error_response("无效的人格 ID", status_code=400)
        ok = await self.plugin.persona_mgr.set_default(persona_id)
        if not ok:
            return error_response("人格不存在", status_code=404)
        return {"ok": True}

    # --- 会话 API ---

    async def list_sessions(self):
        page = int(request.query.get("page", 1))
        page_size = int(request.query.get("page_size", 50))
        sessions, total = await self.plugin.context_mgr.repo.list_sessions(
            page, page_size
        )
        # 批量查询归档数量（单次 GROUP BY 查询，避免 N+1）
        session_keys = [s["session_key"] for s in sessions]
        try:
            archive_counts = (
                await self.plugin.db.archived_session_repo.count_by_session_keys(
                    session_keys
                )
            )
        except Exception:
            archive_counts = {}
        for s in sessions:
            s["archive_count"] = archive_counts.get(s["session_key"], 0)
        return {
            "sessions": sessions,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    async def get_session(self, key):
        messages = await self.plugin.context_mgr.load_context(key)
        (
            prompt_tokens,
            completion_tokens,
        ) = await self.plugin.context_mgr.repo.get_token_usage(key)
        return {
            "session_key": key,
            "messages": messages,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    async def clear_session(self, key):
        """清空指定会话的上下文并归零 Token 计数（保留会话行，对应 /clear）。"""
        await self.plugin.context_mgr.inflight_mgr.reset_session(key)
        async with self.plugin.context_mgr.get_session_lock(key):
            cleared = await self.plugin.context_mgr.repo.clear_session(key)
        return {"ok": True, "cleared_messages": cleared}

    async def delete_session(self, key):
        await self.plugin.context_mgr.inflight_mgr.reset_session(key)
        ok = await self.plugin.context_mgr.repo.delete_session(key)
        if not ok:
            return error_response("会话不存在", status_code=404)
        # 记忆不随会话删除，保留以供后续会话复用
        return {"ok": True}

    async def llm_preview(self, key):
        """模拟构建 LLM 调用上下文，返回完整预览数据。

        展示加载后经过所有处理（observed→user、图片解析、模态过滤等）
        的最终上下文，以及 system prompt、工具列表和 token 估算。
        """
        try:
            from astrbot.core.provider.modalities import (
                sanitize_contexts_by_modalities,
            )

            # 加载并解析上下文
            raw_messages = await self.plugin.context_mgr.load_context(key)

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

            from copy import deepcopy

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
                "session_key": key,
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
            return result
        except Exception as e:
            logger.error(f"LLM 预览异常: {e}\n{traceback.format_exc()}")
            return error_response(str(e), status_code=500)

    # --- 归档会话 API ---

    async def list_archives(self, key):
        """列出指定会话的所有归档"""
        archives = await self.plugin.db.archived_session_repo.list_by_session_key(key)
        return {
            "archives": [
                {
                    "id": a.id,
                    "title": a.title or "未命名",
                    "message_count": a.message_count,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                    "updated_at": a.updated_at.isoformat() if a.updated_at else None,
                }
                for a in archives
            ]
        }

    async def get_archive(self, key, id):
        """查看归档会话的完整上下文"""
        try:
            archive_id = int(id)
        except (ValueError, TypeError):
            return error_response("无效的归档 ID", status_code=400)
        archive = await self.plugin.db.archived_session_repo.get_by_id(archive_id)
        if not archive:
            return error_response("归档不存在", status_code=404)
        if archive.session_key != key:
            return error_response("归档不属于此会话", status_code=403)

        try:
            messages = json.loads(archive.messages_json)
        except (json.JSONDecodeError, TypeError):
            return error_response("归档消息数据损坏，无法解析", status_code=500)
        # 解析 image_ref 为 data URL 以便前端显示
        if self.plugin.context_mgr.image_store:
            messages = await self.plugin.context_mgr._resolve_images_for_messages(
                messages
            )

        return {
            "id": archive.id,
            "session_key": archive.session_key,
            "title": archive.title or "未命名",
            "message_count": archive.message_count,
            "messages": messages,
            "created_at": archive.created_at.isoformat()
            if archive.created_at
            else None,
            "updated_at": archive.updated_at.isoformat()
            if archive.updated_at
            else None,
        }

    async def restore_archive(self, key, id):
        """恢复归档会话到活跃状态（WebUI 版本，不走 LLM 标题生成）"""
        try:
            archive_id = int(id)
        except (ValueError, TypeError):
            return error_response("无效的归档 ID", status_code=400)

        async with self.plugin.context_mgr.get_session_lock(key):
            archive = await self.plugin.db.archived_session_repo.get_by_id(archive_id)
            if not archive:
                return error_response("归档不存在", status_code=404)
            if archive.session_key != key:
                return error_response("归档不属于此会话", status_code=403)

            # 先验证归档 JSON 完整性，再做不可逆操作
            try:
                target_messages = json.loads(archive.messages_json)
            except (json.JSONDecodeError, TypeError):
                return error_response("归档消息数据损坏，无法恢复", status_code=500)

            # 归档当前上下文（使用时间戳标题，不调用 LLM）
            raw_current = await self.plugin.context_mgr.repo.get_context(key)
            if raw_current:
                from ..utils import shanghai_now_iso

                fallback_title = (
                    f"自动归档 ({shanghai_now_iso()[:16].replace('T', ' ')})"
                )
                (
                    cur_prompt,
                    cur_completion,
                ) = await self.plugin.context_mgr.repo.get_token_usage(key)
                await self.plugin.db.archived_session_repo.archive(
                    key,
                    fallback_title,
                    raw_current,
                    prompt_tokens=cur_prompt,
                    completion_tokens=cur_completion,
                )

            # 恢复归档上下文
            target_messages = await self.plugin.context_mgr._store_images_for_messages(
                target_messages
            )
            await self.plugin.context_mgr.repo.save_context(key, target_messages)

            # 恢复归档的 Token 计数快照
            await self.plugin.context_mgr.repo.set_token_usage(
                key,
                archive.prompt_tokens or 0,
                archive.completion_tokens or 0,
            )

            # 删除已恢复的归档
            await self.plugin.db.archived_session_repo.delete(archive_id)

            await self.plugin.context_mgr.inflight_mgr.reset_session(key)

        return {"ok": True, "title": archive.title or "未命名"}

    async def delete_archive(self, key, id):
        """删除归档会话"""
        try:
            archive_id = int(id)
        except (ValueError, TypeError):
            return error_response("无效的归档 ID", status_code=400)

        async with self.plugin.context_mgr.get_session_lock(key):
            archive = await self.plugin.db.archived_session_repo.get_by_id(archive_id)
            if not archive:
                return error_response("归档不存在", status_code=404)
            if archive.session_key != key:
                return error_response("归档不属于此会话", status_code=403)
            ok = await self.plugin.db.archived_session_repo.delete(archive_id)
            if not ok:
                return error_response("归档不存在", status_code=404)
            return {"ok": True}

    # --- 配置 API ---

    async def get_config(self):
        config_data = {}
        for key in self._CONFIG_KEYS:
            if key in self._SENSITIVE_KEYS:
                config_data[key] = ""
                config_data[f"{key}_set"] = bool(self.plugin.config.get(key))
            else:
                config_data[key] = self.plugin.config.get(key)
        return config_data

    async def update_config(self):
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        for key in self._CONFIG_KEYS:
            if key not in data:
                continue
            if key in self._SENSITIVE_KEYS and not str(data.get(key, "")).strip():
                continue
            self.plugin.config[key] = data[key]

        # 保存配置
        try:
            self.plugin.config.save_config()
        except Exception:
            pass

        # 重载压缩器
        self.plugin.context_mgr.config = self.plugin.config
        self.plugin.context_mgr.reload_compressor()

        # 重载消息抖动管理器
        from ..debounce.manager import MessageDebouncer
        from ..utils.config import cfg_bool as _cfg_bool

        debounce_enabled = _cfg_bool(
            self.plugin.config, "enable_message_debounce", False
        )
        if debounce_enabled:
            if self.plugin.debouncer:
                # 确保 debouncer 使用最新 config 引用
                self.plugin.debouncer.config = self.plugin.config
            else:
                try:
                    self.plugin.debouncer = MessageDebouncer(
                        config=self.plugin.config,
                        process_fn=self.plugin._process_debounced_messages,
                    )
                    logger.info("[ChatEngine] 消息抖动已通过 WebUI 启用")
                except Exception as e:
                    logger.warning(f"[ChatEngine] 消息抖动初始化失败: {e}")
        elif not debounce_enabled and self.plugin.debouncer:
            try:
                # close() 内部会先 flush_all() 再等待进行中的任务完成
                await self.plugin.debouncer.close()
            except Exception:
                pass
            self.plugin.debouncer = None
            logger.info("[ChatEngine] 消息抖动已通过 WebUI 禁用")

        return {"ok": True}

    # --- 工具 API ---

    async def list_tools(self):
        tools = await self.plugin.tool_mgr.get_all_tools_info()
        return {"tools": tools}

    async def refresh_tools(self):
        tools = await self.plugin.tool_mgr.refresh_tools()
        return {"tools": tools, "count": len(tools)}

    async def enable_tool(self, name):
        await self.plugin.tool_mgr.enable_tool(name)
        return {"ok": True}

    async def disable_tool(self, name):
        await self.plugin.tool_mgr.disable_tool(name)
        return {"ok": True}

    # --- 记忆 API ---

    async def list_short_term_memories(self, key):
        mgr = self._get_memory_mgr()
        if not mgr:
            return {"memories": []}
        try:
            memories = await mgr.list_short_term(key)
            return {"memories": memories}
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def list_long_term_memories(self, key):
        mgr = self._get_memory_mgr()
        if not mgr:
            return {"memories": []}
        try:
            memories = await mgr.list_long_term(key)
            return {"memories": memories}
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def add_short_term_memory(self, key):
        mgr = self._get_memory_mgr()
        if not mgr:
            return error_response("记忆系统未启用", status_code=400)
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        content = data.get("content", "").strip()
        if not content:
            return error_response("内容不能为空", status_code=400)
        try:
            mid = await mgr.save_memory(key, content, "short_term", source="manual")
            return {"ok": True, "id": mid}
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def add_long_term_memory(self, key):
        mgr = self._get_memory_mgr()
        if not mgr:
            return error_response("记忆系统未启用", status_code=400)
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        content = data.get("content", "").strip()
        pinned = bool(data.get("pinned", False))
        if not content:
            return error_response("内容不能为空", status_code=400)
        try:
            mid = await mgr.save_memory(
                key,
                content,
                "long_term",
                source="manual",
                pinned=pinned,
            )
            return {"ok": True, "id": mid}
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def update_short_term_memory(self, key, id):
        mgr = self._get_memory_mgr()
        if not mgr:
            return error_response("记忆系统未启用", status_code=400)
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        content = data.get("content", "").strip()
        if not content:
            return error_response("内容不能为空", status_code=400)
        try:
            ok = await mgr.update_memory(key, id, content)
            return {"ok": ok}
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def update_long_term_memory(self, key, id):
        mgr = self._get_memory_mgr()
        if not mgr:
            return error_response("记忆系统未启用", status_code=400)
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        content = data.get("content", "").strip()
        if not content:
            return error_response("内容不能为空", status_code=400)
        # pinned: true/false 显式设置，null/缺省表示不修改
        pinned_raw = data.get("pinned")
        pinned = bool(pinned_raw) if pinned_raw is not None else None
        try:
            ok = await mgr.update_memory(
                key,
                id,
                content,
                pinned=pinned,
            )
            return {"ok": ok}
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def delete_short_term_memory(self, key, id):
        mgr = self._get_memory_mgr()
        if not mgr:
            return error_response("记忆系统未启用", status_code=400)
        try:
            ok = await mgr.delete_memory(key, id, "short_term")
            if not ok:
                return error_response("记忆不存在", status_code=404)
            return {"ok": True}
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def delete_long_term_memory(self, key, id):
        mgr = self._get_memory_mgr()
        if not mgr:
            return error_response("记忆系统未启用", status_code=400)
        try:
            ok = await mgr.delete_memory(key, id, "long_term")
            if not ok:
                return error_response("记忆不存在", status_code=404)
            return {"ok": True}
        except Exception as e:
            return error_response(str(e), status_code=500)

    # --- 主动回复 API ---

    async def list_proactive_sessions(self):
        mgr = self._get_proactive_mgr()
        if not mgr:
            return {"sessions": []}
        try:
            sessions = await mgr.list_sessions()
            return {"sessions": sessions}
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def set_proactive_timeout(self, key):
        mgr = self._get_proactive_mgr()
        if not mgr:
            return error_response("主动回复未启用", status_code=400)
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        enabled = bool(data.get("enabled", False))
        try:
            await mgr.set_timeout_enabled(key, enabled)
            return {"ok": True}
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def set_proactive_round(self, key):
        mgr = self._get_proactive_mgr()
        if not mgr:
            return error_response("主动回复未启用", status_code=400)
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        enabled = bool(data.get("enabled", False))
        try:
            await mgr.set_round_enabled(key, enabled)
            return {"ok": True}
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def set_proactive_ai_judge(self, key):
        mgr = self._get_proactive_mgr()
        if not mgr:
            return error_response("主动回复未启用", status_code=400)
        data = await request.json(default=None)
        if data is None:
            return error_response("无效的 JSON 格式", status_code=400)
        enabled = bool(data.get("enabled", False))
        try:
            await mgr.set_ai_judge_enabled(key, enabled)
            return {"ok": True}
        except Exception as e:
            return error_response(str(e), status_code=500)


def register_web_api_routes(plugin) -> None:
    """向 AstrBot Dashboard 注册全部插件 Web API 路由。

    注意:<path:key> 可跨 "/" 匹配,注册顺序即匹配优先级,
    更长的具体路由必须先于更短的通配路由注册。
    """
    ctx = plugin.context
    api = ChatWebAPI(plugin)

    routes = [
        # --- 人格 ---
        ("/api/personas", api.list_personas, ["GET"]),
        ("/api/personas", api.create_persona, ["POST"]),
        ("/api/personas/<id>/update", api.update_persona, ["POST"]),
        ("/api/personas/<id>/delete", api.delete_persona, ["POST"]),
        ("/api/personas/<id>/set_default", api.set_default_persona, ["POST"]),
        # --- 会话(长路由在前,通配在后) ---
        ("/api/sessions/<path:key>/llm-preview", api.llm_preview, ["GET"]),
        (
            "/api/sessions/<path:key>/archives/<id>/restore",
            api.restore_archive,
            ["POST"],
        ),
        ("/api/sessions/<path:key>/archives/<id>/delete", api.delete_archive, ["POST"]),
        ("/api/sessions/<path:key>/archives/<id>", api.get_archive, ["GET"]),
        ("/api/sessions/<path:key>/archives", api.list_archives, ["GET"]),
        ("/api/sessions/<path:key>/clear", api.clear_session, ["POST"]),
        ("/api/sessions/<path:key>/delete", api.delete_session, ["POST"]),
        ("/api/sessions/<path:key>", api.get_session, ["GET"]),
        ("/api/sessions", api.list_sessions, ["GET"]),
        # --- 配置 ---
        ("/api/config", api.get_config, ["GET"]),
        ("/api/config/update", api.update_config, ["POST"]),
        # --- 工具 ---
        ("/api/tools", api.list_tools, ["GET"]),
        ("/api/tools/refresh", api.refresh_tools, ["POST"]),
        ("/api/tools/<name>/enable", api.enable_tool, ["POST"]),
        ("/api/tools/<name>/disable", api.disable_tool, ["POST"]),
        # --- 记忆(带 id 的长路由在前) ---
        (
            "/api/memories/<path:key>/short/<id>/update",
            api.update_short_term_memory,
            ["POST"],
        ),
        (
            "/api/memories/<path:key>/short/<id>/delete",
            api.delete_short_term_memory,
            ["POST"],
        ),
        (
            "/api/memories/<path:key>/long/<id>/update",
            api.update_long_term_memory,
            ["POST"],
        ),
        (
            "/api/memories/<path:key>/long/<id>/delete",
            api.delete_long_term_memory,
            ["POST"],
        ),
        ("/api/memories/<path:key>/short", api.list_short_term_memories, ["GET"]),
        ("/api/memories/<path:key>/long", api.list_long_term_memories, ["GET"]),
        ("/api/memories/<path:key>/short", api.add_short_term_memory, ["POST"]),
        ("/api/memories/<path:key>/long", api.add_long_term_memory, ["POST"]),
        # --- 主动回复 ---
        ("/api/proactive/sessions", api.list_proactive_sessions, ["GET"]),
        ("/api/proactive/<path:key>/timeout", api.set_proactive_timeout, ["POST"]),
        ("/api/proactive/<path:key>/round", api.set_proactive_round, ["POST"]),
        ("/api/proactive/<path:key>/ai_judge", api.set_proactive_ai_judge, ["POST"]),
        # --- 图片配额 ---
        ("/api/image-quota", api.list_image_quota, ["GET"]),
        ("/api/image-quota/reset", api.reset_image_quota, ["POST"]),
    ]

    # 路由必须以插件名为前缀:Dashboard 按 /<plugin_name>/<route> 全路径匹配转发请求,
    # 而 Page 端 bridge endpoint 不含插件名(见官方文档 plugin-pages.md)。
    prefix = f"/{plugin.name}"
    for route, handler, methods in routes:
        ctx.register_web_api(
            f"{prefix}{route}", handler, methods, f"ChatEngine {route}"
        )
