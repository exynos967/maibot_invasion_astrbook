from __future__ import annotations

import re
from collections import deque


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"\bhttps?://\S+\b", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"\b\d{6,}\b")
_MENTION_RE = re.compile(r"@[^\s]+")


def sanitize_forum_text(text: str, *, allow_urls: bool, allow_mentions: bool) -> str:
    """Best-effort sanitizer to avoid leaking sensitive info to a public forum.

    Note: This is a risk-control layer, not a security boundary.
    """

    out = str(text or "")

    out = _EMAIL_RE.sub("<EMAIL>", out)
    out = _LONG_DIGITS_RE.sub("<ID>", out)

    if not allow_urls:
        out = _URL_RE.sub("<URL>", out)
    if not allow_mentions:
        out = _MENTION_RE.sub("<MENTION>", out)

    # Normalize whitespace a bit.
    out = re.sub(r"[ \t]+", " ", out).strip()
    return out


class PostRateLimiter:
    """In-memory rolling-window rate limiter for proactive forum posting."""

    def __init__(
        self,
        *,
        max_posts_per_day: int,
        max_posts_per_hour: int,
        min_interval_sec: int,
        day_window_sec: int = 86400,
        hour_window_sec: int = 3600,
    ) -> None:
        self.max_posts_per_day = int(max_posts_per_day)
        self.max_posts_per_hour = int(max_posts_per_hour)
        self.min_interval_sec = int(min_interval_sec)
        self.day_window_sec = int(day_window_sec)
        self.hour_window_sec = int(hour_window_sec)

        self._timestamps: deque[float] = deque()
        self._last_post_ts: float | None = None

    def _prune(self, *, now: float) -> None:
        threshold = now - max(0, self.day_window_sec)
        while self._timestamps and self._timestamps[0] < threshold:
            self._timestamps.popleft()

    def _count_since(self, *, now: float, window_sec: int) -> int:
        if window_sec <= 0:
            return len(self._timestamps)
        start = now - window_sec
        return sum(1 for ts in self._timestamps if ts >= start)

    def allow(self, *, now: float) -> bool:
        self._prune(now=now)

        if self.min_interval_sec > 0 and self._last_post_ts is not None:
            if now - self._last_post_ts < self.min_interval_sec:
                return False

        if self.max_posts_per_day > 0 and len(self._timestamps) >= self.max_posts_per_day:
            return False

        if (
            self.max_posts_per_hour > 0
            and self._count_since(now=now, window_sec=self.hour_window_sec) >= self.max_posts_per_hour
        ):
            return False

        return True

    def record(self, *, now: float) -> None:
        self._prune(now=now)
        self._timestamps.append(now)
        self._last_post_ts = now
