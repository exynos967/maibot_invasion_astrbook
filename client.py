from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass(frozen=True, slots=True)
class AstrBookClientConfig:
    api_base: str
    token: str
    timeout_sec: int = 40


class AstrBookClient:
    """AstrBook HTTP API client."""

    def __init__(self, config: AstrBookClientConfig):
        self._api_base = (config.api_base or "").rstrip("/")
        self._token = config.token or ""
        self._timeout_sec = int(config.timeout_sec or 40)
        self._session: aiohttp.ClientSession | None = None

    def configure(self, config: AstrBookClientConfig) -> None:
        self._api_base = (config.api_base or "").rstrip("/")
        self._token = config.token or ""
        self._timeout_sec = int(config.timeout_sec or 40)

    @property
    def api_base(self) -> str:
        return self._api_base

    @property
    def token_configured(self) -> bool:
        return bool(self._token.strip())

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate",
        }

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        timeout = aiohttp.ClientTimeout(total=self._timeout_sec)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _make_request(
        self, method: str, endpoint: str, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._token.strip():
            return {"error": "Token not configured. Please set 'astrbook.token' in plugin config."}
        if not self._api_base:
            return {"error": "api_base not configured. Please set 'astrbook.api_base' in plugin config."}

        url = f"{self._api_base}{endpoint}"
        session = await self._get_session()

        try:
            if method == "GET":
                async with session.get(url, headers=self._get_headers(), params=params) as resp:
                    return await self._parse_response(resp)
            if method == "POST":
                async with session.post(url, headers=self._get_headers(), json=data) as resp:
                    return await self._parse_response(resp)
            if method == "DELETE":
                async with session.delete(url, headers=self._get_headers()) as resp:
                    return await self._parse_response(resp)
            return {"error": f"Unsupported method: {method}"}
        except asyncio.TimeoutError:
            return {"error": "Request timeout"}
        except aiohttp.ClientConnectorError:
            return {"error": f"Cannot connect to server: {self._api_base}"}
        except aiohttp.ClientError as e:
            return {"error": f"Request error: {str(e)}"}
        except Exception as e:
            return {"error": f"Request error: {str(e)}"}

    async def _parse_response(self, resp: aiohttp.ClientResponse) -> dict[str, Any]:
        if resp.status == 200:
            content_type = resp.headers.get("content-type", "")
            if "text/plain" in content_type:
                return {"text": await resp.text()}
            try:
                return await resp.json()
            except Exception:
                return {"text": await resp.text()}

        if resp.status == 401:
            return {"error": "Token invalid or expired"}
        if resp.status == 404:
            return {"error": "Resource not found"}
        text = await resp.text()
        return {"error": f"Request failed: {resp.status} - {text[:200] if text else 'No response'}"}

    # ==================== endpoints ====================

    async def browse_threads(self, page: int = 1, page_size: int = 10, category: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "page_size": min(int(page_size), 50), "format": "text"}
        if category:
            params["category"] = category
        return await self._make_request("GET", "/api/threads", params=params)

    async def list_threads(self, page: int = 1, page_size: int = 10, category: str | None = None) -> Any:
        """List threads in JSON format (server default).

        This is useful for programmatic operations (e.g. resolving the latest thread_id),
        while `browse_threads` is mainly for human-readable text output.
        """

        params: dict[str, Any] = {"page": page, "page_size": min(int(page_size), 50)}
        if category:
            params["category"] = category
        return await self._make_request("GET", "/api/threads", params=params)

    async def search_threads(self, keyword: str, page: int = 1, category: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"q": keyword, "page": page, "page_size": 10}
        if category:
            params["category"] = category
        return await self._make_request("GET", "/api/threads/search", params=params)

    async def read_thread(self, thread_id: int, page: int = 1) -> dict[str, Any]:
        return await self._make_request(
            "GET",
            f"/api/threads/{thread_id}",
            params={"page": page, "page_size": 20, "format": "text"},
        )

    async def create_thread(self, title: str, content: str, category: str = "chat") -> dict[str, Any]:
        return await self._make_request(
            "POST",
            "/api/threads",
            data={"title": title, "content": content, "category": category},
        )

    async def reply_thread(self, thread_id: int, content: str) -> dict[str, Any]:
        return await self._make_request("POST", f"/api/threads/{thread_id}/replies", data={"content": content})

    async def reply_floor(self, reply_id: int, content: str) -> dict[str, Any]:
        return await self._make_request("POST", f"/api/replies/{reply_id}/sub_replies", data={"content": content})

    async def get_sub_replies(self, reply_id: int, page: int = 1) -> dict[str, Any]:
        return await self._make_request(
            "GET",
            f"/api/replies/{reply_id}/sub_replies",
            params={"page": page, "page_size": 20, "format": "text"},
        )

    async def check_notifications(self) -> dict[str, Any]:
        return await self._make_request("GET", "/api/notifications/unread-count")

    async def get_notifications(self, unread_only: bool = True) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": 10}
        if unread_only:
            params["is_read"] = "false"
        return await self._make_request("GET", "/api/notifications", params=params)

    async def mark_notifications_read(self) -> dict[str, Any]:
        return await self._make_request("POST", "/api/notifications/read-all", data={})

    async def delete_thread(self, thread_id: int) -> dict[str, Any]:
        return await self._make_request("DELETE", f"/api/threads/{thread_id}")

    async def delete_reply(self, reply_id: int) -> dict[str, Any]:
        return await self._make_request("DELETE", f"/api/replies/{reply_id}")
