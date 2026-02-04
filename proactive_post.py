from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from json_repair import repair_json

from src.chat.message_receive.chat_stream import ChatStream, get_chat_manager
from src.chat.utils.chat_message_builder import build_readable_messages, get_raw_msg_before_timestamp_with_chat
from src.common.database.database_model import ChatStreams
from src.common.logger import get_logger
from src.config.config import model_config
from src.memory_system.memory_retrieval import (
    build_memory_retrieval_prompt,
    init_memory_retrieval_prompt,
)
from src.plugin_system.apis import llm_api

from .posting_policy import sanitize_forum_text
from .prompting import build_forum_persona_block
from .service import AstrBookService
from .tools import VALID_CATEGORIES

logger = get_logger("astrbook_forum_proactive_post")

_memory_prompt_inited = False


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        fixed = repair_json(text)
        data = json.loads(fixed)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProactivePostCandidate:
    stream: ChatStream
    chat_history: str
    memory_hint: str


@dataclass(frozen=True, slots=True)
class ProactivePostResult:
    status: Literal["posted", "skipped", "error"]
    reason: str
    thread_id: int | None = None
    title: str | None = None
    category: str | None = None
    dry_run: bool = False


def _iter_candidate_streams_from_runtime(now: float) -> Iterable[ChatStream]:
    mgr = get_chat_manager()
    for stream in list(getattr(mgr, "streams", {}).values()):
        try:
            if stream.last_active_time and now - float(stream.last_active_time) < 86400 * 365:
                yield stream
        except Exception:
            continue


def _iter_candidate_streams_from_db(now: float, window_sec: int) -> Iterable[ChatStream]:
    threshold = now - max(0, int(window_sec))
    query = (
        ChatStreams.select()
        .where(ChatStreams.last_active_time >= threshold)
        .order_by(ChatStreams.last_active_time.desc())
        .limit(50)
    )

    for row in query:
        try:
            data = {
                "stream_id": row.stream_id,
                "platform": row.platform,
                "create_time": row.create_time,
                "last_active_time": row.last_active_time,
                "user_info": {
                    "platform": row.user_platform,
                    "user_id": row.user_id,
                    "user_nickname": row.user_nickname,
                    "user_cardname": row.user_cardname,
                },
                "group_info": (
                    {
                        "platform": row.group_platform,
                        "group_id": row.group_id,
                        "group_name": row.group_name,
                    }
                    if row.group_id
                    else None
                ),
            }
            yield ChatStream.from_dict(data)
        except Exception:
            continue


def _filter_stream(
    stream: ChatStream,
    *,
    include_private: bool,
    allowed_group_ids: set[str],
) -> bool:
    if stream.group_info is None:
        return include_private

    if allowed_group_ids:
        gid = str(getattr(stream.group_info, "group_id", "") or "")
        return bool(gid and gid in allowed_group_ids)
    return True


async def build_proactive_post_candidate(
    service: AstrBookService, *, preferred_stream_id: str | None = None
) -> ProactivePostCandidate | None:
    now = time.time()

    include_private = service.get_config_bool("posting.include_private_chats", default=False)
    allowed_group_ids = set(service.get_config_list_str("posting.source_group_ids"))

    window_sec = service.get_config_int("posting.source_window_sec", default=7200, min_value=60, max_value=86400 * 30)
    context_messages = service.get_config_int("posting.context_messages", default=30, min_value=5, max_value=200)

    stream: ChatStream | None = None
    if preferred_stream_id:
        preferred_stream_id = str(preferred_stream_id)
        try:
            stream = get_chat_manager().get_stream(preferred_stream_id)
        except Exception:
            stream = None
        if not stream:
            try:
                row = ChatStreams.get_or_none(ChatStreams.stream_id == preferred_stream_id)
            except Exception:
                row = None
            if row:
                try:
                    data = {
                        "stream_id": row.stream_id,
                        "platform": row.platform,
                        "create_time": row.create_time,
                        "last_active_time": row.last_active_time,
                        "user_info": {
                            "platform": row.user_platform,
                            "user_id": row.user_id,
                            "user_nickname": row.user_nickname,
                            "user_cardname": row.user_cardname,
                        },
                        "group_info": (
                            {
                                "platform": row.group_platform,
                                "group_id": row.group_id,
                                "group_name": row.group_name,
                            }
                            if row.group_id
                            else None
                        ),
                    }
                    stream = ChatStream.from_dict(data)
                except Exception:
                    stream = None
        if stream and not _filter_stream(stream, include_private=include_private, allowed_group_ids=allowed_group_ids):
            stream = None

    # Prefer runtime streams (more fresh), fallback to DB.
    if not stream:
        candidates = [
            s
            for s in _iter_candidate_streams_from_runtime(now)
            if (now - float(getattr(s, "last_active_time", 0.0) or 0.0) <= window_sec)
            and _filter_stream(s, include_private=include_private, allowed_group_ids=allowed_group_ids)
        ]

        if not candidates:
            candidates = [
                s
                for s in _iter_candidate_streams_from_db(now, window_sec=window_sec)
                if _filter_stream(s, include_private=include_private, allowed_group_ids=allowed_group_ids)
            ]

        if not candidates:
            return None

        # Use the most active ones first, but keep randomness to avoid repetitive topics.
        candidates = sorted(candidates, key=lambda s: float(getattr(s, "last_active_time", 0.0) or 0.0), reverse=True)
        pick_pool = candidates[: min(10, len(candidates))]
        stream = random.choice(pick_pool)

    messages = get_raw_msg_before_timestamp_with_chat(
        chat_id=stream.stream_id,
        timestamp=now,
        limit=context_messages,
        filter_intercept_message_level=1,
    )
    chat_history = build_readable_messages(
        messages,
        replace_bot_name=True,
        timestamp_mode="relative",
        truncate=True,
        show_actions=False,
        long_time_notice=True,
        remove_emoji_stickers=True,
    )
    chat_history = chat_history.strip()
    if not chat_history:
        return None

    enable_memory = service.get_config_bool("posting.enable_memory_retrieval", default=True)
    think_level = service.get_config_int("posting.memory_think_level", default=0, min_value=0, max_value=1)
    memory_hint = ""
    if enable_memory:
        global _memory_prompt_inited
        if not _memory_prompt_inited:
            try:
                init_memory_retrieval_prompt()
            except Exception:
                # Avoid breaking proactive posting due to init issues.
                pass
            _memory_prompt_inited = True

        try:
            memory_hint = await build_memory_retrieval_prompt(
                message=_truncate(chat_history, 1200),
                sender="论坛",
                target="主动发帖",
                chat_stream=stream,
                think_level=think_level,
            )
        except Exception as e:
            logger.warning(f"[proactive_post] memory retrieval failed: {e}")
            memory_hint = ""

    return ProactivePostCandidate(stream=stream, chat_history=chat_history, memory_hint=memory_hint)


async def proactive_post_once(
    service: AstrBookService, *, force: bool = False, preferred_stream_id: str | None = None
) -> ProactivePostResult:
    """Generate and create a new forum thread proactively (full auto).

    Risk controls are enforced via posting.* config.
    """

    if not service.client.token_configured:
        msg = "Token not configured, proactive posting disabled"
        service.last_error = msg
        return ProactivePostResult(status="error", reason=msg)

    if not force and not service.get_config_bool("posting.enabled", default=False):
        return ProactivePostResult(status="skipped", reason="posting.enabled=false")

    probability = service.get_config_float("posting.post_probability", default=0.2, min_value=0.0, max_value=1.0)
    if not force and random.random() > probability:
        return ProactivePostResult(status="skipped", reason=f"probability not hit (post_probability={probability:.2f})")

    now = time.time()
    if not service.post_rate_limiter.allow(now=now):
        return ProactivePostResult(status="skipped", reason="rate limited by posting policy")

    candidate = await build_proactive_post_candidate(service, preferred_stream_id=preferred_stream_id)
    if not candidate:
        return ProactivePostResult(
            status="skipped",
            reason="no suitable chat context (inactive / filtered / not in allowlist / only command messages)",
        )

    allowed_categories = service.get_config_list_str("posting.categories_allowlist")
    allowed_categories = [c for c in allowed_categories if c in VALID_CATEGORIES]
    if not allowed_categories:
        allowed_categories = list(VALID_CATEGORIES)

    persona_block = build_forum_persona_block()

    max_context_chars = service.get_config_int(
        "posting.max_context_chars", default=3500, min_value=500, max_value=20000
    )
    chat_history = _truncate(candidate.chat_history, max_context_chars)
    memory_hint = _truncate(candidate.memory_hint, 1500)

    # Pre-sanitize context to reduce accidental leakage in model outputs.
    chat_history = sanitize_forum_text(chat_history, allow_urls=False, allow_mentions=False)
    memory_hint = sanitize_forum_text(memory_hint, allow_urls=False, allow_mentions=False)

    prompt = f"""
{persona_block}

你将代表 MaiBot 在 AstrBook 论坛发布一个新的主题帖子。

你需要参考下面的“聊天记录摘要”和“回忆信息”，提出一个适合公开讨论的话题，并写出一篇论坛贴（不要写成聊天回复）。

【安全与风控要求（必须遵守）】
1) 不要泄露任何隐私/敏感信息：QQ号、群号、手机号、邮箱、真实姓名、具体群名、具体聊天原句逐字引用、内部链接/邀请链接/Token。
2) 不要提及“某某群”“某某私聊”之类的具体来源，只能用泛化描述（例如“我最近和朋友聊天时…”）。
3) 不要编造事实；只基于提供材料做总结/观点/提问。
4) 内容要有价值：要么分享观点/经验，要么提出清晰问题。

允许的分类：{allowed_categories}

【聊天记录摘要（可能截断）】
{chat_history}

【回忆信息（可能为空）】
{memory_hint}

请输出严格 JSON（不要输出其他内容）：\n
{{\"should_post\": true/false, \"category\": \"chat\", \"title\": \"...\", \"content\": \"...\", \"reason\": \"...\"}}

字段要求：
- should_post=false 时，其它字段可以为空字符串
- title 需要 2-100 字符
- content 至少 50 字符，最多 1200 字符左右
""".strip()

    temperature = service.get_config_float("posting.temperature", default=0.7, min_value=0.0, max_value=2.0)
    max_tokens = service.get_config_int("posting.max_tokens", default=800, min_value=64, max_value=2048)

    ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
        prompt=prompt,
        model_config=model_config.model_task_config.replyer,
        request_type="astrbook.proactive_post",
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if not ok:
        logger.warning(f"[proactive_post] LLM failed: {resp}")
        return ProactivePostResult(status="error", reason=f"LLM failed: {resp}")

    data = _parse_json_object(resp)
    if not data:
        logger.warning(f"[proactive_post] invalid json from model={model_name}: {resp[:200]}")
        return ProactivePostResult(status="error", reason="invalid json from model")

    if not bool(data.get("should_post", False)):
        reason = str(data.get("reason", "") or "").strip() or "model decided not to post"
        return ProactivePostResult(status="skipped", reason=reason)

    category = str(data.get("category", "chat") or "chat").strip()
    if category not in allowed_categories:
        category = allowed_categories[0] if allowed_categories else "chat"

    title = str(data.get("title", "") or "").strip()
    content = str(data.get("content", "") or "").strip()

    # Basic validation.
    if len(title) < 2:
        return ProactivePostResult(status="skipped", reason="title too short", title=title, category=category)
    if len(title) > 100:
        title = title[:100]
    if len(content) < 20:
        return ProactivePostResult(status="skipped", reason="content too short", title=title, category=category)

    allow_urls = service.get_config_bool("posting.allow_urls", default=False)
    allow_mentions = service.get_config_bool("posting.allow_mentions", default=False)

    title = sanitize_forum_text(title, allow_urls=allow_urls, allow_mentions=allow_mentions)
    content = sanitize_forum_text(content, allow_urls=allow_urls, allow_mentions=allow_mentions)

    max_content_chars = service.get_config_int(
        "posting.max_content_chars", default=1200, min_value=200, max_value=20000
    )
    content = content[:max_content_chars].strip()

    # Dedupe by hash in a rolling window.
    dedupe_window_sec = service.get_config_int(
        "posting.dedupe_window_sec", default=86400, min_value=0, max_value=86400 * 30
    )
    if dedupe_window_sec > 0:
        service.cleanup_recent_post_hashes(now=now, window_sec=dedupe_window_sec)
        post_hash = _stable_hash(f"{title}\n{content}")
        if post_hash in service.recent_post_hashes:
            return ProactivePostResult(status="skipped", reason="duplicate content in dedupe window", title=title)
    else:
        post_hash = _stable_hash(f"{title}\n{content}")

    dry_run = service.get_config_bool("posting.dry_run", default=False)
    if dry_run:
        service.recent_post_hashes[post_hash] = now
        service.post_rate_limiter.record(now=now)
        service.memory.add_memory(
            "created",
            f"（dry_run）我计划主动发帖《{title}》分类:{category}，但未实际发布。",
            metadata={"category": category, "title": title, "dry_run": True},
        )
        return ProactivePostResult(
            status="posted",
            reason="dry_run: generated but not published",
            title=title,
            category=category,
            dry_run=True,
        )

    result = await service.client.create_thread(title=title, content=content, category=category)
    if "error" in result:
        service.last_error = str(result.get("error"))
        service.memory.add_memory(
            "created",
            f"我尝试主动发帖《{title}》但失败了：{service.last_error}",
            metadata={"category": category, "title": title},
        )
        return ProactivePostResult(
            status="error",
            reason=f"create_thread failed: {service.last_error}",
            title=title,
            category=category,
        )

    thread_id = result.get("id")
    service.recent_post_hashes[post_hash] = now
    service.post_rate_limiter.record(now=now)

    service.memory.add_memory(
        "created",
        f"我主动发了一个新帖《{title}》(ID:{thread_id})",
        metadata={
            "thread_id": thread_id,
            "category": category,
            "title": title,
            "source_stream": candidate.stream.stream_id,
        },
    )

    return ProactivePostResult(
        status="posted",
        reason="published",
        thread_id=thread_id if isinstance(thread_id, int) else None,
        title=title,
        category=category,
    )
