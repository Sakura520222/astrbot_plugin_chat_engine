"""Persona manager — CRUD operations and active persona management."""

from astrbot.api import logger

from ..db.models import CEPersona
from ..db.persona_repo import PersonaRepository

# 默认人格 (当数据库中没有人格时使用)
DEFAULT_PERSONA = CEPersona(
    id=0,
    name="默认助手",
    system_prompt="你是一个友善、有帮助的 AI 助手。",
    is_default=True,
)


class ChatPersonaManager:
    """人格管理器 — 管理人格的增删改查和活跃人格"""

    def __init__(self, persona_repo: PersonaRepository):
        self.repo = persona_repo

    async def get_active_persona(self) -> CEPersona:
        """获取当前活跃人格。如果没有默认人格，返回内置默认。"""
        try:
            persona = await self.repo.get_default()
            if persona:
                return persona
        except Exception as e:
            logger.error(f"[ChatEngine] 获取默认人格失败: {e}")
        return DEFAULT_PERSONA

    async def get_system_prompt(self) -> str:
        """获取当前活跃人格的 system prompt"""
        persona = await self.get_active_persona()
        return persona.system_prompt or DEFAULT_PERSONA.system_prompt

    async def create_persona(
        self, name: str, system_prompt: str, is_default: bool = False
    ) -> CEPersona:
        """创建新人格"""
        return await self.repo.create(name, system_prompt, is_default)

    async def update_persona(self, persona_id: int, **kwargs) -> CEPersona | None:
        """更新人格"""
        return await self.repo.update(persona_id, **kwargs)

    async def delete_persona(self, persona_id: int) -> bool:
        """删除人格"""
        return await self.repo.delete(persona_id)

    async def set_default(self, persona_id: int) -> bool:
        """设为默认人格"""
        return await self.repo.set_default(persona_id)

    async def list_personas(self) -> list[CEPersona]:
        """列出所有人格"""
        return await self.repo.list_all()

    async def get_persona_by_id(self, persona_id: int) -> CEPersona | None:
        """按 ID 获取人格"""
        return await self.repo.get_by_id(persona_id)
