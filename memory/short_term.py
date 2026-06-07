"""Short-term memory store — JSON file per session."""

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path

from astrbot.api import logger

from ..utils import shanghai_now_iso as _shanghai_now_iso


class ShortTermMemoryStore:
    """短期记忆存储 — 每个 session_key 一个 JSON 文件。"""

    def __init__(self, base_dir: str):
        self._base_dir = Path(base_dir) / "memory" / "short_term"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, session_key: str) -> asyncio.Lock:
        if session_key not in self._locks:
            self._locks[session_key] = asyncio.Lock()
        return self._locks[session_key]

    def _file_path(self, session_key: str) -> Path:
        h = hashlib.sha256(session_key.encode()).hexdigest()
        return self._base_dir / f"{h}.json"

    @staticmethod
    def _default_data(session_key: str) -> dict:
        return {
            "session_key": session_key,
            "memories": [],
            "turn_count": 0,
            "last_summary_turn": 0,
        }

    async def load(self, session_key: str) -> dict:
        """加载短期记忆数据，不存在时返回空默认结构。"""
        path = self._file_path(session_key)
        if not path.exists():
            return self._default_data(session_key)
        try:
            text = await asyncio.to_thread(path.read_text, "utf-8")
            return json.loads(text)
        except Exception as e:
            logger.warning(f"[Memory] 加载短期记忆失败 [{session_key}]: {e}")
            return self._default_data(session_key)

    async def save_data(self, session_key: str, data: dict) -> None:
        """保存短期记忆数据到文件。"""
        path = self._file_path(session_key)
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2)
            await asyncio.to_thread(self._write_file, path, text)
        except Exception as e:
            logger.error(f"[Memory] 保存短期记忆失败 [{session_key}]: {e}")

    @staticmethod
    def _write_file(path: Path, text: str) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, "utf-8")
        tmp.replace(path)

    async def add(self, session_key: str, content: str, source: str = "tool") -> str:
        """新增一条短期记忆，返回记忆 ID。"""
        async with self._get_lock(session_key):
            data = await self.load(session_key)
            now = _shanghai_now_iso()
            mid = uuid.uuid4().hex
            data["memories"].append(
                {
                    "id": mid,
                    "content": content,
                    "created_at": now,
                    "updated_at": now,
                    "source": source,
                }
            )
            await self.save_data(session_key, data)
            return mid

    async def update(self, session_key: str, mem_id: str, content: str) -> bool:
        """更新指定短期记忆，返回是否成功。"""
        async with self._get_lock(session_key):
            data = await self.load(session_key)
            for m in data["memories"]:
                if m["id"] == mem_id:
                    m["content"] = content
                    m["updated_at"] = _shanghai_now_iso()
                    await self.save_data(session_key, data)
                    return True
            return False

    async def delete(self, session_key: str, mem_id: str) -> bool:
        """删除指定短期记忆，返回是否成功。"""
        async with self._get_lock(session_key):
            data = await self.load(session_key)
            before = len(data["memories"])
            data["memories"] = [m for m in data["memories"] if m["id"] != mem_id]
            if len(data["memories"]) < before:
                await self.save_data(session_key, data)
                return True
            return False

    async def get_by_id(self, session_key: str, mem_id: str) -> dict | None:
        """按 ID 查找短期记忆。"""
        data = await self.load(session_key)
        for m in data["memories"]:
            if m["id"] == mem_id:
                return m
        return None

    async def increment_turn(self, session_key: str) -> int:
        """轮数 +1，返回更新后的轮数。"""
        async with self._get_lock(session_key):
            data = await self.load(session_key)
            data["turn_count"] = data.get("turn_count", 0) + 1
            await self.save_data(session_key, data)
            return data["turn_count"]

    async def set_last_summary_turn(self, session_key: str, turn: int) -> None:
        """设置上次自动总结时的轮数。"""
        async with self._get_lock(session_key):
            data = await self.load(session_key)
            data["last_summary_turn"] = turn
            await self.save_data(session_key, data)

    async def replace_memories(
        self,
        session_key: str,
        keeps: list[str],
        deletes: list[str],
        adds: list[dict],
        updates: dict[str, str],
    ) -> None:
        """根据总结结果批量更新短期记忆。"""
        async with self._get_lock(session_key):
            data = await self.load(session_key)
            now = _shanghai_now_iso()
            delete_set = set(deletes)
            new_memories = []
            for m in data["memories"]:
                if m["id"] in delete_set:
                    continue
                if m["id"] in updates:
                    m["content"] = updates[m["id"]]
                    m["updated_at"] = now
                new_memories.append(m)
            for content in adds:
                new_memories.append(
                    {
                        "id": uuid.uuid4().hex,
                        "content": content,
                        "created_at": now,
                        "updated_at": now,
                        "source": "auto",
                    }
                )
            data["memories"] = new_memories
            await self.save_data(session_key, data)

    async def delete_session(self, session_key: str) -> None:
        """删除整个 session 的短期记忆文件。"""
        path = self._file_path(session_key)
        if path.exists():
            try:
                await asyncio.to_thread(os.remove, path)
            except Exception as e:
                logger.warning(f"[Memory] 删除短期记忆文件失败: {e}")
        self._locks.pop(session_key, None)

    async def list_memories(self, session_key: str) -> list[dict]:
        """返回所有短期记忆列表。"""
        data = await self.load(session_key)
        return data.get("memories", [])
