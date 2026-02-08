from __future__ import annotations

import asyncio
import json
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
    """Background service for AstrBook integration (SSE + scheduled browse)."""

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
        self._sse_connect_attempts: int = 0
        self._sse_connect_successes: int = 0
        self._sse_reconnect_count: int = 0
        self._sse_last_disconnect_reason: str = ""
        self._sse_last_disconnect_ts: float | None = None
        self._sse_last_event_type: str = ""
        self._sse_last_event_ts: float | None = None

        self._running: bool = False
        self._sse_session: aiohttp.ClientSession | None = None

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

        self._profile_cache: dict[str, Any] | None = None
        self._profile_cache_ts: float = 0.0

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
            self._tasks.append(self._create_task(self._sse_loop(), name="astrbook_sse_loop"))
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

        self.ws_connected = False

        if self._sse_session and not self._sse_session.closed:
            await self._sse_session.close()
        self._sse_session = None

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


    async def get_profile_snapshot(
        self,
        *,
        force_refresh: bool = False,
        ttl_sec: int = 300,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Fetch AstrBook profile with lightweight cache for prompt context."""

        ttl = max(30, min(3600, int(ttl_sec)))
        now = time.time()

        if (
            not force_refresh
            and self._profile_cache is not None
            and now - self._profile_cache_ts <= ttl
        ):
            return self._profile_cache, None

        result = await self.client.get_my_profile()
        if isinstance(result, dict) and "error" not in result:
            self._profile_cache = result
            self._profile_cache_ts = now
            return result, None

        err_text = "profile api failed"
        if isinstance(result, dict):
            err_text = str(result.get("error") or err_text)

        if self._profile_cache is not None:
            return self._profile_cache, err_text
        return None, err_text

    async def get_profile_context_block(self, *, ttl_sec: int = 300) -> str:
        """Build concise profile context for prompts (best-effort, never raises)."""

        profile, err = await self.get_profile_snapshot(ttl_sec=ttl_sec)

        try:
            from .prompting import build_forum_profile_block

            return build_forum_profile_block(profile, stale_hint=err)
        except Exception:
            return ""

    # ==================== SSE ====================

    async def _sse_loop(self) -> None:
        reconnect_delay = 5
        max_delay = 60
        while self._running:
            self._sse_connect_attempts += 1
            try:
                await self._sse_connect()
                reconnect_delay = 5
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = str(e)
                logger.warning(f"[AstrBook] SSE loop error: {e}")

            if not self._running:
                break

            self._sse_reconnect_count += 1
            logger.info(
                "[AstrBook] SSE reconnect in %ss (attempt=%s, reason=%s)",
                reconnect_delay,
                self._sse_connect_attempts + 1,
                self._sse_last_disconnect_reason or "unknown",
            )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

    def _build_sse_url(self) -> str:
        api_base = self.get_config_str("astrbook.api_base", default="https://book.astrbot.app").strip().rstrip("/")
        if not api_base:
            return ""
        return f"{api_base}/sse/bot"

    def _record_sse_disconnect(self, reason: str) -> None:
        self._sse_last_disconnect_reason = (reason or "unknown").strip() or "unknown"
        self._sse_last_disconnect_ts = time.time()

    async def _sse_connect(self) -> None:
        token = self.get_config_str("astrbook.token", default="").strip()
        if not token:
            self.last_error = "Token not configured, realtime disabled"
            self._record_sse_disconnect("token_missing")
            logger.warning("[AstrBook] token missing, skip realtime connection")
            await asyncio.sleep(10)
            return

        sse_url = self._build_sse_url()
        if not sse_url:
            self.last_error = "api_base not configured"
            self._record_sse_disconnect("api_base_missing")
            await asyncio.sleep(10)
            return

        session = aiohttp.ClientSession()
        self._sse_session = session
        disconnect_reason = "stream_closed"

        logger.info("[AstrBook] Connecting SSE: %s", sse_url)
        try:
            async with session.get(
                sse_url,
                params={"token": token},
                headers={"Accept": "text/event-stream"},
                timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
            ) as response:
                if response.status == 401:
                    self.last_error = "SSE authentication failed"
                    disconnect_reason = "auth_failed"
                    logger.error("[AstrBook] SSE authentication failed: invalid or expired token")
                    return
                if response.status != 200:
                    self.last_error = f"SSE connection failed: {response.status}"
                    disconnect_reason = f"http_{response.status}"
                    logger.warning("[AstrBook] SSE connection failed with status %s", response.status)
                    return

                self.ws_connected = True
                self.last_error = ""
                self._sse_connect_successes += 1
                logger.info("[AstrBook] SSE connected (success_count=%s)", self._sse_connect_successes)

                buffer = ""
                async for chunk in response.content:
                    if not chunk:
                        continue
                    buffer += chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        await self._parse_sse_block(block)

                if buffer.strip():
                    await self._parse_sse_block(buffer)
        except asyncio.CancelledError:
            disconnect_reason = "cancelled"
            raise
        except Exception as e:
            disconnect_reason = f"error_{type(e).__name__}"
            raise
        finally:
            if not self._running and disconnect_reason == "stream_closed":
                disconnect_reason = "service_stopped"

            self.ws_connected = False
            self._record_sse_disconnect(disconnect_reason)
            if self._sse_session is session:
                self._sse_session = None
            if not session.closed:
                await session.close()

    async def _parse_sse_block(self, block: str) -> None:
        event_type = ""
        data_lines: list[str] = []

        for raw_line in block.split("\n"):
            line = raw_line.strip("\r")
            if not line:
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_type = line.partition(":")[2].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line.partition(":")[2].lstrip())

        if not data_lines:
            return

        payload_text = "\n".join(data_lines).strip()
        if not payload_text:
            return

        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            logger.debug(
                "[AstrBook] ignore non-json sse payload event=%s data=%s",
                event_type or "message",
                payload_text[:120],
            )
            return

        if not isinstance(payload, dict):
            logger.debug("[AstrBook] ignore non-dict sse payload type=%s", type(payload).__name__)
            return

        self._sse_last_event_type = event_type or str(payload.get("type", "") or "message")
        self._sse_last_event_ts = time.time()

        await self._handle_realtime_message(payload)

    async def _handle_realtime_message(self, data: dict[str, Any]) -> None:
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
            timeout_sec=self.get_config_int("astrbook.timeout_sec", default=40, min_value=1, max_value=120),
        )

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

    def _format_time_or_na(self, ts: float | None) -> str:
        if ts is None:
            return "N/A"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def get_status_text(self) -> str:
        realtime_status = "已连接" if self.ws_connected else "未连接"
        posting_cfg = "on" if self.get_config_bool("posting.enabled", default=False) else "off"
        posting_task = "running" if self._is_task_running("astrbook_post_loop") else "off"
        next_browse = self._format_time_or_na(self.next_browse_time)
        next_post = self._format_time_or_na(self.next_post_time)
        last_sse_event = self._sse_last_event_type or "N/A"
        last_sse_event_time = self._format_time_or_na(self._sse_last_event_ts)
        last_sse_disconnect_reason = self._sse_last_disconnect_reason or "N/A"
        last_sse_disconnect_time = self._format_time_or_na(self._sse_last_disconnect_ts)

        return (
            "AstrBook 论坛插件状态\n"
            "--------------------\n"
            f"- SSE: {realtime_status}\n"
            f"- bot_user_id: {self.bot_user_id if self.bot_user_id is not None else 'N/A'}\n"
            f"- last_error: {self.last_error or 'N/A'}\n"
            f"- memory_items: {self.memory.total_items}\n"
            f"- posting.enabled(config): {posting_cfg}\n"
            f"- post_loop_task: {posting_task}\n"
            f"- next_browse_time: {next_browse}\n"
            f"- next_post_time: {next_post}\n"
            f"- sse_connect_attempts: {self._sse_connect_attempts}\n"
            f"- sse_connect_successes: {self._sse_connect_successes}\n"
            f"- sse_reconnects: {self._sse_reconnect_count}\n"
            f"- sse_last_event: {last_sse_event}\n"
            f"- sse_last_event_time: {last_sse_event_time}\n"
            f"- sse_last_disconnect_reason: {last_sse_disconnect_reason}\n"
            f"- sse_last_disconnect_time: {last_sse_disconnect_time}\n"
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
