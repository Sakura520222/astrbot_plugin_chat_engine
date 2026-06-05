"""Memory tool implementations — called from @llm_tool methods in main.py."""


async def save_memory_tool(
    memory_mgr,
    session_key: str,
    content: str,
    mem_type: str,
    source: str = "tool",
    pinned: bool = False,
) -> str:
    """保存一条记忆。"""
    if mem_type not in ("short_term", "long_term"):
        return f"Invalid type '{mem_type}'. Must be 'short_term' or 'long_term'."

    # 截断过长内容
    max_chars = memory_mgr._cfg_int("short_term_max_chars", 200)
    if len(content) > max_chars:
        content = content[:max_chars]

    if mem_type == "short_term":
        # 检查上限
        max_count = memory_mgr._cfg_int("short_term_max_count", 30)
        current = await memory_mgr.short_term.list_memories(session_key)
        if len(current) >= max_count:
            return f"Short-term memory is full ({max_count} items). Wait for auto-summary to clean up."

        mid = await memory_mgr.short_term.add(session_key, content, source=source)
        return f"Saved to short-term memory (id: {mid})"

    else:  # long_term
        if not memory_mgr.long_term.available:
            return (
                "Long-term memory is not available (no EmbeddingProvider configured)."
            )

        # 检查上限
        max_count = memory_mgr._cfg_int("long_term_max_count", 200)
        current = await memory_mgr.long_term.list_all(session_key)
        if len(current) >= max_count:
            return f"Long-term memory is full ({max_count} items)."

        mid = await memory_mgr.long_term.save(
            session_key,
            content,
            source=source,
            pinned=pinned,
        )
        if mid:
            status = "pinned" if pinned else "active"
            return f"Saved to long-term memory (id: {mid}, {status})"
        return "Failed to save to long-term memory."


async def search_memory_tool(
    memory_mgr, session_key: str, query: str, top_k: int = 5
) -> str:
    """搜索长期记忆。"""
    if not memory_mgr.long_term.available:
        return "Long-term memory is not available (no EmbeddingProvider configured)."

    # 限制 top_k 范围
    top_k = max(1, min(top_k, 20))

    results = await memory_mgr.long_term.search(
        session_key,
        query=query,
        top_k=top_k,
        fetch_k=memory_mgr._cfg_int("long_term_fetch_k", 20),
        enable_rerank=memory_mgr._cfg_bool("long_term_enable_rerank", True),
        similarity_threshold=memory_mgr._cfg_float(
            "long_term_similarity_threshold", 0.3
        ),
    )

    if not results:
        return "No relevant memories found."

    lines = []
    for r in results:
        lines.append(f"[id:{r['id']}] (similarity: {r['similarity']}) {r['content']}")
    return "\n".join(lines)


async def update_memory_tool(
    memory_mgr, session_key: str, mem_id: str, content: str
) -> str:
    """更新一条记忆（自动查找短期/长期）。"""
    # 截断过长内容
    max_chars = memory_mgr._cfg_int("short_term_max_chars", 200)
    if len(content) > max_chars:
        content = content[:max_chars]

    # 先查短期
    ok = await memory_mgr.short_term.update(session_key, mem_id, content)
    if ok:
        return "Memory updated (short-term)."

    # 再查长期
    if memory_mgr.long_term.available:
        ok = await memory_mgr.long_term.update(session_key, mem_id, content)
        if ok:
            return "Memory updated (long-term)."

    return "Memory not found."


async def delete_memory_tool(
    memory_mgr, session_key: str, mem_id: str, mem_type: str
) -> str:
    """删除一条记忆。"""
    if mem_type == "short_term":
        ok = await memory_mgr.short_term.delete(session_key, mem_id)
        if ok:
            return "Memory deleted (short-term)."
        return "Memory not found in short-term."

    elif mem_type == "long_term":
        if not memory_mgr.long_term.available:
            return "Long-term memory is not available."
        ok = await memory_mgr.long_term.delete(session_key, mem_id)
        if ok:
            return "Memory deleted (long-term)."
        return "Memory not found in long-term."

    else:
        return f"Invalid type '{mem_type}'. Must be 'short_term' or 'long_term'."
