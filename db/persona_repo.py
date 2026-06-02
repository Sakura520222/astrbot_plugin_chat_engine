"""CEPersona CRUD operations"""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import CEPersona


class PersonaRepository:
    """人格仓库 — 管理人格的增删改查"""

    def __init__(self, session_factory: async_sessionmaker):
        self._factory = session_factory

    async def get_default(self) -> CEPersona | None:
        """获取当前默认人格"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEPersona).where(CEPersona.is_default == True)  # noqa: E712
            )
            return result.scalar_one_or_none()

    async def get_by_id(self, persona_id: int) -> CEPersona | None:
        """按 ID 获取人格"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEPersona).where(CEPersona.id == persona_id)
            )
            return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> CEPersona | None:
        """按名称获取人格"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEPersona).where(CEPersona.name == name)
            )
            return result.scalar_one_or_none()

    async def list_all(self) -> list[CEPersona]:
        """列出所有人格"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEPersona).order_by(CEPersona.is_default.desc(), CEPersona.id)
            )
            return list(result.scalars().all())

    async def create(
        self, name: str, system_prompt: str, is_default: bool = False
    ) -> CEPersona:
        """创建新人格。如果 is_default，先清除其他默认。"""
        async with self._factory() as session:
            if is_default:
                await session.execute(update(CEPersona).values(is_default=False))

            persona = CEPersona(
                name=name,
                system_prompt=system_prompt,
                is_default=is_default,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            session.add(persona)
            await session.commit()
            await session.refresh(persona)
            return persona

    async def update(self, persona_id: int, **kwargs) -> CEPersona | None:
        """更新人格字段。支持 name, system_prompt, is_default。"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEPersona).where(CEPersona.id == persona_id)
            )
            persona = result.scalar_one_or_none()
            if persona is None:
                return None

            if kwargs.get("is_default"):
                await session.execute(update(CEPersona).values(is_default=False))

            for key, value in kwargs.items():
                if hasattr(persona, key) and key not in ("id", "created_at"):
                    setattr(persona, key, value)

            persona.updated_at = datetime.utcnow()
            await session.commit()
            await session.refresh(persona)
            return persona

    async def delete(self, persona_id: int) -> bool:
        """删除指定人格"""
        async with self._factory() as session:
            result = await session.execute(
                select(CEPersona).where(CEPersona.id == persona_id)
            )
            persona = result.scalar_one_or_none()
            if persona is None:
                return False

            await session.delete(persona)
            await session.commit()
            return True

    async def set_default(self, persona_id: int) -> bool:
        """将指定人格设为默认"""
        async with self._factory() as session:
            # 清除所有默认
            await session.execute(update(CEPersona).values(is_default=False))
            # 设置指定人格为默认
            result = await session.execute(
                select(CEPersona).where(CEPersona.id == persona_id)
            )
            persona = result.scalar_one_or_none()
            if persona is None:
                return False

            persona.is_default = True
            persona.updated_at = datetime.utcnow()
            await session.commit()
            return True
