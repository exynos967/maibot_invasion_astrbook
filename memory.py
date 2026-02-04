from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """A single forum memory item."""

    memory_type: str
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_type": self.memory_type,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryItem":
        ts = data.get("timestamp")
        # Historical compatibility: astrbot version stored ISO timestamps.
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts).timestamp()
            except Exception:
                ts = time.time()
        if not isinstance(ts, (int, float)):
            ts = time.time()
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        return cls(
            memory_type=str(data.get("memory_type", "")),
            content=str(data.get("content", "")),
            timestamp=float(ts),
            metadata=metadata,
        )


class ForumMemory:
    """Cross-session memory storage for AstrBook activities.

    Persisted as JSON to disk, designed to be consumed by LLM tools and humans.
    """

    def __init__(self, max_items: int = 50, storage_path: Path | str = "data/astrbook/forum_memory.json") -> None:
        self._max_items = max(1, int(max_items))
        self._storage_path = Path(storage_path)
        self._memories: list[MemoryItem] = []

        os.makedirs(self._storage_path.parent, exist_ok=True)
        self._load()

    @property
    def storage_path(self) -> Path:
        return self._storage_path

    @property
    def max_items(self) -> int:
        return self._max_items

    @property
    def total_items(self) -> int:
        return len(self._memories)

    def configure(self, max_items: int | None = None, storage_path: Path | str | None = None) -> None:
        changed_path = False
        if max_items is not None:
            self._max_items = max(1, int(max_items))
        if storage_path is not None:
            new_path = Path(storage_path)
            if new_path != self._storage_path:
                self._storage_path = new_path
                changed_path = True
            os.makedirs(self._storage_path.parent, exist_ok=True)

        if changed_path:
            self._load()
        self._trim()

    def add_memory(self, memory_type: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        item = MemoryItem(memory_type=memory_type, content=content, metadata=metadata or {})
        self._memories.append(item)
        self._trim()
        self._save()

    def add_diary(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        meta = {"is_agent_summary": True, "char_count": len(content)}
        if metadata:
            meta.update(metadata)
        self.add_memory("diary", content, meta)

    def get_memories(self, memory_type: str | None = None, limit: int | None = None) -> list[MemoryItem]:
        items = self._memories
        if memory_type:
            items = [m for m in items if m.memory_type == memory_type]
        items = list(reversed(items))  # newest first
        if limit is not None:
            items = items[: max(0, int(limit))]
        return items

    def get_summary(self, limit: int = 10) -> str:
        items = self.get_memories(limit=limit)
        if not items:
            return "最近没有论坛活动记录。"

        lines = ["我最近在 AstrBook 论坛的活动："]
        for item in items:
            time_str = datetime.fromtimestamp(item.timestamp).strftime("%m-%d %H:%M")
            lines.append(f"  {self._get_type_emoji(item.memory_type)} [{time_str}] {item.content}")
        return "\n".join(lines)

    def get_recent_thread_ids(self, window_sec: int) -> set[int]:
        """Return thread_ids appeared in memories within a time window."""
        now = time.time()
        window = max(0, int(window_sec))
        ret: set[int] = set()
        for m in reversed(self._memories):  # newest -> oldest
            if now - m.timestamp > window:
                break
            thread_id = m.metadata.get("thread_id")
            if isinstance(thread_id, int):
                ret.add(thread_id)
            elif isinstance(thread_id, str) and thread_id.isdigit():
                ret.add(int(thread_id))
        return ret

    def recall_forum_experience(self, limit: int = 5) -> str:
        limit = max(1, int(limit))
        if not self._memories:
            return "我还没有逛过论坛，没有可以回忆的经历。"

        diaries = [m for m in self._memories if m.memory_type == "diary"]
        other_memories = [m for m in self._memories if m.memory_type != "diary"]

        lines: list[str] = ["📔 我在 AstrBook 论坛的回忆：", ""]

        if diaries:
            lines.append("【我的日记】")
            for item in list(reversed(diaries))[:limit]:
                date_str = datetime.fromtimestamp(item.timestamp).strftime("%Y-%m-%d")
                lines.append(f"  📝 [{date_str}] {item.content}")
            lines.append("")

        remaining = limit - min(limit, len(diaries))
        if remaining > 0 and other_memories:
            lines.append("【最近动态】")
            emojis = {
                "browsed": "👀",
                "mentioned": "📢",
                "replied": "💬",
                "new_thread": "📝",
                "created": "✍️",
                "auto_reply": "🤖",
            }
            for item in list(reversed(other_memories))[:remaining]:
                lines.append(f"  {emojis.get(item.memory_type, '📌')} {item.content}")

        if len(lines) <= 2:
            return "我还没有逛过论坛，没有可以回忆的经历。"
        return "\n".join(lines).rstrip()

    # ==================== internal ====================

    def _trim(self) -> None:
        if len(self._memories) > self._max_items:
            self._memories = self._memories[-self._max_items :]

    def _load(self) -> None:
        self._memories = []
        if not self._storage_path.exists():
            return
        try:
            raw = json.loads(self._storage_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return
            for item in raw:
                if isinstance(item, dict):
                    self._memories.append(MemoryItem.from_dict(item))
            self._trim()
        except Exception:
            # If corrupted, rebuild an empty file to avoid cascading errors.
            self._memories = []
            self._save()

    def _save(self) -> None:
        try:
            os.makedirs(self._storage_path.parent, exist_ok=True)
            data = [m.to_dict() for m in self._memories]
            self._storage_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            # Avoid raising in memory subsystem; callers should not crash.
            return

    @staticmethod
    def _get_type_emoji(memory_type: str) -> str:
        mapping = {
            "browsed": "👀",
            "mentioned": "📢",
            "replied": "💬",
            "new_thread": "📝",
            "created": "✍️",
            "diary": "📔",
            "auto_reply": "🤖",
        }
        return mapping.get(memory_type, "📌")
