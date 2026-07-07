"""ImageQuotaRepository 单元测试 — 内存 SQLite,不依赖 AstrBot 框架。

覆盖:
- get_used: 新 key 返回 0
- incr_used: 首次返回 1、递增、并发原子性
- decr_used: 递减且不低于 0
- 日期/ key 隔离性
- list_today: 按日期过滤
- reset: 清除记录
"""

import asyncio
import tempfile
import uuid
from pathlib import Path

import pytest_asyncio
from astrbot_plugin_chat_engine.db.image_quota_repo import ImageQuotaRepository
from astrbot_plugin_chat_engine.db.models import chat_engine_metadata
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@pytest_asyncio.fixture
async def repo():
    """文件 SQLite (WAL) + 建表,返回 ImageQuotaRepository 实例。

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
    yield ImageQuotaRepository(factory)
    await engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        try:
            db_path.with_name(db_path.name + suffix).unlink()
        except OSError:
            pass


# --- get_used ---


async def test_get_used_returns_zero_for_new_key(repo):
    assert await repo.get_used("user:123", "2026-07-07") == 0


# --- incr_used ---


async def test_incr_used_returns_one_on_first_call(repo):
    count = await repo.incr_used("user:123", "2026-07-07")
    assert count == 1
    assert await repo.get_used("user:123", "2026-07-07") == 1


async def test_incr_used_increments_existing(repo):
    await repo.incr_used("user:123", "2026-07-07")
    count = await repo.incr_used("user:123", "2026-07-07")
    assert count == 2
    assert await repo.get_used("user:123", "2026-07-07") == 2


async def test_incr_used_concurrent_safe(repo):
    """并发 5 次 incr,最终计数应为 5(无丢失更新)。"""
    await asyncio.gather(
        *[repo.incr_used("user:123", "2026-07-07") for _ in range(5)]
    )
    assert await repo.get_used("user:123", "2026-07-07") == 5


# --- decr_used ---


async def test_decr_used_decrements(repo):
    await repo.incr_used("user:123", "2026-07-07")
    await repo.incr_used("user:123", "2026-07-07")
    await repo.decr_used("user:123", "2026-07-07")
    assert await repo.get_used("user:123", "2026-07-07") == 1


async def test_decr_used_not_below_zero(repo):
    """无记录或已为 0 时 decr 不应为负。"""
    await repo.decr_used("user:123", "2026-07-07")  # 无记录
    assert await repo.get_used("user:123", "2026-07-07") == 0
    await repo.incr_used("user:123", "2026-07-07")
    await repo.decr_used("user:123", "2026-07-07")
    await repo.decr_used("user:123", "2026-07-07")  # 已为 0
    assert await repo.get_used("user:123", "2026-07-07") == 0


# --- 隔离性 ---


async def test_different_dates_independent(repo):
    await repo.incr_used("user:123", "2026-07-07")
    await repo.incr_used("user:123", "2026-07-07")
    assert await repo.get_used("user:123", "2026-07-08") == 0


async def test_different_keys_independent(repo):
    await repo.incr_used("user:123", "2026-07-07")
    await repo.incr_used("user:456", "2026-07-07")
    assert await repo.get_used("user:123", "2026-07-07") == 1
    assert await repo.get_used("user:456", "2026-07-07") == 1


# --- list_today ---


async def test_list_today_filters_by_date(repo):
    await repo.incr_used("user:123", "2026-07-07")
    await repo.incr_used("user:456", "2026-07-07")
    await repo.incr_used("user:789", "2026-07-08")  # 不同日期,不应出现
    records = await repo.list_today("2026-07-07")
    keys = {r.quota_key for r in records}
    assert keys == {"user:123", "user:456"}


# --- reset ---


async def test_reset_clears_record(repo):
    await repo.incr_used("user:123", "2026-07-07")
    await repo.incr_used("user:123", "2026-07-07")
    ok = await repo.reset("user:123", "2026-07-07")
    assert ok is True
    assert await repo.get_used("user:123", "2026-07-07") == 0


async def test_reset_returns_false_for_nonexistent(repo):
    ok = await repo.reset("user:999", "2026-07-07")
    assert ok is False
