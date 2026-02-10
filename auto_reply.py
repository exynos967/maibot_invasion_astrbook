from __future__ import annotations

import json
import random
from typing import Any

from json_repair import repair_json

from src.common.logger import get_logger
from src.plugin_system.apis import llm_api

from .model_slots import resolve_model_slot
from .prompting import build_forum_persona_block
from .service import AstrBookService

logger = get_logger("astrbook_forum_auto")


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        fixed = repair_json(text)
        data = json.loads(fixed)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"



def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            try:
                return int(text)
            except Exception:
                return None
    return None


def _iter_thread_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "threads", "data", "results", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = value.get("items") or value.get("threads")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    return []


def _extract_thread_author_id(threads_result: dict[str, Any], thread_id: int) -> int | None:
    items = _iter_thread_items(threads_result)
    for item in items:
        current_thread_id = _safe_int(item.get("id")) or _safe_int(item.get("thread_id"))
        if current_thread_id != thread_id:
            continue

        direct_author_id = _safe_int(item.get("author_id")) or _safe_int(item.get("user_id"))
        if direct_author_id is not None:
            return direct_author_id

        author = item.get("author")
        if isinstance(author, dict):
            nested_author_id = _safe_int(author.get("id")) or _safe_int(author.get("user_id"))
            if nested_author_id is not None:
                return nested_author_id

    return None


async def _apply_autonomous_social_actions(
    service: AstrBookService,
    *,
    enabled: bool,
    scene: str,
    like_enabled: bool,
    like_target_type: str,
    like_target_id: int | None,
    follow_enabled: bool,
    follow_user_id: int | None,
    block_enabled: bool,
    block_user_id: int | None,
) -> None:
    if not enabled:
        return

    if like_enabled and like_target_type in {"thread", "reply"} and isinstance(like_target_id, int):
        like_result = await service.client.like_content(target_type=like_target_type, target_id=like_target_id)
        if "error" in like_result:
            service.memory.add_memory(
                "auto_action",
                f"{scene}时尝试点赞{like_target_type}#{like_target_id}失败：{like_result['error']}",
                metadata={"target_type": like_target_type, "target_id": like_target_id, "scene": scene},
            )
        else:
            liked = bool(like_result.get("liked", False))
            like_count = like_result.get("like_count")
            like_count_text = str(like_count) if isinstance(like_count, int) else "未知"
            if liked:
                service.memory.add_memory(
                    "auto_action",
                    f"{scene}时已自主点赞{like_target_type}#{like_target_id}（当前点赞数：{like_count_text}）。",
                    metadata={"target_type": like_target_type, "target_id": like_target_id, "scene": scene},
                )
            else:
                service.memory.add_memory(
                    "auto_action",
                    f"{scene}时检测到{like_target_type}#{like_target_id}此前已点赞（当前点赞数：{like_count_text}）。",
                    metadata={"target_type": like_target_type, "target_id": like_target_id, "scene": scene},
                )

    if follow_enabled and isinstance(follow_user_id, int):
        if not (service.bot_user_id and follow_user_id == service.bot_user_id):
            follow_result = await service.client.toggle_follow(user_id=follow_user_id, action="follow")
            if "error" in follow_result:
                error_text = str(follow_result.get("error") or "")
                error_lower = error_text.lower()
                already_followed = (
                    ("already" in error_lower and "follow" in error_lower)
                    or "已关注" in error_text
                    or "重复关注" in error_text
                )
                if already_followed:
                    service.memory.add_memory(
                        "auto_action",
                        f"{scene}时检测到 user_id={follow_user_id} 已在关注列表中。",
                        metadata={"followed_user_id": follow_user_id, "scene": scene},
                    )
                else:
                    service.memory.add_memory(
                        "auto_action",
                        f"{scene}时尝试关注 user_id={follow_user_id} 失败：{error_text or 'unknown error'}",
                        metadata={"followed_user_id": follow_user_id, "scene": scene},
                    )
            else:
                msg = str(follow_result.get("message", "") or "").strip()
                suffix = f"（{msg}）" if msg else ""
                service.memory.add_memory(
                    "auto_action",
                    f"{scene}时已自主关注 user_id={follow_user_id}{suffix}",
                    metadata={"followed_user_id": follow_user_id, "scene": scene},
                )

    if not block_enabled:
        return
    if not isinstance(block_user_id, int):
        return
    if service.bot_user_id and block_user_id == service.bot_user_id:
        return

    block_result = await service.client.block_user(user_id=block_user_id)
    if "error" in block_result:
        error_text = str(block_result.get("error") or "")
        if "already" in error_text.lower() and "block" in error_text.lower():
            service.memory.add_memory(
                "auto_action",
                f"{scene}时检测到 user_id={block_user_id} 已在黑名单中。",
                metadata={"blocked_user_id": block_user_id, "scene": scene},
            )
            return

        service.memory.add_memory(
            "auto_action",
            f"{scene}时尝试拉黑 user_id={block_user_id} 失败：{error_text or 'unknown error'}",
            metadata={"blocked_user_id": block_user_id, "scene": scene},
        )
        return

    service.memory.add_memory(
        "auto_action",
        f"{scene}时已自主拉黑 user_id={block_user_id}。",
        metadata={"blocked_user_id": block_user_id, "scene": scene},
    )


async def auto_reply_notification(service: AstrBookService, notification: dict[str, Any]) -> None:
    """Auto reply for an SSE notification (reply/sub_reply/mention/new_post)."""

    thread_id = notification.get("thread_id")
    reply_id = notification.get("reply_id")
    thread_title = str(notification.get("thread_title", "") or "")
    from_user_id = notification.get("from_user_id")
    from_username = str(notification.get("from_username") or notification.get("author") or "unknown")
    msg_type = str(notification.get("type", "") or "")
    content = str(notification.get("content", "") or "")

    if not isinstance(thread_id, int):
        return

    social_actions_enabled = service.get_config_bool("realtime.autonomous_social_actions", default=True)
    follow_actions_enabled = social_actions_enabled and service.get_config_bool("realtime.autonomous_follow", default=False)
    block_actions_enabled = social_actions_enabled and service.get_config_bool("realtime.autonomous_block", default=False)
    like_target_type = "reply" if isinstance(reply_id, int) else "thread"
    like_target_id = reply_id if isinstance(reply_id, int) else thread_id

    thread_text = ""
    thread_result = await service.client.read_thread(thread_id=thread_id, page=1)
    if "text" in thread_result:
        thread_text = str(thread_result.get("text") or "")

    thread_text = _truncate(thread_text, max_chars=3500)
    notif_text = _truncate(content, max_chars=800)

    persona_block = build_forum_persona_block()
    profile_block = await service.get_profile_context_block()
    prompt = f"""
{persona_block}
{profile_block}

你正在 AstrBook 论坛参与讨论。

现在你收到了一条论坛通知：
- 类型: {msg_type}
- 来自: @{from_username}
- 帖子: 《{thread_title}》(ID:{thread_id})
- 内容预览: {notif_text}

下面是帖子正文与部分楼层（可能被截断）：
{thread_text}

请你判断是否需要回复，并决定是否要执行额外动作（点赞/关注/拉黑）。

要求：
1) 只输出严格 JSON，不要输出任何多余文字。
2) JSON 格式：{{"should_reply": true/false, "content": "...", "should_like": true/false, "should_follow": true/false, "block_user": true/false}}
3) content 为空字符串表示不回复。
4) 回复需有实质内容，避免纯水；语气自然、友好。
5) should_like=true 表示对当前通知相关目标点个赞（优先点赞被回复的楼层，其次帖子）。
6) should_follow=true 表示关注通知发起者（仅在对方长期输出高质量内容时使用，默认 false）。
7) block_user=true 仅在对方明显恶意骚扰/辱骂/广告刷屏时才使用，正常讨论必须为 false。
""".strip()

    temperature = service.get_config_float("realtime.reply_temperature", default=0.4, min_value=0.0, max_value=2.0)
    max_tokens = service.get_config_int("realtime.reply_max_tokens", default=8192, min_value=32, max_value=8192)

    _, model_slot_config = resolve_model_slot(service, task_key="llm.realtime_auto_reply_slot")
    ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
        prompt=prompt,
        model_config=model_slot_config,
        request_type="astrbook.auto_reply",
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if not ok:
        logger.warning(f"[auto_reply] LLM failed: {resp}")
        return

    data = _parse_json_object(resp)
    if not data:
        logger.warning(f"[auto_reply] invalid json from model={model_name}: {resp[:200]}")
        return

    should_reply = bool(data.get("should_reply", False))
    reply_content = str(data.get("content", "") or "").strip()
    should_like = bool(data.get("should_like", False))
    should_follow = bool(data.get("should_follow", False))
    should_block = bool(data.get("block_user", False))

    if not should_reply or not reply_content:
        service.memory.add_memory(
            "auto_reply",
            f"收到 @{from_username} 的通知但我选择不自动回复（{msg_type}，帖子ID:{thread_id}）。",
            metadata={
                "thread_id": thread_id,
                "reply_id": reply_id,
                "from_user": from_username,
                "notification_type": msg_type,
            },
        )
        await _apply_autonomous_social_actions(
            service,
            enabled=social_actions_enabled,
            scene="通知自动处理",
            like_enabled=should_like,
            like_target_type=like_target_type,
            like_target_id=like_target_id if isinstance(like_target_id, int) else None,
            follow_enabled=should_follow and follow_actions_enabled,
            follow_user_id=from_user_id if isinstance(from_user_id, int) else None,
            block_enabled=should_block and block_actions_enabled,
            block_user_id=from_user_id if isinstance(from_user_id, int) else None,
        )
        return

    if isinstance(reply_id, int):
        result = await service.client.reply_floor(reply_id=reply_id, content=reply_content)
        if "error" in result:
            service.last_error = str(result.get("error"))
            service.memory.add_memory(
                "auto_reply",
                f"我尝试在帖子《{thread_title}》(ID:{thread_id}) 楼中楼回复 @{from_username} 但失败了：{result['error']}",
                metadata={"thread_id": thread_id, "reply_id": reply_id, "from_user": from_username},
            )
            await _apply_autonomous_social_actions(
                service,
                enabled=social_actions_enabled,
                scene="通知自动处理",
                like_enabled=should_like,
                like_target_type=like_target_type,
                like_target_id=like_target_id if isinstance(like_target_id, int) else None,
                follow_enabled=should_follow and follow_actions_enabled,
                follow_user_id=from_user_id if isinstance(from_user_id, int) else None,
                block_enabled=should_block and block_actions_enabled,
                block_user_id=from_user_id if isinstance(from_user_id, int) else None,
            )
            return

        service.memory.add_memory(
            "replied",
            f"我在帖子《{thread_title}》(ID:{thread_id}) 的楼中楼回复了 @{from_username}: {_truncate(reply_content, 60)}",
            metadata={"thread_id": thread_id, "reply_id": reply_id, "from_user": from_username},
        )
        await _apply_autonomous_social_actions(
            service,
            enabled=social_actions_enabled,
            scene="通知自动处理",
            like_enabled=should_like,
            like_target_type=like_target_type,
            like_target_id=like_target_id if isinstance(like_target_id, int) else None,
            follow_enabled=should_follow and follow_actions_enabled,
            follow_user_id=from_user_id if isinstance(from_user_id, int) else None,
            block_enabled=should_block and block_actions_enabled,
            block_user_id=from_user_id if isinstance(from_user_id, int) else None,
        )
        return

    result = await service.client.reply_thread(thread_id=thread_id, content=reply_content)
    if "error" in result:
        service.last_error = str(result.get("error"))
        service.memory.add_memory(
            "auto_reply",
            f"我尝试在帖子《{thread_title}》(ID:{thread_id}) 回复 @{from_username} 但失败了：{result['error']}",
            metadata={"thread_id": thread_id, "from_user": from_username},
        )
        await _apply_autonomous_social_actions(
            service,
            enabled=social_actions_enabled,
            scene="通知自动处理",
            like_enabled=should_like,
            like_target_type=like_target_type,
            like_target_id=like_target_id if isinstance(like_target_id, int) else None,
            follow_enabled=should_follow and follow_actions_enabled,
            follow_user_id=from_user_id if isinstance(from_user_id, int) else None,
            block_enabled=should_block and block_actions_enabled,
            block_user_id=from_user_id if isinstance(from_user_id, int) else None,
        )
        return

    service.memory.add_memory(
        "replied",
        f"我在帖子《{thread_title}》(ID:{thread_id}) 回复了 @{from_username}: {_truncate(reply_content, 60)}",
        metadata={"thread_id": thread_id, "from_user": from_username},
    )
    await _apply_autonomous_social_actions(
        service,
        enabled=social_actions_enabled,
        scene="通知自动处理",
        like_enabled=should_like,
        like_target_type=like_target_type,
        like_target_id=like_target_id if isinstance(like_target_id, int) else None,
        follow_enabled=should_follow and follow_actions_enabled,
        follow_user_id=from_user_id if isinstance(from_user_id, int) else None,
        block_enabled=should_block and block_actions_enabled,
        block_user_id=from_user_id if isinstance(from_user_id, int) else None,
    )


async def browse_once(service: AstrBookService) -> None:
    """One scheduled browse session: browse threads then optionally reply at most N times."""

    category = None
    allowlist = service.get_config_list_str("browse.categories_allowlist")
    if allowlist:
        category = random.choice(allowlist)

    social_actions_enabled = service.get_config_bool("browse.autonomous_social_actions", default=True)
    follow_actions_enabled = social_actions_enabled and service.get_config_bool("browse.autonomous_follow", default=False)
    block_actions_enabled = social_actions_enabled and service.get_config_bool("browse.autonomous_block", default=False)

    result = await service.client.browse_threads(page=1, page_size=10, category=category)
    if "error" in result:
        service.last_error = str(result.get("error"))
        return
    browse_text = str(result.get("text") or "")
    if not browse_text.strip():
        return

    skip_window = service.get_config_int(
        "browse.skip_threads_window_sec", default=86400, min_value=0, max_value=86400 * 30
    )
    skip_thread_ids = sorted(service.memory.get_recent_thread_ids(window_sec=skip_window))

    persona_block = build_forum_persona_block()
    profile_block = await service.get_profile_context_block()
    prompt = f"""
{persona_block}
{profile_block}

你正在 AstrBook 论坛闲逛，现在是一次定时逛帖任务。

下面是论坛的帖子列表（text 格式输出）：
{_truncate(browse_text, 3500)}

你最多可以在一个帖子下回复 1 次（不要发新帖）。为了避免“没看内容就回”，你需要先选择一个帖子去阅读，然后再决定是否回复。

请避免选择你最近已经参与过的帖子（避免重复），以下是你最近参与过的 thread_id 列表：
{skip_thread_ids}

请输出严格 JSON（不要输出其他内容）：

{{"action":"none"|"reply_thread","thread_id": 123, "thread_title":"...", "diary":"..."}}

字段说明：
- action: none 表示只浏览不回复；reply_thread 表示你想打开并阅读某个帖子，然后再决定是否回复
- thread_id: 当 action=reply_thread 时必填
- thread_title: 可选，帖子标题（便于记录）
- diary: 逛帖日记/总结（建议填写，50-300字左右）
""".strip()

    temperature = service.get_config_float("browse.browse_temperature", default=0.6, min_value=0.0, max_value=2.0)
    max_tokens = service.get_config_int("browse.browse_max_tokens", default=8192, min_value=64, max_value=8192)

    _, browse_decision_model = resolve_model_slot(service, task_key="llm.browse_decision_slot")
    ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
        prompt=prompt,
        model_config=browse_decision_model,
        request_type="astrbook.browse",
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if not ok:
        logger.warning(f"[browse] LLM failed: {resp}")
        return

    data = _parse_json_object(resp)
    if not data:
        logger.warning(f"[browse] invalid json from model={model_name}: {resp[:200]}")
        return

    diary = str(data.get("diary", "") or "").strip()
    action = str(data.get("action", "none") or "none").strip()
    if action != "reply_thread":
        if diary:
            service.memory.add_diary(diary)
        service.memory.add_memory("browsed", "我逛了逛 AstrBook 论坛，没有发表回复。", metadata={"category": category})
        return

    thread_id = data.get("thread_id")
    thread_title = str(data.get("thread_title", "") or "").strip()
    if not isinstance(thread_id, int):
        return

    if thread_id in set(skip_thread_ids):
        if diary:
            service.memory.add_diary(diary)
        service.memory.add_memory(
            "browsed",
            f"我原本想回复帖子ID:{thread_id}，但发现最近参与过，为了避免重复我选择跳过。",
            metadata={"thread_id": thread_id},
        )
        return

    max_replies = service.get_config_int("browse.max_replies_per_session", default=1, min_value=0, max_value=5)
    if max_replies <= 0:
        return

    thread_author_id: int | None = None
    if social_actions_enabled:
        listing_result = await service.client.list_threads(page=1, page_size=20, category=category)
        if isinstance(listing_result, dict) and "error" not in listing_result:
            thread_author_id = _extract_thread_author_id(listing_result, thread_id)

    thread_text = ""
    thread_result = await service.client.read_thread(thread_id=thread_id, page=1)
    if "error" in thread_result:
        service.last_error = str(thread_result.get("error"))
        if diary:
            service.memory.add_diary(diary)
        service.memory.add_memory(
            "browsed",
            f"我逛论坛时打开帖子ID:{thread_id} 但读取失败：{service.last_error}",
            metadata={"thread_id": thread_id, "category": category},
        )
        return
    if "text" in thread_result:
        thread_text = str(thread_result.get("text") or "")

    thread_text = _truncate(thread_text, max_chars=3500)

    reply_prompt = f"""
{persona_block}
{profile_block}

你正在 AstrBook 论坛闲逛，这是一次定时逛帖任务。

你已经打开并阅读了这个帖子：
- 帖子: 《{thread_title or '（标题未知）'}》(ID:{thread_id})
- 帖子作者 user_id: {thread_author_id if isinstance(thread_author_id, int) else 'unknown'}

下面是帖子正文与部分楼层（text 格式输出，可能被截断）：
{thread_text}

现在请你决定是否需要回复，并决定是否执行额外动作（点赞/关注/拉黑）。

要求：
1) 只输出严格 JSON，不要输出任何多余文字。
2) JSON 格式：{{"should_reply": true/false, "content": "...", "diary": "...", "should_like": true/false, "follow_thread_author": true/false, "block_thread_author": true/false}}
3) should_reply=false 时，content 为空字符串。
4) 回复需有实质内容，避免纯水；语气自然、友好；不要发新帖。
5) should_like=true 表示给该帖子点个赞。
6) follow_thread_author=true 表示关注该帖作者（仅在其内容持续高质量时使用，默认 false）。
7) block_thread_author=true 仅在作者明显恶意骚扰/辱骂/广告刷屏时才使用，正常讨论必须为 false。
8) diary 为逛帖日记/总结（建议填写，50-300字左右）。
""".strip()

    _, browse_reply_model = resolve_model_slot(service, task_key="llm.browse_reply_slot")
    ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
        prompt=reply_prompt,
        model_config=browse_reply_model,
        request_type="astrbook.browse.reply",
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if not ok:
        logger.warning(f"[browse.reply] LLM failed: {resp}")
        return

    reply_data = _parse_json_object(resp)
    if not reply_data:
        logger.warning(f"[browse.reply] invalid json from model={model_name}: {resp[:200]}")
        return

    diary2 = str(reply_data.get("diary", "") or "").strip()
    final_diary = diary2 or diary
    if final_diary:
        service.memory.add_diary(final_diary)

    should_reply = bool(reply_data.get("should_reply", False))
    reply_content = str(reply_data.get("content", "") or "").strip()
    should_like = bool(reply_data.get("should_like", False))
    follow_thread_author = bool(reply_data.get("follow_thread_author", False))
    block_thread_author = bool(reply_data.get("block_thread_author", False))

    if not should_reply or not reply_content:
        service.memory.add_memory(
            "browsed",
            f"我逛论坛时读完帖子ID:{thread_id} 后决定不回复。",
            metadata={"thread_id": thread_id, "category": category},
        )
        await _apply_autonomous_social_actions(
            service,
            enabled=social_actions_enabled,
            scene="定时逛帖",
            like_enabled=should_like,
            like_target_type="thread",
            like_target_id=thread_id,
            follow_enabled=follow_thread_author and follow_actions_enabled,
            follow_user_id=thread_author_id,
            block_enabled=block_thread_author and block_actions_enabled,
            block_user_id=thread_author_id,
        )
        return

    post = await service.client.reply_thread(thread_id=thread_id, content=reply_content)
    if "error" in post:
        service.last_error = str(post.get("error"))
        service.memory.add_memory(
            "browsed",
            f"我逛论坛时尝试回复帖子ID:{thread_id}但失败了：{post['error']}",
            metadata={"thread_id": thread_id, "category": category},
        )
        await _apply_autonomous_social_actions(
            service,
            enabled=social_actions_enabled,
            scene="定时逛帖",
            like_enabled=should_like,
            like_target_type="thread",
            like_target_id=thread_id,
            follow_enabled=follow_thread_author and follow_actions_enabled,
            follow_user_id=thread_author_id,
            block_enabled=block_thread_author and block_actions_enabled,
            block_user_id=thread_author_id,
        )
        return

    service.memory.add_memory(
        "replied",
        f"我逛论坛时在帖子ID:{thread_id} 回复了一段内容：{_truncate(reply_content, 60)}",
        metadata={"thread_id": thread_id, "category": category},
    )
    await _apply_autonomous_social_actions(
        service,
        enabled=social_actions_enabled,
        scene="定时逛帖",
        like_enabled=should_like,
        like_target_type="thread",
        like_target_id=thread_id,
        follow_enabled=follow_thread_author and follow_actions_enabled,
        follow_user_id=thread_author_id,
        block_enabled=block_thread_author and block_actions_enabled,
        block_user_id=thread_author_id,
    )
