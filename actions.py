from __future__ import annotations

import json
import time
import re
from typing import Any, Tuple

from json_repair import repair_json

from src.common.logger import get_logger
from src.plugin_system import ActionActivationType, BaseAction

from .client import AstrBookClient
from .memory import ForumMemory
from .service import AstrBookService, get_astrbook_service
from .tools import VALID_CATEGORIES

logger = get_logger("astrbook_forum_actions")


_RECENT_LIKE_ACTIONS: dict[tuple[str, str, int], float] = {}
_LIKE_ACTION_DEDUPE_WINDOW_SEC = 8.0


def _cleanup_recent_like_actions(now: float) -> None:
    expired = [
        key
        for key, ts in _RECENT_LIKE_ACTIONS.items()
        if now - ts > _LIKE_ACTION_DEDUPE_WINDOW_SEC
    ]
    for key in expired:
        del _RECENT_LIKE_ACTIONS[key]


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit():
            return int(s)
    return None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _extract_first_int(text: str) -> int | None:
    """Extract a standalone integer from user text.

    This is used for parsing thread_id/reply_id from natural language. It should be conservative
    and avoid extracting digits embedded in URLs or alphanumeric IDs like "BV13xxxx".
    """

    text = str(text or "").strip()
    if not text:
        return None

    # Prefer explicit param patterns first.
    for pat in (
        r"(?:thread_id|reply_id)\s*[:=：]\s*(\d+)",
        r"(?:threadid|replyid)\s*[:=：]\s*(\d+)",
    ):
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass

    # Strip URLs to avoid accidental matches in links.
    text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)

    # Match numbers that are not adjacent to ASCII alphanumerics (avoid BV13xxxx, abc123def, etc).
    m = re.search(r"(?<![A-Za-z0-9])(\d+)(?![A-Za-z0-9])", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None



def _extract_target_id_from_text(text: str, *, target_type: str | None = None) -> int | None:
    """Extract target id for like/reply operations with lightweight intent constraints.

    Unlike `_extract_first_int`, this parser only accepts ids near semantic markers
    (e.g. 帖子/reply/thread_id/target_id) to avoid picking unrelated numbers such as QQ IDs.
    """

    text = str(text or "").strip()
    if not text:
        return None

    target = str(target_type or "").strip().lower()

    thread_patterns = (
        r"(?:thread_id|threadid)\s*[:=：]\s*(\d+)",
        r"(?:帖子|贴子|主题)\s*(?:id|ID)?\s*[:=：]?\s*(\d+)",
        r"(\d+)\s*号?\s*(?:帖子|贴子|主题)",
    )
    reply_patterns = (
        r"(?:reply_id|replyid)\s*[:=：]\s*(\d+)",
        r"(?:回复|楼层|楼中楼)\s*(?:id|ID)?\s*[:=：]?\s*(\d+)",
        r"(\d+)\s*号?\s*(?:回复|楼层|楼中楼)",
    )
    common_patterns = (
        r"(?:target_id|targetid)\s*[:=：]\s*(\d+)",
        r"\bid\s*[:=：]\s*(\d+)",
    )

    if target == "thread":
        patterns = (*thread_patterns, *common_patterns)
    elif target == "reply":
        patterns = (*reply_patterns, *common_patterns)
    else:
        patterns = (*thread_patterns, *reply_patterns, *common_patterns)

    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue
        try:
            return int(m.group(1))
        except Exception:
            continue

    return None


def _truncate(text: str, max_chars: int) -> str:
    text = str(text or "")
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        fixed = repair_json(text)
        data = json.loads(fixed)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _wants_auto_reply(text: str) -> bool:
    """Heuristic: whether user asks the bot to generate a reply by itself."""

    text = str(text or "").strip()
    if not text:
        return False

    return bool(
        re.search(
            r"(自动|自主|你来|你自己|帮我.*(生成|写|拟|回复)|根据.*(内容|上下文).*回复)",
            text,
        )
    )


def _wants_latest_thread(text: str) -> bool:
    """Heuristic: whether user asks about the latest/recent thread."""

    text = str(text or "").strip()
    if not text:
        return False

    return bool(
        re.search(
            r"(最新|最近|latest).{0,8}(帖子|贴子|一帖|一贴|主题|帖子们|帖子呢)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _normalize_title(text: str) -> str:
    text = str(text or "").strip().lower()
    # Remove some punctuations and whitespace for better matching.
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[“”\"'‘’]+", "", text)
    return text


def _extract_thread_title(text: str) -> str | None:
    """Extract a thread title from user message.

    Prefer the common Chinese book-title quotes: 《...》.
    """

    text = str(text or "").strip()
    if not text:
        return None

    m = re.search(r"《([^》]{2,120})》", text)
    if m:
        return m.group(1).strip()

    m = re.search(r"(?:标题|title)\s*[:=：]\s*([^\n]{2,120})", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Fallback: try to capture the phrase after "回复/回帖/查看/阅读".
    m = re.search(r"(?:回复|回帖|回贴|查看|阅读|读帖|读贴|看帖|看贴)\s+([^\n]{2,120})", text)
    if m:
        candidate = m.group(1).strip()
        # Avoid picking obvious parameter strings like "thread_id=123".
        if "thread_id" not in candidate and "reply_id" not in candidate and "content=" not in candidate:
            return candidate

    return None


def _format_thread_candidates(items: list[dict[str, Any]], *, limit: int = 5) -> str:
    lines = ["找到多个匹配的帖子，请指定 thread_id（例如：回帖 thread_id=16 content=...）："]
    for item in items[: max(1, limit)]:
        tid = item.get("id")
        title = str(item.get("title", "") or "").strip()
        if isinstance(tid, int):
            lines.append(f"- {tid}: {title or '（无标题）'}")
    return "\n".join(lines)


def _extract_threads_from_browse_text(text: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Best-effort parse thread entries from browse_threads(text) output."""

    text = str(text or "")
    items: list[dict[str, Any]] = []

    # Common format: "[16] [Tech] title ..."
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        m = re.match(r"^\[(\d+)\]\s*(?:\[[^\]]+\]\s*)?(.*)$", line)
        if m:
            try:
                tid = int(m.group(1))
            except Exception:
                continue
            title = (m.group(2) or "").strip()
            items.append({"id": tid, "title": title})
            if len(items) >= max(1, limit):
                return items

    # Fallback: try "ID: 16" style lines.
    for line in text.splitlines():
        line = line.strip()
        m = re.search(r"\bID[:：]\s*(\d+)\b", line, flags=re.IGNORECASE)
        if not m:
            continue
        try:
            tid = int(m.group(1))
        except Exception:
            continue
        items.append({"id": tid, "title": line})
        if len(items) >= max(1, limit):
            return items

    return items


async def _resolve_latest_thread_id(
    client: AstrBookClient,
    *,
    category: str | None = None,
) -> Tuple[int | None, str | None]:
    """Resolve latest thread_id by listing threads (JSON preferred, text fallback)."""

    candidates, err = await _get_latest_thread_candidates(client, category=category)
    if not candidates:
        return None, err or "无法获取最新帖子。"

    tid = candidates[0].get("id")
    return (int(tid), None) if isinstance(tid, int) else (None, "无法解析最新 thread_id。")


def _extract_thread_items_from_list_result(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("items", "threads", "data", "results", "list"):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break
        else:
            items = []
    else:
        items = []

    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tid = it.get("id", None)
        if tid is None:
            tid = it.get("thread_id", None)
        if isinstance(tid, str) and tid.isdigit():
            try:
                tid = int(tid)
            except Exception:
                tid = None
        if not isinstance(tid, int):
            continue

        title = str(it.get("title", "") or it.get("thread_title", "") or "").strip()
        pinned = bool(it.get("is_pinned") or it.get("pinned") or it.get("is_top") or it.get("top"))
        if "置顶" in title or "pinned" in title.lower():
            pinned = True

        out.append({"id": tid, "title": title, "pinned": pinned})

    # Deduplicate by id while preserving order.
    dedup: dict[int, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for it in out:
        tid = it.get("id")
        if not isinstance(tid, int) or tid in dedup:
            continue
        dedup[tid] = it
        ordered.append(it)
    return ordered


async def _get_latest_thread_candidates(
    client: AstrBookClient,
    *,
    category: str | None = None,
) -> Tuple[list[dict[str, Any]], str | None]:
    """Get latest thread candidates from list_threads (JSON) or browse_threads(text)."""

    items: list[dict[str, Any]] = []

    try:
        result = await client.list_threads(page=1, page_size=10, category=category)
    except Exception as e:
        result = {"error": str(e)}

    if isinstance(result, dict) and "error" in result:
        items = []
    else:
        items = _extract_thread_items_from_list_result(result)

    if not items:
        # Fallback to the text output and parse.
        result2 = await client.browse_threads(page=1, page_size=10, category=category)
        if isinstance(result2, dict) and "error" in result2:
            return [], f"获取帖子列表失败：{result2['error']}"

        browse_text = ""
        if isinstance(result2, dict):
            browse_text = str(result2.get("text") or "")
        items = _extract_threads_from_browse_text(browse_text, limit=10)
        for it in items:
            title = str(it.get("title", "") or "")
            if "置顶" in title or "pinned" in title.lower():
                it["pinned"] = True
            else:
                it["pinned"] = False

    if not items:
        return [], "无法从帖子列表解析 thread_id，请先手动浏览帖子列表。"

    # Prefer non-pinned entries, and always sort by id descending as newest-first fallback.
    non_pinned = sorted(
        [it for it in items if not bool(it.get("pinned", False))],
        key=lambda it: int(it.get("id", 0)),
        reverse=True,
    )
    pinned = sorted(
        [it for it in items if bool(it.get("pinned", False))],
        key=lambda it: int(it.get("id", 0)),
        reverse=True,
    )
    return (non_pinned + pinned), None


async def _resolve_thread_id_by_title(
    client: AstrBookClient,
    *,
    title_or_keyword: str,
    prefer_exact_title: str | None = None,
) -> Tuple[int | None, str | None]:
    title_or_keyword = str(title_or_keyword or "").strip()
    if not title_or_keyword:
        return None, "缺少帖子标题/关键词。"

    result = await client.search_threads(keyword=title_or_keyword, page=1, category=None)
    if "error" in result:
        return None, f"搜索帖子失败：{result['error']}"

    items_raw = result.get("items", [])
    if not isinstance(items_raw, list):
        items_raw = []
    items: list[dict[str, Any]] = [it for it in items_raw if isinstance(it, dict)]
    if not items:
        return None, f"没有找到包含“{title_or_keyword}”的帖子。"

    prefer = prefer_exact_title or title_or_keyword
    prefer_norm = _normalize_title(prefer)
    if prefer_norm:
        strong_matches: list[dict[str, Any]] = []
        for it in items:
            t = _normalize_title(it.get("title", ""))
            if not t:
                continue
            if t == prefer_norm or prefer_norm in t or t in prefer_norm:
                strong_matches.append(it)

        if len(strong_matches) == 1 and isinstance(strong_matches[0].get("id"), int):
            return int(strong_matches[0]["id"]), None

    if (result.get("total") == 1 or len(items) == 1) and isinstance(items[0].get("id"), int):
        return int(items[0]["id"]), None

    return None, _format_thread_candidates(items)


class _AstrBookAction(BaseAction):
    """Shared helpers for AstrBook forum actions."""

    def _get_service(self) -> AstrBookService:
        svc = get_astrbook_service()
        if svc:
            svc.update_config(self.plugin_config)
            return svc
        return AstrBookService(self.plugin_config)

    def _get_client(self) -> AstrBookClient:
        return self._get_service().client

    def _get_memory(self) -> ForumMemory:
        return self._get_service().memory

    async def _ensure_token(self) -> bool:
        client = self._get_client()
        if client.token_configured:
            return True
        await self.send_text("AstrBook token 未配置，请在插件配置 `astrbook.token` 中填写。")
        return False


class AstrBookBrowseThreadsAction(_AstrBookAction):
    action_name = "astrbook_browse_threads"
    action_description = "浏览 AstrBook 论坛帖子列表，并把列表发到聊天中。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["逛论坛", "浏览论坛", "帖子列表", "看看论坛", "论坛有什么", "browse_threads"]
    parallel_action = False

    action_parameters = {
        "page": "页码，从 1 开始，默认 1",
        "page_size": "每页数量，默认 10，最大 50",
        "category": "分类筛选（可选）：chat/deals/misc/tech/help/intro/acg；不填表示全部",
    }
    action_require = ["当用户想浏览论坛帖子列表时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        page = _coerce_int(self.action_data.get("page")) or 1
        page_size = _coerce_int(self.action_data.get("page_size")) or 10
        page_size = max(1, min(50, page_size))
        category = str(self.action_data.get("category", "") or "").strip() or None
        if category and category not in VALID_CATEGORIES:
            category = None

        result = await self._get_client().browse_threads(page=page, page_size=page_size, category=category)
        if "error" in result:
            await self.send_text(f"获取帖子列表失败：{result['error']}")
            return False, "browse_threads failed"

        content = str(result.get("text") or "").strip()
        if not content:
            await self.send_text("论坛帖子列表为空或返回异常。")
            return False, "empty browse_threads"

        await self.send_text(_truncate(content, 3800))
        self._get_memory().add_memory("browsed", "我浏览了 AstrBook 论坛帖子列表。", metadata={"category": category})
        return True, "browsed threads"


class AstrBookSearchThreadsAction(_AstrBookAction):
    action_name = "astrbook_search_threads"
    action_description = "按关键词搜索 AstrBook 论坛帖子，并把搜索结果发到聊天中。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["搜索帖子", "搜帖子", "查帖子", "搜索论坛", "search_threads"]
    parallel_action = False

    action_parameters = {
        "keyword": "搜索关键词（必填）",
        "page": "页码，默认 1",
        "category": "分类筛选（可选）：chat/deals/misc/tech/help/intro/acg；不填表示全部",
    }
    action_require = ["当用户想按关键词搜索论坛帖子时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        keyword = str(self.action_data.get("keyword", "") or "").strip()
        if not keyword and self.action_message:
            keyword = str(getattr(self.action_message, "processed_plain_text", "") or "").strip()
        if not keyword:
            await self.send_text("请提供搜索关键词，例如：搜索帖子 关键词=xxx")
            return False, "missing keyword"

        page = _coerce_int(self.action_data.get("page")) or 1
        category = str(self.action_data.get("category", "") or "").strip() or None
        if category and category not in VALID_CATEGORIES:
            category = None

        result = await self._get_client().search_threads(keyword=keyword, page=page, category=category)
        if "error" in result:
            await self.send_text(f"搜索失败：{result['error']}")
            return False, "search_threads failed"

        items = result.get("items", [])
        total = result.get("total", 0)
        if not total or not items:
            await self.send_text(f"没有找到包含“{keyword}”的帖子。")
            return True, "no results"

        category_names = {
            "chat": "Chat",
            "deals": "Deals",
            "misc": "Misc",
            "tech": "Tech",
            "help": "Help",
            "intro": "Intro",
            "acg": "ACG",
        }
        lines = [f"🔍 Search Results for '{keyword}' ({total} found):\n"]
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or "id" not in item or "title" not in item:
                continue
            cat = category_names.get(item.get("category"), "")
            author = item.get("author", {}) if isinstance(item.get("author"), dict) else {}
            author_name = author.get("nickname") or author.get("username", "Unknown")
            lines.append(f"[{item['id']}] [{cat}] {item['title']}")
            lines.append(f"    by @{author_name} | {item.get('reply_count', 0)} replies")
            if item.get("content_preview"):
                lines.append(f"    {str(item['content_preview'])[:80]}...")
            lines.append("")

        if result.get("total_pages", 1) > 1:
            lines.append(
                f"Page {result.get('page', 1)}/{result.get('total_pages', 1)} - Use page parameter to see more"
            )

        await self.send_text(_truncate("\n".join(lines), 3800))
        self._get_memory().add_memory("browsed", f"我搜索了论坛帖子：{keyword}", metadata={"keyword": keyword})
        return True, "searched threads"


class AstrBookReadThreadAction(_AstrBookAction):
    action_name = "astrbook_read_thread"
    action_description = "阅读 AstrBook 论坛某个帖子（正文 + 部分楼层回复），并把内容发到聊天中。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = [
        "号帖",
        "号贴",
        "最新的帖子",
        "最新帖子",
        "最近的帖子",
        "最近帖子",
        "帖子ID",
        "贴子ID",
        "帖子内容",
        "贴子内容",
        "看帖",
        "看贴",
        "读帖",
        "读贴",
        "read_thread",
        "thread_id",
    ]
    parallel_action = False

    action_parameters = {
        "thread_id": "帖子 ID（可选；数字）。若未知可用 keyword/title 搜索",
        "keyword": "帖子标题/关键词（可选）；当未提供 thread_id 或 thread_id 不存在时用于搜索",
        "page": "楼层页码，默认 1",
    }
    action_require = ["当用户明确要求查看/阅读某个帖子内容时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        svc = self._get_service()

        user_req = ""
        if self.action_message:
            user_req = str(getattr(self.action_message, "processed_plain_text", "") or "").strip()

        keyword = str(self.action_data.get("keyword", "") or "").strip()
        thread_id = _coerce_int(self.action_data.get("thread_id"))
        if thread_id is None and self.action_message:
            thread_id = _extract_first_int(user_req)

        wants_latest = _wants_latest_thread(user_req)
        latest_candidates: list[dict[str, Any]] | None = None

        # If thread_id missing, try searching by title/keyword.
        if thread_id is None:
            if wants_latest:
                latest_candidates, err = await _get_latest_thread_candidates(svc.client, category=None)
                if not latest_candidates:
                    await self.send_text(err or "无法获取最新帖子，请先浏览帖子列表。")
                    return False, "missing thread_id"
                tid = latest_candidates[0].get("id")
                if not isinstance(tid, int):
                    await self.send_text("无法解析最新 thread_id，请先浏览帖子列表。")
                    return False, "missing thread_id"
                thread_id = tid
            else:
                title = _extract_thread_title(user_req)
                keyword = keyword or (title or "")
                if not keyword and user_req and 2 <= len(user_req) <= 80:
                    keyword = user_req
                if keyword:
                    resolved_id, err = await _resolve_thread_id_by_title(
                        svc.client,
                        title_or_keyword=keyword,
                        prefer_exact_title=title,
                    )
                    if resolved_id is None:
                        await self.send_text(err or "无法通过标题搜索到帖子，请提供 thread_id。")
                        return False, "missing thread_id"
                    thread_id = resolved_id
                else:
                    await self.send_text("请提供 thread_id，或在消息里用《标题》标注帖子标题。")
                    return False, "missing thread_id"

        page = _coerce_int(self.action_data.get("page")) or 1
        page = max(1, page)

        result = await svc.client.read_thread(thread_id=thread_id, page=page)
        if "error" in result:
            # Fallback: if thread_id was wrong (planner guessed), try search by title.
            err_text = str(result.get("error") or "")
            if ("not found" in err_text.lower() or "404" in err_text) and user_req:
                if wants_latest:
                    if latest_candidates is None:
                        latest_candidates, err2 = await _get_latest_thread_candidates(
                            svc.client, category=None
                        )
                    else:
                        err2 = None

                    if not latest_candidates:
                        err_text = err2 or err_text
                    else:
                        last_err = err_text
                        for cand in latest_candidates:
                            tid = cand.get("id")
                            if not isinstance(tid, int) or tid == thread_id:
                                continue

                            trial = await svc.client.read_thread(thread_id=tid, page=page)
                            if "error" not in trial:
                                result = trial
                                thread_id = tid
                                err_text = ""
                                break

                            cand_err = str(trial.get("error") or "")
                            last_err = cand_err or last_err
                            if "not found" in cand_err.lower() or "404" in cand_err:
                                continue

                            err_text = cand_err or last_err
                            break
                        else:
                            err_text = last_err
                else:
                    title = _extract_thread_title(user_req)
                    fallback_kw = keyword or title
                    if not fallback_kw and 2 <= len(user_req) <= 80:
                        fallback_kw = user_req
                    if fallback_kw:
                        resolved_id, err2 = await _resolve_thread_id_by_title(
                            svc.client,
                            title_or_keyword=fallback_kw,
                            prefer_exact_title=title,
                        )
                        if resolved_id is not None and resolved_id != thread_id:
                            result = await svc.client.read_thread(thread_id=resolved_id, page=page)
                            if "error" not in result:
                                thread_id = resolved_id
                                err_text = ""
                            else:
                                err_text = str(result.get("error") or err_text)
                        else:
                            err_text = err2 or err_text

            if "error" in result:
                await self.send_text(f"读取帖子失败：{err_text}")
                return False, "read_thread failed"

        text = str(result.get("text") or "").strip()
        if not text:
            await self.send_text("帖子内容为空或返回异常。")
            return False, "empty thread text"

        if len(text) > 3800:
            text = text[:3770] + "…\n\n（内容较长，已截断；可通过 page 参数查看更多楼层。）"

        await self.send_text(text)
        self._get_memory().add_memory("browsed", f"我查看了帖子ID:{thread_id}", metadata={"thread_id": thread_id})
        return True, f"read thread {thread_id}"


class AstrBookCreateThreadAction(_AstrBookAction):
    action_name = "astrbook_create_thread"
    action_description = "在 AstrBook 论坛发布一个新主题帖子。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = [
        "开一个帖子",
        "开个帖子",
        "开一个新帖",
        "开个新帖",
        "发到论坛",
        "发到论坛上",
        "发帖",
        "发贴",
        "发一个帖子",
        "发一个贴",
        "发个帖子",
        "发个贴",
        "发布帖子",
        "发布贴",
        "新帖",
        "新贴",
        "create_thread",
    ]
    parallel_action = False

    action_parameters = {
        "title": "帖子标题，2-100 字符（必填；若用户未提供可由你生成）",
        "content": "帖子内容，至少 5 字符（必填；若用户未提供可由你生成）",
        "category": "分类：chat/deals/misc/tech/help/intro/acg，默认 chat",
    }
    action_require = ["当用户明确要求在论坛发新帖时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        svc = self._get_service()

        title = str(self.action_data.get("title", "") or "").strip()
        content = str(self.action_data.get("content", "") or "").strip()
        category = str(self.action_data.get("category", "chat") or "chat").strip()
        if category not in VALID_CATEGORIES:
            category = "chat"

        # Fallback: ask model to draft if user didn't provide title/content.
        if (not title or not content) and self.action_message:
            user_req = str(getattr(self.action_message, "processed_plain_text", "") or "").strip()
            if user_req:
                from src.config.config import model_config
                from src.plugin_system.apis import llm_api

                from .prompting import build_forum_persona_block

                persona_block = build_forum_persona_block()
                profile_block = await svc.get_profile_context_block()
                prompt = f"""
{persona_block}
{profile_block}

用户希望你在 AstrBook 论坛发一个新帖，但他/她的请求可能没有提供完整的标题或正文。

用户请求：
{user_req}

允许的分类：{VALID_CATEGORIES}

请输出严格 JSON（不要输出其他内容）：
{{"category":"chat","title":"...","content":"..."}}

要求：
1) title 2-100 字符
2) content 至少 50 字符，尽量不超过 1200 字符
""".strip()

                ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
                    prompt=prompt,
                    model_config=model_config.model_task_config.replyer,
                    request_type="astrbook.action.create_thread.draft",
                    temperature=0.7,
                    max_tokens=8192,
                )
                if ok:
                    data = _parse_json_object(resp)
                    if data:
                        title = title or str(data.get("title", "") or "").strip()
                        content = content or str(data.get("content", "") or "").strip()
                        cat2 = str(data.get("category", "") or "").strip()
                        if cat2 in VALID_CATEGORIES:
                            category = cat2
                else:
                    logger.warning("[actions] draft create_thread failed: %s", resp)

        if len(title) < 2 or len(title) > 100:
            await self.send_text("发帖失败：title 需要 2-100 字符。")
            return False, "invalid title"
        if len(content) < 5:
            await self.send_text("发帖失败：content 至少 5 字符。")
            return False, "invalid content"

        result = await svc.client.create_thread(title=title, content=content, category=category)
        if "error" in result:
            await self.send_text(f"发帖失败：{result['error']}")
            return False, "create_thread failed"

        thread_id = result.get("id")
        if isinstance(thread_id, int):
            svc.memory.add_memory(
                "created",
                f"我在 AstrBook 发了一个新帖《{title}》(ID:{thread_id})",
                metadata={"thread_id": thread_id, "category": category},
            )

        await self.send_text(f"Thread created! ID: {thread_id}, Title: {result.get('title', title)}")
        return True, "thread created"


class AstrBookReplyThreadAction(_AstrBookAction):
    action_name = "astrbook_reply_thread"
    action_description = "回复 AstrBook 论坛帖子（可手动指定 content，或留空让 bot 读帖后自动生成回复）。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = [
        "回帖",
        "回贴",
        "号帖",
        "号贴",
        "最新的帖子",
        "最新帖子",
        "最近的帖子",
        "最近帖子",
        "回复帖子",
        "回复贴子",
        "评论帖子",
        "评论贴子",
        "reply_thread",
    ]
    parallel_action = False

    action_parameters = {
        "thread_id": "帖子 ID（可选；数字）。若未知可用 thread_title/keyword 搜索",
        "thread_title": "帖子标题（可选）。当未提供 thread_id 时用于搜索",
        "keyword": "标题关键词（可选）。当未提供 thread_id 时用于搜索",
        "content": "手动回帖内容（可选）；不填则自动读帖生成",
        "instruction": "自动生成时的额外要求（可选），例如“更礼貌/更简短/用xx语气”",
        "auto_generate": "是否强制自动生成（可选，true/false）；用户要求“你来自己回/自动回”时为 true",
    }
    action_require = ["当用户明确要求在论坛回帖/回复某个帖子时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        svc = self._get_service()

        user_req = ""
        if self.action_message:
            user_req = str(getattr(self.action_message, "processed_plain_text", "") or "").strip()

        keyword = str(self.action_data.get("keyword", "") or "").strip()
        thread_title = str(self.action_data.get("thread_title", "") or "").strip()

        thread_id = _coerce_int(self.action_data.get("thread_id"))
        if thread_id is None and self.action_message:
            thread_id = _extract_first_int(user_req)

        wants_latest = _wants_latest_thread(user_req)
        latest_candidates: list[dict[str, Any]] | None = None

        # If thread_id missing, try resolving by title/keyword.
        if thread_id is None:
            if wants_latest:
                latest_candidates, err = await _get_latest_thread_candidates(svc.client, category=None)
                if not latest_candidates:
                    await self.send_text(err or "无法获取最新帖子，请先浏览帖子列表。")
                    return False, "missing thread_id"
                tid = latest_candidates[0].get("id")
                if not isinstance(tid, int):
                    await self.send_text("无法解析最新 thread_id，请先浏览帖子列表。")
                    return False, "missing thread_id"
                thread_id = tid
            else:
                extracted_title = _extract_thread_title(user_req)
                prefer_title = thread_title or extracted_title
                search_kw = keyword or prefer_title or ""
                if not search_kw and user_req and 2 <= len(user_req) <= 80:
                    search_kw = user_req
                if search_kw:
                    resolved_id, err = await _resolve_thread_id_by_title(
                        svc.client,
                        title_or_keyword=search_kw,
                        prefer_exact_title=prefer_title,
                    )
                    if resolved_id is None:
                        await self.send_text(err or "无法通过标题搜索到帖子，请提供 thread_id。")
                        return False, "missing thread_id"
                    thread_id = resolved_id

        if thread_id is None:
            await self.send_text("请提供 thread_id，或在消息里用《标题》标注帖子标题。")
            return False, "missing thread_id"

        content = str(self.action_data.get("content", "") or "").strip()
        instruction = str(self.action_data.get("instruction", "") or "").strip()
        auto_generate = bool(_coerce_bool(self.action_data.get("auto_generate")) or False)

        # Auto-generate if user didn't provide content, or user explicitly requests "you reply yourself".
        auto_mode = auto_generate or _wants_auto_reply(user_req) or not content
        if auto_mode:
            # When planner mistakenly fills "content" with user's instruction, treat it as instruction.
            if not instruction and content:
                instruction = content

            thread_result = await svc.client.read_thread(thread_id=thread_id, page=1)
            if "error" in thread_result:
                err_text = str(thread_result.get("error") or "")
                # If planner guessed wrong id, fallback to title search once.
                if ("not found" in err_text.lower() or "404" in err_text) and user_req:
                    if wants_latest:
                        if latest_candidates is None:
                            latest_candidates, err2 = await _get_latest_thread_candidates(
                                svc.client, category=None
                            )
                        else:
                            err2 = None

                        if not latest_candidates:
                            err_text = err2 or err_text
                        else:
                            last_err = err_text
                            for cand in latest_candidates:
                                tid = cand.get("id")
                                if not isinstance(tid, int) or tid == thread_id:
                                    continue

                                trial = await svc.client.read_thread(thread_id=tid, page=1)
                                if "error" not in trial:
                                    thread_id = tid
                                    thread_result = trial
                                    err_text = ""
                                    break

                                cand_err = str(trial.get("error") or "")
                                last_err = cand_err or last_err
                                if "not found" in cand_err.lower() or "404" in cand_err:
                                    continue

                                err_text = cand_err or last_err
                                break
                            else:
                                err_text = last_err
                    else:
                        extracted_title = _extract_thread_title(user_req)
                        prefer_title = thread_title or extracted_title
                        search_kw = keyword or prefer_title
                        if not search_kw and 2 <= len(user_req) <= 80:
                            search_kw = user_req
                        if search_kw:
                            resolved_id, err2 = await _resolve_thread_id_by_title(
                                svc.client,
                                title_or_keyword=search_kw,
                                prefer_exact_title=prefer_title,
                            )
                            if resolved_id is not None and resolved_id != thread_id:
                                thread_id = resolved_id
                                thread_result = await svc.client.read_thread(thread_id=thread_id, page=1)
                                if "error" not in thread_result:
                                    err_text = ""
                                else:
                                    err_text = str(thread_result.get("error") or err_text)
                            else:
                                err_text = err2 or err_text

                if err_text:
                    await self.send_text(f"读取帖子失败：{err_text}")
                    return False, "read_thread failed"

            thread_text = str(thread_result.get("text") or "").strip()
            if not thread_text:
                await self.send_text("读取帖子失败：返回内容为空。")
                return False, "empty thread text"

            from src.config.config import model_config
            from src.plugin_system.apis import llm_api

            from .prompting import build_forum_persona_block, normalize_plain_text

            persona_block = build_forum_persona_block()
            profile_block = await svc.get_profile_context_block()
            extra_req = f"额外要求：{instruction}\n" if instruction else ""
            prompt = f"""
{persona_block}
{profile_block}

用户希望你在 AstrBook 论坛回复一个帖子（thread_id={thread_id}）。
{extra_req}
用户原始请求（供你理解意图，不要原样贴进回复）：
{user_req or '（无）'}

下面是帖子正文与部分楼层（text 格式，可能被截断）：
{_truncate(thread_text, 3500)}

请你写一段将要发布到论坛的回复。

只输出严格 JSON（不要输出其他内容）：
{{"content":"..."}}

要求：
1) content 10-400 字符，简洁有信息量，避免纯水。
2) 直接输出要发的正文（纯文本），不要输出 Markdown 代码块/标题/多余说明。
3) 不要出现“作为AI/作为语言模型”等措辞。
""".strip()

            temperature = svc.get_config_float("realtime.reply_temperature", default=0.6, min_value=0.0, max_value=2.0)
            max_tokens = svc.get_config_int("realtime.reply_max_tokens", default=8192, min_value=64, max_value=8192)

            ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_config.model_task_config.replyer,
                request_type="astrbook.action.reply_thread.auto",
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if not ok:
                logger.warning("[actions] auto reply_thread failed: %s", resp)
                await self.send_text("自动生成回帖失败：模型调用失败。")
                return False, "auto reply_thread llm failed"

            data = _parse_json_object(resp) or {}
            draft = str(data.get("content", "") or "").strip()
            if not draft:
                draft = normalize_plain_text(resp)
            if not draft:
                logger.warning("[actions] auto reply_thread invalid output model=%s: %s", model_name, resp[:200])
                await self.send_text("自动生成回帖失败：模型输出解析失败。")
                return False, "auto reply_thread invalid json"

            content = draft

        result = await svc.client.reply_thread(thread_id=thread_id, content=content)
        if "error" in result:
            err_text = str(result.get("error") or "")
            # Fallback: if wrong id, try to resolve once by title/keyword.
            if ("not found" in err_text.lower() or "404" in err_text) and (keyword or thread_title or user_req):
                if wants_latest:
                    if latest_candidates is None:
                        latest_candidates, err2 = await _get_latest_thread_candidates(
                            svc.client, category=None
                        )
                    else:
                        err2 = None

                    if not latest_candidates:
                        err_text = err2 or err_text
                    else:
                        last_err = err_text
                        for cand in latest_candidates:
                            tid = cand.get("id")
                            if not isinstance(tid, int) or tid == thread_id:
                                continue

                            trial = await svc.client.reply_thread(thread_id=tid, content=content)
                            if "error" not in trial:
                                thread_id = tid
                                result = trial
                                err_text = ""
                                break

                            cand_err = str(trial.get("error") or "")
                            last_err = cand_err or last_err
                            if "not found" in cand_err.lower() or "404" in cand_err:
                                continue

                            err_text = cand_err or last_err
                            break
                        else:
                            err_text = last_err
                else:
                    extracted_title = _extract_thread_title(user_req)
                    prefer_title = thread_title or extracted_title
                    search_kw = keyword or prefer_title
                    if not search_kw and user_req and 2 <= len(user_req) <= 80:
                        search_kw = user_req
                    if search_kw:
                        resolved_id, err2 = await _resolve_thread_id_by_title(
                            svc.client,
                            title_or_keyword=search_kw,
                            prefer_exact_title=prefer_title,
                        )
                        if resolved_id is not None and resolved_id != thread_id:
                            thread_id = resolved_id
                            result = await svc.client.reply_thread(thread_id=thread_id, content=content)
                            if "error" not in result:
                                err_text = ""
                            else:
                                err_text = str(result.get("error") or err_text)
                        else:
                            err_text = err2 or err_text

            if err_text:
                await self.send_text(f"回帖失败：{err_text}")
                return False, "reply_thread failed"

        svc.memory.add_memory(
            "replied",
            f"我回复了帖子ID:{thread_id}: {content[:60]}",
            metadata={"thread_id": thread_id},
        )
        prefix = "已自动生成并回帖" if auto_mode else "回帖成功"
        await self.send_text(f"{prefix}（thread_id={thread_id}）\n{_truncate(content, 1200)}")
        return True, "replied thread"


class AstrBookReplyFloorAction(_AstrBookAction):
    action_name = "astrbook_reply_floor"
    action_description = "楼中楼回复（可手动指定 content，或留空让 bot 根据上下文自动生成）。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["楼中楼", "回复楼层", "reply_floor", "sub_reply"]
    parallel_action = False

    action_parameters = {
        "thread_id": "（可选）帖子 ID；提供后可读取帖子上下文，生成更贴合的楼中楼回复",
        "reply_id": "楼层/回复 ID（必填，数字）",
        "content": "手动楼中楼回复内容（可选）；不填则自动生成",
        "instruction": "自动生成时的额外要求（可选），例如“更简短/更礼貌”",
        "auto_generate": "是否强制自动生成（可选，true/false）；用户要求“你来自己回/自动回”时为 true",
    }
    action_require = ["当用户明确要求在楼中楼继续回复时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        svc = self._get_service()

        thread_id = _coerce_int(self.action_data.get("thread_id"))
        reply_id = _coerce_int(self.action_data.get("reply_id"))
        if reply_id is None and self.action_message:
            reply_id = _extract_first_int(str(getattr(self.action_message, "processed_plain_text", "") or ""))
        if reply_id is None:
            await self.send_text("请提供 reply_id，例如：楼中楼回复 reply_id=123 content=...")
            return False, "missing reply_id"

        user_req = ""
        if self.action_message:
            user_req = str(getattr(self.action_message, "processed_plain_text", "") or "").strip()

        content = str(self.action_data.get("content", "") or "").strip()
        instruction = str(self.action_data.get("instruction", "") or "").strip()
        auto_generate = bool(_coerce_bool(self.action_data.get("auto_generate")) or False)

        auto_mode = auto_generate or _wants_auto_reply(user_req) or not content
        if auto_mode:
            if not instruction and content:
                instruction = content

            ctx_result = await svc.client.get_sub_replies(reply_id=reply_id, page=1)
            if "error" in ctx_result:
                await self.send_text(f"获取楼中楼上下文失败：{ctx_result['error']}")
                return False, "get_sub_replies failed"

            ctx_text = str(ctx_result.get("text") or "").strip()
            if not ctx_text:
                await self.send_text("无法获取楼中楼上下文，请手动提供 content。")
                return False, "empty sub_replies context"

            thread_text = ""
            if isinstance(thread_id, int):
                thread_result = await svc.client.read_thread(thread_id=thread_id, page=1)
                if "text" in thread_result:
                    thread_text = str(thread_result.get("text") or "").strip()

            from src.config.config import model_config
            from src.plugin_system.apis import llm_api

            from .prompting import build_forum_persona_block, normalize_plain_text

            persona_block = build_forum_persona_block()
            profile_block = await svc.get_profile_context_block()
            extra_req = f"额外要求：{instruction}\n" if instruction else ""
            thread_ctx_block = (
                f"\n下面是帖子正文与部分楼层（text 格式，可能被截断）：\n{_truncate(thread_text, 2500)}\n"
                if thread_text
                else ""
            )
            prompt = f"""
{persona_block}
{profile_block}

用户希望你在 AstrBook 论坛进行一次楼中楼回复（reply_id={reply_id}）。
{extra_req}
用户原始请求（供你理解意图，不要原样贴进回复）：
{user_req or '（无）'}

{thread_ctx_block}

下面是该楼层与楼中楼回复上下文（text 格式，可能被截断）：
{_truncate(ctx_text, 3500)}

请你写一段将要发布到楼中楼的回复。

只输出严格 JSON（不要输出其他内容）：
{{"content":"..."}}

要求：
1) content 10-300 字符，简洁有信息量，避免纯水。
2) 直接输出要发的正文（纯文本），不要输出 Markdown 代码块/标题/多余说明。
3) 不要出现“作为AI/作为语言模型”等措辞。
""".strip()

            temperature = svc.get_config_float("realtime.reply_temperature", default=0.6, min_value=0.0, max_value=2.0)
            max_tokens = svc.get_config_int("realtime.reply_max_tokens", default=8192, min_value=64, max_value=8192)

            ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
                prompt=prompt,
                model_config=model_config.model_task_config.replyer,
                request_type="astrbook.action.reply_floor.auto",
                temperature=temperature,
                max_tokens=max_tokens,
            )
            if not ok:
                logger.warning("[actions] auto reply_floor failed: %s", resp)
                await self.send_text("自动生成楼中楼回复失败：模型调用失败。")
                return False, "auto reply_floor llm failed"

            data = _parse_json_object(resp) or {}
            draft = str(data.get("content", "") or "").strip()
            if not draft:
                draft = normalize_plain_text(resp)
            if not draft:
                logger.warning("[actions] auto reply_floor invalid output model=%s: %s", model_name, resp[:200])
                await self.send_text("自动生成楼中楼回复失败：模型输出解析失败。")
                return False, "auto reply_floor invalid json"

            content = draft

        result = await svc.client.reply_floor(reply_id=reply_id, content=content)
        if "error" in result:
            await self.send_text(f"楼中楼回复失败：{result['error']}")
            return False, "reply_floor failed"

        svc.memory.add_memory(
            "replied",
            f"我进行了楼中楼回复(reply_id={reply_id}): {content[:60]}",
            metadata={"reply_id": reply_id},
        )
        prefix = "已自动生成并楼中楼回复" if auto_mode else "楼中楼回复成功"
        await self.send_text(f"{prefix}（reply_id={reply_id}）\n{_truncate(content, 1200)}")
        return True, "replied floor"


class AstrBookGetSubRepliesAction(_AstrBookAction):
    action_name = "astrbook_get_sub_replies"
    action_description = "获取某一层的楼中楼回复列表，并把列表发到聊天中。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["查看楼中楼", "楼中楼列表", "get_sub_replies"]
    parallel_action = False

    action_parameters = {
        "reply_id": "楼层/回复 ID（必填，数字）",
        "page": "页码，默认 1",
    }
    action_require = ["当用户想查看某层的楼中楼回复列表时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        reply_id = _coerce_int(self.action_data.get("reply_id"))
        if reply_id is None and self.action_message:
            reply_id = _extract_first_int(str(getattr(self.action_message, "processed_plain_text", "") or ""))
        if reply_id is None:
            await self.send_text("请提供 reply_id，例如：查看楼中楼 reply_id=123")
            return False, "missing reply_id"

        page = _coerce_int(self.action_data.get("page")) or 1
        page = max(1, page)

        result = await self._get_client().get_sub_replies(reply_id=reply_id, page=page)
        if "error" in result:
            await self.send_text(f"获取楼中楼失败：{result['error']}")
            return False, "get_sub_replies failed"

        content = str(result.get("text") or "").strip()
        if not content:
            await self.send_text("楼中楼列表为空或返回异常。")
            return False, "empty sub replies"

        await self.send_text(_truncate(content, 3800))
        return True, "got sub replies"


class AstrBookGetMyProfileAction(_AstrBookAction):
    action_name = "astrbook_get_my_profile"
    action_description = "查看我在 AstrBook 的个人资料（昵称、等级、经验等）。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["我的资料", "个人资料", "我的等级", "我的经验", "profile", "get_my_profile"]
    parallel_action = False

    action_parameters: dict[str, str] = {}
    action_require = ["当用户想查看自己在论坛的资料/等级/经验时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        result = await self._get_client().get_my_profile()
        if "error" in result:
            await self.send_text(f"获取个人资料失败：{result['error']}")
            return False, "get_my_profile failed"

        username = str(result.get("username", "未知用户") or "未知用户")
        nickname = str(result.get("nickname", "") or "").strip() or username
        level = result.get("level", 1)
        exp = result.get("exp", 0)
        avatar = str(result.get("avatar", "") or "").strip() or "未设置"
        persona = str(result.get("persona", "") or "").strip() or "未设置"
        created_at = str(result.get("created_at", "未知") or "未知")

        if len(persona) > 80:
            persona = persona[:77] + "..."

        lines = [
            "📋 我的论坛资料：",
            f"- 用户名：@{username}",
            f"- 昵称：{nickname}",
            f"- 等级：Lv.{level}",
            f"- 经验：{exp} EXP",
            f"- 头像：{avatar}",
            f"- 人设：{persona}",
            f"- 注册时间：{created_at}",
        ]
        await self.send_text("\n".join(lines))
        return True, "got my profile"


class AstrBookLikeContentAction(_AstrBookAction):
    action_name = "astrbook_like_content"
    action_description = "给 AstrBook 帖子或回复点赞。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["点赞", "点个赞", "赞一下", "like", "like_content"]
    parallel_action = False

    action_parameters = {
        "target_type": "目标类型：thread 或 reply",
        "target_id": "目标 ID（帖子ID或回复ID）",
    }
    action_require = ["当用户明确要点赞帖子或回复时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        svc = self._get_service()

        user_req = ""
        if self.action_message:
            user_req = str(getattr(self.action_message, "processed_plain_text", "") or "").strip()

        wants_latest = _wants_latest_thread(user_req)
        latest_candidates: list[dict[str, Any]] | None = None

        target_type = str(self.action_data.get("target_type", "") or "").strip().lower()
        if target_type not in {"thread", "reply"}:
            if re.search(r"(回复|楼层|楼中楼|reply)", user_req, flags=re.IGNORECASE):
                target_type = "reply"
            elif re.search(r"(帖子|贴子|thread|主题)", user_req, flags=re.IGNORECASE):
                target_type = "thread"

        target_id = _coerce_int(self.action_data.get("target_id"))
        if target_id is None:
            if target_type == "thread":
                target_id = _coerce_int(self.action_data.get("thread_id"))
            elif target_type == "reply":
                target_id = _coerce_int(self.action_data.get("reply_id"))

        if target_id is None and user_req:
            target_id = _extract_target_id_from_text(user_req, target_type=target_type)

        if target_type == "thread" and target_id is None and wants_latest:
            latest_candidates, err = await _get_latest_thread_candidates(svc.client, category=None)
            if not latest_candidates:
                await self.send_text(err or "无法获取最新帖子，请先浏览帖子列表。")
                return False, "missing target_id"
            latest_id = latest_candidates[0].get("id")
            if not isinstance(latest_id, int):
                await self.send_text("无法解析最新帖子 ID，请先浏览帖子列表。")
                return False, "missing target_id"
            target_id = latest_id

        if target_type == "thread" and target_id is None and user_req:
            title = _extract_thread_title(user_req)
            if title:
                resolved_id, err = await _resolve_thread_id_by_title(
                    svc.client,
                    title_or_keyword=title,
                    prefer_exact_title=title,
                )
                if resolved_id is not None:
                    target_id = resolved_id
                else:
                    await self.send_text(err or "无法通过标题定位帖子，请补充 thread_id。")
                    return False, "missing target_id"

        if target_type not in {"thread", "reply"}:
            await self.send_text("请提供 target_type=thread/reply，例如：点赞 target_type=thread target_id=585")
            return False, "invalid target_type"
        if target_id is None:
            await self.send_text("请提供 target_id，或说“给最新的帖子点赞”。")
            return False, "missing target_id"

        chat_key = str(getattr(self, "chat_id", "") or "global")
        now = time.time()
        _cleanup_recent_like_actions(now)
        dedupe_key = (chat_key, target_type, target_id)
        last_ts = _RECENT_LIKE_ACTIONS.get(dedupe_key)
        if isinstance(last_ts, float) and now - last_ts <= _LIKE_ACTION_DEDUPE_WINDOW_SEC:
            logger.info(
                "[AstrBook] skip duplicate like action in short window: chat=%s type=%s id=%s",
                chat_key,
                target_type,
                target_id,
            )
            return True, "duplicate like ignored"

        result = await svc.client.like_content(target_type=target_type, target_id=target_id)
        if "error" in result:
            err_text = str(result.get("error") or "")
            if (
                target_type == "thread"
                and wants_latest
                and ("not found" in err_text.lower() or "404" in err_text)
            ):
                if latest_candidates is None:
                    latest_candidates, _ = await _get_latest_thread_candidates(svc.client, category=None)
                if latest_candidates:
                    for cand in latest_candidates:
                        cand_id = cand.get("id")
                        if not isinstance(cand_id, int) or cand_id == target_id:
                            continue
                        trial = await svc.client.like_content(target_type="thread", target_id=cand_id)
                        if "error" not in trial:
                            result = trial
                            target_id = cand_id
                            err_text = ""
                            break
                        err_text = str(trial.get("error") or err_text)

            if "error" in result:
                await self.send_text(f"点赞失败：{err_text or result['error']}")
                return False, "like_content failed"

        _RECENT_LIKE_ACTIONS[(chat_key, target_type, target_id)] = time.time()

        liked = bool(result.get("liked", False))
        like_count = _coerce_int(result.get("like_count"))
        like_count_text = str(like_count) if isinstance(like_count, int) else "未知"

        if liked:
            await self.send_text(
                f"点赞成功！该{('帖子' if target_type == 'thread' else '回复')}#{target_id} 当前点赞数：{like_count_text}"
            )
            return True, "liked"

        await self.send_text(f"你已经点过赞了。当前点赞数：{like_count_text}")
        return True, "already liked"


class AstrBookGetBlockListAction(_AstrBookAction):
    action_name = "astrbook_get_block_list"
    action_description = "查看我在 AstrBook 的黑名单列表。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["黑名单", "查看黑名单", "block list", "get_block_list"]
    parallel_action = False

    action_parameters: dict[str, str] = {}
    action_require = ["当用户想查看自己拉黑了哪些用户时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        result = await self._get_client().get_block_list()
        if "error" in result:
            await self.send_text(f"获取黑名单失败：{result['error']}")
            return False, "get_block_list failed"

        items = result.get("items", [])
        total = _coerce_int(result.get("total"))
        if not isinstance(total, int):
            total = len(items) if isinstance(items, list) else 0

        if total <= 0:
            await self.send_text("你的黑名单目前为空。")
            return True, "empty block list"

        lines = [f"🚫 黑名单列表（共 {total} 人）：", ""]
        if not isinstance(items, list):
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            blocked_user = item.get("blocked_user")
            if not isinstance(blocked_user, dict):
                blocked_user = {}
            username = str(blocked_user.get("username", "未知用户") or "未知用户")
            nickname = str(blocked_user.get("nickname", "") or "").strip()
            display_name = nickname or username
            user_id = blocked_user.get("id", "未知")
            lines.append(f"- {display_name} (@{username})，用户ID：{user_id}")

        lines.append("")
        lines.append("可用：取消拉黑 user_id=...")
        await self.send_text("\n".join(lines))
        return True, "got block list"


class AstrBookBlockUserAction(_AstrBookAction):
    action_name = "astrbook_block_user"
    action_description = "将指定用户加入黑名单。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["拉黑", "加入黑名单", "block_user", "block user"]
    parallel_action = False

    action_parameters = {"user_id": "要拉黑的用户 ID（必填）"}
    action_require = ["当用户明确要拉黑某人时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        user_req = ""
        if self.action_message:
            user_req = str(getattr(self.action_message, "processed_plain_text", "") or "").strip()

        user_id = _coerce_int(self.action_data.get("user_id"))
        if user_id is None and self.action_message:
            user_id = _extract_first_int(user_req)
        if user_id is None:
            await self.send_text("请提供 user_id，例如：拉黑 user_id=123")
            return False, "missing user_id"

        result = await self._get_client().block_user(user_id=user_id)
        if "error" in result:
            await self.send_text(f"拉黑失败：{result['error']}")
            return False, "block_user failed"

        blocked_user = result.get("blocked_user")
        if not isinstance(blocked_user, dict):
            blocked_user = {}
        username = str(blocked_user.get("username", "未知用户") or "未知用户")
        await self.send_text(f"已拉黑 @{username}（user_id={user_id}）。")
        return True, "blocked user"


class AstrBookUnblockUserAction(_AstrBookAction):
    action_name = "astrbook_unblock_user"
    action_description = "将指定用户移出黑名单。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["取消拉黑", "解除拉黑", "移出黑名单", "unblock_user", "unblock user"]
    parallel_action = False

    action_parameters = {"user_id": "要取消拉黑的用户 ID（必填）"}
    action_require = ["当用户明确要取消拉黑某人时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        user_req = ""
        if self.action_message:
            user_req = str(getattr(self.action_message, "processed_plain_text", "") or "").strip()

        user_id = _coerce_int(self.action_data.get("user_id"))
        if user_id is None and self.action_message:
            user_id = _extract_first_int(user_req)
        if user_id is None:
            await self.send_text("请提供 user_id，例如：取消拉黑 user_id=123")
            return False, "missing user_id"

        result = await self._get_client().unblock_user(user_id=user_id)
        if "error" in result:
            await self.send_text(f"取消拉黑失败：{result['error']}")
            return False, "unblock_user failed"

        await self.send_text(f"已取消拉黑 user_id={user_id}。")
        return True, "unblocked user"


class AstrBookCheckBlockStatusAction(_AstrBookAction):
    action_name = "astrbook_check_block_status"
    action_description = "检查某个用户是否在黑名单中。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["是否拉黑", "黑名单状态", "check_block_status", "block status"]
    parallel_action = False

    action_parameters = {"user_id": "要检查的用户 ID（必填）"}
    action_require = ["当用户想确认是否拉黑某人时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        user_req = ""
        if self.action_message:
            user_req = str(getattr(self.action_message, "processed_plain_text", "") or "").strip()

        user_id = _coerce_int(self.action_data.get("user_id"))
        if user_id is None and self.action_message:
            user_id = _extract_first_int(user_req)
        if user_id is None:
            await self.send_text("请提供 user_id，例如：是否拉黑 user_id=123")
            return False, "missing user_id"

        result = await self._get_client().check_block_status(user_id=user_id)
        if "error" in result:
            await self.send_text(f"查询黑名单状态失败：{result['error']}")
            return False, "check_block_status failed"

        is_blocked = bool(result.get("is_blocked", False))
        if is_blocked:
            await self.send_text(f"user_id={user_id} 当前在黑名单中。")
            return True, "is blocked"

        await self.send_text(f"user_id={user_id} 当前不在黑名单中。")
        return True, "not blocked"


class AstrBookSearchUsersAction(_AstrBookAction):
    action_name = "astrbook_search_users"
    action_description = "按关键词搜索论坛用户（用于获取 user_id 以便拉黑等操作）。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["查用户", "搜索用户", "search_users", "搜用户"]
    parallel_action = False

    action_parameters = {
        "keyword": "搜索关键词（用户名或昵称）",
        "limit": "返回数量，默认 10，最大 20",
    }
    action_require = ["当用户想查找用户ID（用于拉黑等）时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        user_req = ""
        if self.action_message:
            user_req = str(getattr(self.action_message, "processed_plain_text", "") or "").strip()

        keyword = str(self.action_data.get("keyword", "") or "").strip()
        if not keyword and user_req:
            keyword = re.sub(r"^(查用户|搜索用户|搜用户)\s*", "", user_req).strip()
        if not keyword:
            await self.send_text("请提供 keyword，例如：查用户 keyword=小真寻")
            return False, "missing keyword"

        limit = _coerce_int(self.action_data.get("limit")) or 10
        limit = max(1, min(20, limit))

        result = await self._get_client().search_users(keyword=keyword, limit=limit)
        if "error" in result:
            await self.send_text(f"搜索用户失败：{result['error']}")
            return False, "search_users failed"

        items = result.get("items", [])
        total = _coerce_int(result.get("total"))
        if not isinstance(total, int):
            total = len(items) if isinstance(items, list) else 0

        if total <= 0:
            await self.send_text(f"未找到与“{keyword}”匹配的用户。")
            return True, "no users found"

        lines = [f"🔍 用户搜索结果（关键词：{keyword}，共 {total} 人）：", ""]
        if not isinstance(items, list):
            items = []
        for user in items:
            if not isinstance(user, dict):
                continue
            nickname = str(user.get("nickname", "") or "").strip()
            username = str(user.get("username", "未知用户") or "未知用户")
            user_id = user.get("id", "未知")
            display_name = nickname or username
            lines.append(f"- {display_name} (@{username})，user_id={user_id}")

        lines.append("")
        lines.append("可用：拉黑 user_id=...")
        await self.send_text("\n".join(lines))
        return True, "searched users"


class AstrBookCheckNotificationsAction(_AstrBookAction):
    action_name = "astrbook_check_notifications"
    action_description = "检查 AstrBook 论坛未读通知数量，并把结果发到聊天中。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["未读通知", "通知数量", "check_notifications"]
    parallel_action = False

    action_parameters: dict[str, str] = {}
    action_require = ["当用户想查看论坛未读通知数量时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        result = await self._get_client().check_notifications()
        if "error" in result:
            await self.send_text(f"获取通知失败：{result['error']}")
            return False, "check_notifications failed"

        unread = result.get("unread", 0)
        total = result.get("total", 0)
        if unread and int(unread) > 0:
            await self.send_text(f"You have {unread} unread notifications (total: {total})")
        else:
            await self.send_text("No unread notifications")
        return True, "checked notifications"


class AstrBookGetNotificationsAction(_AstrBookAction):
    action_name = "astrbook_get_notifications"
    action_description = "获取 AstrBook 论坛通知列表（关于回复与提及），并把列表发到聊天中。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["查看通知", "通知列表", "get_notifications"]
    parallel_action = False

    action_parameters = {"unread_only": "是否只获取未读通知，默认 true"}
    action_require = ["当用户想查看论坛通知列表时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        svc = self._get_service()

        unread_only = bool(self.action_data.get("unread_only", True))
        result = await svc.client.get_notifications(unread_only=unread_only)
        if "error" in result:
            await self.send_text(f"获取通知失败：{result['error']}")
            return False, "get_notifications failed"

        items = result.get("items", [])
        total = result.get("total", 0)
        if not items:
            await self.send_text("No notifications")
            return True, "no notifications"

        # Write notification memories so that cross-session recall also works when WS is disabled.
        memory = svc.memory
        existing_notification_ids = {
            m.metadata.get("notification_id")
            for m in memory.get_memories(limit=memory.max_items)
            if isinstance(m.metadata.get("notification_id"), int)
        }
        for n in items if isinstance(items, list) else []:
            if not isinstance(n, dict):
                continue
            notif_id = n.get("id") or n.get("notification_id")
            if isinstance(notif_id, int) and notif_id in existing_notification_ids:
                continue
            if isinstance(notif_id, int):
                existing_notification_ids.add(notif_id)

            notif_type = str(n.get("type", "") or "")
            from_user = n.get("from_user", {}) if isinstance(n.get("from_user"), dict) else {}
            username = str(from_user.get("username", "Unknown") or "Unknown")
            thread_id = n.get("thread_id")
            thread_title = str(n.get("thread_title", "") or "")
            reply_id = n.get("reply_id")
            preview = str(n.get("content_preview") or n.get("content") or "")

            metadata = {
                "notification_id": notif_id,
                "notification_type": notif_type,
                "thread_id": thread_id,
                "reply_id": reply_id,
                "from_user": username,
                "is_read": bool(n.get("is_read")),
            }

            if notif_type == "mention":
                memory.add_memory(
                    "mentioned",
                    f"我在《{thread_title}》中被 @{username} 提及: {preview[:50]}...",
                    metadata=metadata,
                )
            elif notif_type in {"reply", "sub_reply"}:
                memory.add_memory(
                    "replied",
                    f"@{username} 在《{thread_title}》回复了我: {preview[:50]}...",
                    metadata=metadata,
                )

        type_map = {"reply": "💬 Reply", "sub_reply": "↩️ Sub-reply", "mention": "📢 Mention"}
        lines = [f"📬 Notifications ({len(items)}/{total}):\n"]
        for n in items if isinstance(items, list) else []:
            if not isinstance(n, dict):
                continue
            ntype = type_map.get(n.get("type"), n.get("type"))
            from_user = n.get("from_user", {}) if isinstance(n.get("from_user"), dict) else {}
            username = from_user.get("username", "Unknown") or "Unknown"
            thread_id = n.get("thread_id")
            thread_title = (n.get("thread_title") or "")[:30]
            reply_id = n.get("reply_id")
            content = (n.get("content_preview") or "")[:50]
            is_read = "✓" if n.get("is_read") else "●"

            lines.append(f"{is_read} {ntype} from @{username}")
            lines.append(f"   Thread: [{thread_id}] {thread_title}")
            if reply_id:
                lines.append(f"   Reply ID: {reply_id}")
            lines.append(f"   Content: {content}")
            lines.append(
                f"   → To respond: reply_floor(reply_id={reply_id}, content='...')"
                if reply_id
                else f"   → To respond: reply_thread(thread_id={thread_id}, content='...')"
            )
            lines.append("")

        await self.send_text(_truncate("\n".join(lines), 3800))
        return True, "got notifications"


class AstrBookMarkNotificationsReadAction(_AstrBookAction):
    action_name = "astrbook_mark_notifications_read"
    action_description = "标记所有 AstrBook 论坛通知为已读。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["清空通知", "标记已读", "通知已读", "mark_notifications_read"]
    parallel_action = False

    action_parameters: dict[str, str] = {}
    action_require = ["当用户想将论坛通知全部标记为已读时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        result = await self._get_client().mark_notifications_read()
        if "error" in result:
            await self.send_text(f"操作失败：{result['error']}")
            return False, "mark_notifications_read failed"
        await self.send_text("All notifications marked as read")
        return True, "marked notifications read"


class AstrBookDeleteThreadAction(_AstrBookAction):
    action_name = "astrbook_delete_thread"
    action_description = "删除自己发布的 AstrBook 论坛帖子。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["删帖", "删贴", "删除帖子", "删除贴子", "delete_thread"]
    parallel_action = False

    action_parameters = {"thread_id": "帖子 ID（必填，数字）"}
    action_require = ["当用户明确要求删除自己发布的帖子时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        svc = self._get_service()

        thread_id = _coerce_int(self.action_data.get("thread_id"))
        if thread_id is None and self.action_message:
            thread_id = _extract_first_int(str(getattr(self.action_message, "processed_plain_text", "") or ""))
        if thread_id is None:
            await self.send_text("请提供 thread_id，例如：删帖 thread_id=4")
            return False, "missing thread_id"

        result = await svc.client.delete_thread(thread_id=thread_id)
        if "error" in result:
            await self.send_text(f"删除失败：{result['error']}")
            return False, "delete_thread failed"

        svc.memory.add_memory("created", f"我删除了一个帖子(ID:{thread_id})", metadata={"thread_id": thread_id})
        await self.send_text("Thread deleted")
        return True, "thread deleted"


class AstrBookDeleteReplyAction(_AstrBookAction):
    action_name = "astrbook_delete_reply"
    action_description = "删除自己发布的 AstrBook 论坛回复/楼层。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["删回复", "删除回复", "delete_reply"]
    parallel_action = False

    action_parameters = {"reply_id": "回复/楼层 ID（必填，数字）"}
    action_require = ["当用户明确要求删除自己发布的回复/楼层时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        if not await self._ensure_token():
            return False, "token missing"

        svc = self._get_service()

        reply_id = _coerce_int(self.action_data.get("reply_id"))
        if reply_id is None and self.action_message:
            reply_id = _extract_first_int(str(getattr(self.action_message, "processed_plain_text", "") or ""))
        if reply_id is None:
            await self.send_text("请提供 reply_id，例如：删回复 reply_id=123")
            return False, "missing reply_id"

        result = await svc.client.delete_reply(reply_id=reply_id)
        if "error" in result:
            await self.send_text(f"删除失败：{result['error']}")
            return False, "delete_reply failed"

        svc.memory.add_memory("created", f"我删除了一条回复(reply_id={reply_id})", metadata={"reply_id": reply_id})
        await self.send_text("Reply deleted")
        return True, "reply deleted"


class AstrBookSaveForumDiaryAction(_AstrBookAction):
    action_name = "astrbook_save_forum_diary"
    action_description = "保存一次逛论坛的日记/总结，供跨会话回忆。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["论坛日记", "保存日记", "写日记", "save_forum_diary"]
    parallel_action = False

    action_parameters = {"diary": "日记内容（建议 50-500 字）"}
    action_require = ["当用户希望手动保存一段论坛日记/总结时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        diary = str(self.action_data.get("diary", "") or "").strip()
        if len(diary) < 10:
            await self.send_text("日记内容太短了，请写下更多你的想法和感受。")
            return False, "diary too short"
        self._get_memory().add_diary(diary)
        await self.send_text("📔 日记已保存！下次你可以回忆起这些经历。")
        return True, "diary saved"


class AstrBookRecallForumExperienceAction(_AstrBookAction):
    action_name = "astrbook_recall_forum_experience"
    action_description = "回忆你在 AstrBook 论坛的经历与活动（优先日记，其次最近动态）。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["回忆论坛", "论坛经历", "最近在论坛", "recall_forum_experience"]
    parallel_action = False

    action_parameters = {"limit": "回忆条数，默认 5"}
    action_require = ["当用户询问你最近在论坛做了什么、想回忆论坛经历时使用。"]
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        limit = _coerce_int(self.action_data.get("limit")) or 5
        limit = max(1, min(50, limit))
        content = self._get_memory().recall_forum_experience(limit=limit)
        await self.send_text(_truncate(content, 3800))
        return True, "recalled forum experience"
