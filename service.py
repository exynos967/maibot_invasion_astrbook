from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from typing import TYPE_CHECKING, Any

import aiohttp

from src.common.logger import get_logger

from .client import AstrBookClient, AstrBookClientConfig
from .memory import ForumMemory
from .posting_policy import PostRateLimiter

logger = get_logger("astrbook_forum_service")

if TYPE_CHECKING:
    from .proactive_post import ProactivePostResult


_service_instance: "AstrBookService | None" = None


def set_astrbook_service(service: "AstrBookService | None") -> None:
    global _service_instance
    _service_instance = service


def get_astrbook_service() -> "AstrBookService | None":
    return _service_instance


class AstrBookService:
    """Background service for AstrBook integration (WS + scheduled browse)."""

    def __init__(self, config: dict[str, Any]):
        self.config: dict[str, Any] = config or {}

        self.client = AstrBookClient(self._build_client_config())
        self.memory = ForumMemory(
            max_items=self.get_config_int("memory.max_items", default=50, min_value=1, max_value=5000),
            storage_path=self.get_config_str("memory.storage_path", default="data/astrbook/forum_memory.json"),
        )

        self.last_error: str = ""
        self.ws_connected: bool = False
        self.bot_user_id: int | None = None
        self.next_browse_time: float | None = None
        self.next_post_time: float | None = None

        self._running: bool = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_session: aiohttp.ClientSession | None = None

        self._tasks: list[asyncio.Task] = []
        self._bg_tasks: set[asyncio.Task] = set()

        self._recent_reply_ids: dict[int, float] = {}
        self._auto_reply_timestamps: deque[float] = deque(maxlen=200)

        self._post_lock = asyncio.Lock()

        self.post_rate_limiter = PostRateLimiter(
            max_posts_per_day=self.get_config_int("posting.max_posts_per_day", default=1, min_value=0, max_value=100),
            max_posts_per_hour=self.get_config_int("posting.max_posts_per_hour", default=1, min_value=0, max_value=60),
            min_interval_sec=self.get_config_int(
                "posting.min_interval_sec", default=3600, min_value=0, max_value=86400
            ),
        )
        self.recent_post_hashes: dict[str, float] = {}

    def update_config(self, config: dict[str, Any] | None) -> None:
        self.config = config or {}
        self.client.configure(self._build_client_config())
        self.memory.configure(
            max_items=self.get_config_int("memory.max_items", default=50, min_value=1, max_value=5000),
            storage_path=self.get_config_str("memory.storage_path", default="data/astrbook/forum_memory.json"),
        )
        self.post_rate_limiter.max_posts_per_day = self.get_config_int(
            "posting.max_posts_per_day", default=1, min_value=0, max_value=100
        )
        self.post_rate_limiter.max_posts_per_hour = self.get_config_int(
            "posting.max_posts_per_hour", default=1, min_value=0, max_value=60
        )
        self.post_rate_limiter.min_interval_sec = self.get_config_int(
            "posting.min_interval_sec", default=3600, min_value=0, max_value=86400
        )

    async def start(self) -> None:
        self.update_config(self.config)
        if self._running:
            return

        self._running = True
        self.last_error = ""

        realtime_enabled = self.get_config_bool("realtime.enabled", default=True)
        browse_enabled = self.get_config_bool("browse.enabled", default=True)
        posting_enabled = self.get_config_bool("posting.enabled", default=False)

        if realtime_enabled:
            self._tasks.append(self._create_task(self._ws_loop(), name="astrbook_ws_loop"))
        if browse_enabled:
            self._tasks.append(self._create_task(self._browse_loop(), name="astrbook_browse_loop"))
        if posting_enabled:
            self._tasks.append(self._create_task(self._post_loop(), name="astrbook_post_loop"))

        logger.info(
            "[AstrBook] Service started: realtime=%s browse=%s posting=%s",
            "on" if realtime_enabled else "off",
            "on" if browse_enabled else "off",
            "on" if posting_enabled else "off",
        )

    async def stop(self) -> None:
        self._running = False

        for task in list(self._bg_tasks):
            if not task.done():
                task.cancel()

        for task in self._tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)

        self._tasks.clear()
        self._bg_tasks.clear()

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        self.ws_connected = False

        if self._ws_session and not self._ws_session.closed:
            await self._ws_session.close()
        self._ws_session = None

        await self.client.close()

        logger.info("[AstrBook] Service stopped")

    async def trigger_browse_once(self) -> None:
        """Manually trigger one browse session (no scheduling)."""
        self.update_config(self.config)
        from .auto_reply import browse_once  # lazy import (avoid circular)

        await browse_once(self)

    def schedule_browse_once(self) -> None:
        """Schedule a browse session in background (for admin commands)."""
        task = self._create_task(self.trigger_browse_once(), name="astrbook_manual_browse")
        self._bg_tasks.add(task)

    async def trigger_post_once(
        self, *, force: bool = False, preferred_stream_id: str | None = None
    ) -> "ProactivePostResult":
        """Manually trigger one proactive post session (no scheduling)."""
        self.update_config(self.config)
        async with self._post_lock:
            from .proactive_post import proactive_post_once  # lazy import (avoid circular)

            result: ProactivePostResult = await proactive_post_once(
                self, force=force, preferred_stream_id=preferred_stream_id
            )

        # Always log the decision so admins can diagnose "no动静" cases.
        if result.status == "posted":
            logger.info("[AstrBook] proactive post done: %s", result.reason)
        else:
            logger.info("[AstrBook] proactive post %s: %s", result.status, result.reason)
        return result

    def schedule_post_once(
        self, *, force: bool = False, preferred_stream_id: str | None = None
    ) -> asyncio.Task:
        """Schedule a proactive post in background (for admin commands)."""
        task = self._create_task(
            self.trigger_post_once(force=force, preferred_stream_id=preferred_stream_id),
            name="astrbook_manual_post",
        )
        self._bg_tasks.add(task)
        return task

    # ==================== WebSocket ====================

    async def _ws_loop(self) -> None:
        reconnect_delay = 5
        max_delay = 60
        while self._running:
            try:
                await self._ws_connect()
                reconnect_delay = 5
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = str(e)
                logger.warning(f"[AstrBook] WS loop error: {e}")

            if not self._running:
                break
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

    async def _ws_connect(self) -> None:
        token = self.get_config_str("astrbook.token", default="").strip()
        if not token:
            self.last_error = "Token not configured, WebSocket disabled"
            logger.warning("[AstrBook] token missing, skip websocket connection")
            await asyncio.sleep(10)
            return

        ws_url = self.get_config_str("astrbook.ws_url", default="wss://book.astrbot.app/ws/bot").strip()
        if not ws_url:
            self.last_error = "ws_url not configured"
            await asyncio.sleep(10)
            return

        # token in query string (server contract)
        sep = "&" if "?" in ws_url else "?"
        url = f"{ws_url}{sep}token={token}"

        session = aiohttp.ClientSession()
        self._ws_session = session

        logger.info(f"[AstrBook] Connecting WebSocket: {ws_url}")
        async with session.ws_connect(url) as ws:
            self._ws = ws
            self.ws_connected = True
            logger.info("[AstrBook] WebSocket connected")

            heartbeat_task = self._create_task(self._heartbeat_loop(), name="astrbook_ws_heartbeat")
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = msg.json()
                        except Exception:
                            continue
                        await self._handle_ws_message(data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        self.last_error = str(ws.exception() or "ws error")
                        break
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        break
            finally:
                heartbeat_task.cancel()
                self.ws_connected = False

    async def _heartbeat_loop(self) -> None:
        while self._running and self.ws_connected and self._ws and not self._ws.closed:
            try:
                await self._ws.ping()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception:
                break

    async def _handle_ws_message(self, data: dict[str, Any]) -> None:
        msg_type = str(data.get("type", "") or "")

        if msg_type == "connected":
            user_id = data.get("user_id")
            if isinstance(user_id, int):
                self.bot_user_id = user_id
            logger.info(
                "[AstrBook] Connected as %s user_id=%s",
                str(data.get("message", "") or ""),
                str(self.bot_user_id or ""),
            )
            return
        if msg_type == "pong":
            return

        if msg_type in ("reply", "sub_reply", "mention"):
            await self._handle_notification(data)
            return
        if msg_type == "new_thread":
            self._handle_new_thread(data)

    def _handle_new_thread(self, data: dict[str, Any]) -> None:
        thread_id = data.get("thread_id")
        thread_title = str(data.get("thread_title", "") or "")
        author = str(data.get("author", "unknown") or "unknown")

        if isinstance(thread_id, int):
            self.memory.add_memory(
                "new_thread",
                f"有新帖发布：《{thread_title}》by {author}",
                metadata={"thread_id": thread_id, "thread_title": thread_title, "author": author},
            )

    async def _handle_notification(self, data: dict[str, Any]) -> None:
        now = time.time()

        msg_type = str(data.get("type", "") or "")
        thread_id = data.get("thread_id")
        reply_id = data.get("reply_id")
        thread_title = str(data.get("thread_title", "") or "")
        from_user_id = data.get("from_user_id")
        from_username = str(data.get("from_username", "unknown") or "unknown")
        content = str(data.get("content", "") or "")

        if not isinstance(thread_id, int):
            return

        if msg_type == "mention":
            self.memory.add_memory(
                "mentioned",
                f"被 @{from_username} 在《{thread_title}》中提及: {content[:50]}...",
                metadata={"thread_id": thread_id, "thread_title": thread_title, "from_user": from_username},
            )
        else:
            self.memory.add_memory(
                "replied",
                f"{from_username} 回复了你在《{thread_title}》中的发言: {content[:50]}...",
                metadata={"thread_id": thread_id, "thread_title": thread_title, "from_user": from_username},
            )

        # Auto reply decision.
        if not self.get_config_bool("realtime.auto_reply", default=True):
            return

        reply_types = self.get_config_list_str("realtime.reply_types") or ["mention", "reply", "sub_reply"]
        if msg_type not in reply_types:
            return

        # Self-avoid.
        if isinstance(from_user_id, int) and self.bot_user_id and from_user_id == self.bot_user_id:
            return

        # Dedupe (reply_id based).
        dedupe_window = self.get_config_int(
            "realtime.dedupe_window_sec", default=3600, min_value=0, max_value=86400 * 30
        )
        if isinstance(reply_id, int):
            self._cleanup_recent_reply_ids(now=now, window_sec=dedupe_window)
            if reply_id in self._recent_reply_ids and now - self._recent_reply_ids[reply_id] < dedupe_window:
                return

        # Rate limit.
        max_per_min = self.get_config_int("realtime.max_auto_replies_per_minute", default=3, min_value=0, max_value=60)
        if max_per_min <= 0:
            return
        while self._auto_reply_timestamps and now - self._auto_reply_timestamps[0] > 60:
            self._auto_reply_timestamps.popleft()
        if len(self._auto_reply_timestamps) >= max_per_min:
            return

        # Probability.
        prob = self.get_config_float("realtime.reply_probability", default=0.3, min_value=0.0, max_value=1.0)
        if random.random() > prob:
            return

        # Record.
        self._auto_reply_timestamps.append(now)
        if isinstance(reply_id, int):
            self._recent_reply_ids[reply_id] = now

        # Fire-and-forget auto reply.
        from .auto_reply import auto_reply_notification  # lazy import (avoid circular)

        task = self._create_task(
            auto_reply_notification(
                self,
                {
                    "type": msg_type,
                    "thread_id": thread_id,
                    "thread_title": thread_title,
                    "from_user_id": from_user_id,
                    "from_username": from_username,
                    "content": content,
                    "reply_id": reply_id,
                },
            ),
            name="astrbook_auto_reply",
        )
        self._bg_tasks.add(task)

    def _cleanup_recent_reply_ids(self, now: float, window_sec: int) -> None:
        if window_sec <= 0:
            self._recent_reply_ids.clear()
            return
        expired = [rid for rid, ts in self._recent_reply_ids.items() if now - ts > window_sec]
        for rid in expired:
            del self._recent_reply_ids[rid]

    # ==================== Scheduled browse ====================

    async def _browse_loop(self) -> None:
        await asyncio.sleep(60)

        while self._running:
            try:
                interval = self.get_config_int(
                    "browse.browse_interval_sec", default=3600, min_value=30, max_value=86400 * 7
                )
                self.next_browse_time = time.time() + interval

                from .auto_reply import browse_once  # lazy import (avoid circular)

                await browse_once(self)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = str(e)
                logger.warning(f"[AstrBook] browse loop error: {e}")

            interval = self.get_config_int(
                "browse.browse_interval_sec", default=3600, min_value=30, max_value=86400 * 7
            )
            self.next_browse_time = time.time() + interval
            await asyncio.sleep(interval)

    # ==================== Scheduled proactive posting ====================

    async def _post_loop(self) -> None:
        await asyncio.sleep(120)

        while self._running:
            try:
                interval = self._get_post_interval_sec()
                self.next_post_time = time.time() + interval
                await self.trigger_post_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = str(e)
                logger.warning(f"[AstrBook] post loop error: {e}")

            interval = self._get_post_interval_sec()
            self.next_post_time = time.time() + interval
            await asyncio.sleep(interval)

    # ==================== helpers ====================

    def _create_task(self, coro: Any, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro)
        try:
            task.set_name(name)
        except Exception:
            pass
        task.add_done_callback(self._task_done_callback)
        return task

    def _task_done_callback(self, task: asyncio.Task) -> None:
        if task in self._bg_tasks:
            self._bg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning(f"[AstrBook] task error: {exc}")

    def _build_client_config(self) -> AstrBookClientConfig:
        return AstrBookClientConfig(
            api_base=self.get_config_str("astrbook.api_base", default="https://book.astrbot.app"),
            token=self.get_config_str("astrbook.token", default=""),
            timeout_sec=self.get_config_int("astrbook.timeout_sec", default=10, min_value=1, max_value=120),
        )

    async def rewrite_outgoing_text(self, draft: str, *, purpose: str, title: str | None = None) -> str:
        """Rewrite outgoing forum text with MaiBot persona (best-effort).

        This is used for both tools and background tasks (auto-reply / proactive posting) so that
        all forum messages share the same MaiBot persona and writing style.
        """

        draft = str(draft or "").strip()
        if not draft:
            return draft
        if not self.client.token_configured:
            return draft
        if not self.get_config_bool("writing.enabled", default=True):
            return draft

        temperature = self.get_config_float("writing.temperature", default=0.6, min_value=0.0, max_value=2.0)
        max_tokens = self.get_config_int("writing.max_tokens", default=500, min_value=32, max_value=2048)
        max_chars = self.get_config_int("writing.max_chars", default=2000, min_value=200, max_value=20000)

        from .prompting import rewrite_forum_text  # lazy import

        try:
            return await rewrite_forum_text(
                draft=draft,
                purpose=purpose,
                title=title,
                temperature=temperature,
                max_tokens=max_tokens,
                max_chars=max_chars,
            )
        except Exception as e:
            logger.warning("[AstrBook] rewrite failed: %s", e)
            return draft

    def _get_post_interval_sec(self) -> int:
        """Return proactive posting interval in seconds.

        - New config: posting.post_interval_min (minutes)
        - Legacy config (fallback): posting.post_interval_sec (seconds)
        """

        raw_min = self._get_config_value("posting.post_interval_min", None)
        if raw_min is not None:
            interval_min = self.get_config_int("posting.post_interval_min", default=360, min_value=5, max_value=10080)
            return interval_min * 60

        return self.get_config_int("posting.post_interval_sec", default=21600, min_value=300, max_value=86400 * 7)

    def get_status_text(self) -> str:
        ws_status = "已连接" if self.ws_connected else "未连接"
        posting_cfg = "on" if self.get_config_bool("posting.enabled", default=False) else "off"
        posting_task = "running" if self._is_task_running("astrbook_post_loop") else "off"
        next_browse = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.next_browse_time))
            if self.next_browse_time
            else "N/A"
        )
        next_post = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.next_post_time)) if self.next_post_time else "N/A"
        )
        return (
            "AstrBook 论坛插件状态\n"
            "--------------------\n"
            f"- WebSocket: {ws_status}\n"
            f"- bot_user_id: {self.bot_user_id if self.bot_user_id is not None else 'N/A'}\n"
            f"- last_error: {self.last_error or 'N/A'}\n"
            f"- memory_items: {self.memory.total_items}\n"
            f"- posting.enabled(config): {posting_cfg}\n"
            f"- post_loop_task: {posting_task}\n"
            f"- next_browse_time: {next_browse}\n"
            f"- next_post_time: {next_post}\n"
        )

    def _is_task_running(self, name: str) -> bool:
        for task in self._tasks:
            try:
                if task.get_name() == name and not task.done():
                    return True
            except Exception:
                continue
        return False

    def cleanup_recent_post_hashes(self, *, now: float, window_sec: int) -> None:
        if window_sec <= 0:
            self.recent_post_hashes.clear()
            return
        expired = [h for h, ts in self.recent_post_hashes.items() if now - ts > window_sec]
        for h in expired:
            del self.recent_post_hashes[h]

    def _get_config_value(self, key: str, default: Any) -> Any:
        keys = key.split(".")
        current: Any = self.config
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default
        return current

    def get_config_str(self, key: str, default: str) -> str:
        value = self._get_config_value(key, default)
        return str(value) if value is not None else default

    def get_config_bool(self, key: str, default: bool) -> bool:
        value = self._get_config_value(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    def get_config_int(self, key: str, default: int, min_value: int, max_value: int) -> int:
        value = self._get_config_value(key, default)
        try:
            iv = int(value)
        except Exception:
            iv = int(default)
        return max(min_value, min(max_value, iv))

    def get_config_float(self, key: str, default: float, min_value: float, max_value: float) -> float:
        value = self._get_config_value(key, default)
        try:
            fv = float(value)
        except Exception:
            fv = float(default)
        return max(min_value, min(max_value, fv))

    def get_config_list_str(self, key: str) -> list[str]:
        value = self._get_config_value(key, [])
        if not value:
            return []
        if isinstance(value, list):
            items: list[str] = []
            for v in value:
                if isinstance(v, str):
                    s = v.strip()
                    if s:
                        items.append(s)
            return items
        if isinstance(value, str):
            # tolerate comma-separated input
            return [s.strip() for s in value.split(",") if s.strip()]
        return []
