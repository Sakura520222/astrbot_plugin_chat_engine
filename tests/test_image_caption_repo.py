"""ImageCaptionRepository 单元测试 — 文件 SQLite,不依赖 AstrBot 框架。

覆盖:
- find: 未命中返回 None
- save + find: 写入后命中
- save 幂等: 重复写同一 key 不抛错
- 隔离性: provider_id / prompt_hash / sha256 变化互不影响
- 并发: 同 key 并发 save 只落一条
"""

import asyncio
import tempfile
import uuid
from pathlib import Path

import pytest_asyncio
from astrbot_plugin_chat_engine.db.image_caption_repo import ImageCaptionRepository
from astrbot_plugin_chat_engine.db.models import CEImageCaption, chat_engine_metadata
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def repo():
    """文件 SQLite (WAL) + 建表,返回 ImageCaptionRepository 实例。

    用文件库而非 :memory:,使每个 session 获得独立连接,从而能可靠测试
    并发场景(:memory: + StaticPool 会让多 session 共享连接,事务互相干扰)。
    WAL 模式 + timeout 让并发写入序列化而非报锁错误。
    """
    db_path = Path(tempfile.gettempdir()) / f"test_ce_{uuid.uuid4().hex}.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(chat_engine_metadata.create_all)
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    yield ImageCaptionRepository(factory)
    await engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        try:
            db_path.with_name(db_path.name + suffix).unlink()
        except OSError:
            pass


# --- find ---


async def test_find_returns_none_on_miss(repo):
    assert await repo.find("a" * 64, "prov1", "p" * 16) is None


# --- save + find ---


async def test_save_then_find_roundtrip(repo):
    await repo.save("a" * 64, "prov1", "p" * 16, "一张猫的图片")
    assert await repo.find("a" * 64, "prov1", "p" * 16) == "一张猫的图片"


# --- save 幂等 ---


async def test_save_is_idempotent_on_duplicate_key(repo):
    await repo.save("a" * 64, "prov1", "p" * 16, "第一次")
    # 同 key 重复写入不抛错,且内容保持首次值
    await repo.save("a" * 64, "prov1", "p" * 16, "第二次")
    assert await repo.find("a" * 64, "prov1", "p" * 16) == "第一次"


# --- 隔离性 ---


async def test_isolated_by_provider_id(repo):
    await repo.save("a" * 64, "prov1", "p" * 16, "prov1 的转述")
    assert await repo.find("a" * 64, "prov2", "p" * 16) is None


async def test_isolated_by_prompt_hash(repo):
    await repo.save("a" * 64, "prov1", "p1" + "p" * 14, "旧提示词的转述")
    assert await repo.find("a" * 64, "prov1", "p2" + "p" * 14) is None


async def test_isolated_by_sha256(repo):
    await repo.save("a" * 64, "prov1", "p" * 16, "图 A 的转述")
    assert await repo.find("b" * 64, "prov1", "p" * 16) is None


# --- 并发 ---


async def test_concurrent_save_same_key_leaves_single_row(repo):
    """并发写入同 key:唯一约束兜底,最终只落一条且 find 可命中。"""
    await asyncio.gather(
        *[repo.save("c" * 64, "prov1", "p" * 16, f"转述 {i}") for i in range(5)]
    )
    async with repo._factory() as session:
        count = await session.scalar(select(func.count()).select_from(CEImageCaption))
    assert count == 1
    assert await repo.find("c" * 64, "prov1", "p" * 16) is not None
