from __future__ import annotations

import json
import random
from typing import Any

from json_repair import repair_json

from src.common.logger import get_logger
from src.config.config import model_config
from src.plugin_system.apis import llm_api

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


async def auto_reply_notification(service: AstrBookService, notification: dict[str, Any]) -> None:
    """Auto reply for a WS notification (reply/sub_reply/mention)."""

    thread_id = notification.get("thread_id")
    reply_id = notification.get("reply_id")
    thread_title = str(notification.get("thread_title", "") or "")
    from_username = str(notification.get("from_username", "unknown") or "unknown")
    msg_type = str(notification.get("type", "") or "")
    content = str(notification.get("content", "") or "")

    if not isinstance(thread_id, int):
        return

    # Fetch thread context (best-effort).
    thread_text = ""
    thread_result = await service.client.read_thread(thread_id=thread_id, page=1)
    if "text" in thread_result:
        thread_text = str(thread_result.get("text") or "")

    thread_text = _truncate(thread_text, max_chars=3500)
    notif_text = _truncate(content, max_chars=800)

    persona_block = build_forum_persona_block()
    prompt = f"""
{persona_block}

你正在 AstrBook 论坛参与讨论。

现在你收到了一条论坛通知：
- 类型: {msg_type}
- 来自: @{from_username}
- 帖子: 《{thread_title}》(ID:{thread_id})
- 内容预览: {notif_text}

下面是帖子正文与部分楼层（可能被截断）：
{thread_text}

请你判断是否需要回复，并给出你要回复的内容。

要求：
1) 只输出严格 JSON，不要输出任何多余文字。
2) JSON 格式：{{\"should_reply\": true/false, \"content\": \"...\"}}
3) content 为空字符串表示不回复。
4) 回复需有实质内容，避免纯水；语气自然、友好。
""".strip()

    temperature = service.get_config_float("realtime.reply_temperature", default=0.4, min_value=0.0, max_value=2.0)
    max_tokens = service.get_config_int("realtime.reply_max_tokens", default=8192, min_value=32, max_value=8192)

    ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
        prompt=prompt,
        model_config=model_config.model_task_config.replyer,
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
        return

    # Ensure outgoing forum text follows MaiBot persona/style.
    reply_content = await service.rewrite_outgoing_text(
        reply_content,
        purpose=f"auto_reply_{msg_type}",
        title=thread_title or None,
    )

    # Send reply.
    if isinstance(reply_id, int):
        result = await service.client.reply_floor(reply_id=reply_id, content=reply_content)
        if "error" in result:
            service.last_error = str(result.get("error"))
            service.memory.add_memory(
                "auto_reply",
                f"我尝试在帖子《{thread_title}》(ID:{thread_id}) 楼中楼回复 @{from_username} 但失败了：{result['error']}",
                metadata={"thread_id": thread_id, "reply_id": reply_id, "from_user": from_username},
            )
            return
        service.memory.add_memory(
            "replied",
            f"我在帖子《{thread_title}》(ID:{thread_id}) 的楼中楼回复了 @{from_username}: {_truncate(reply_content, 60)}",
            metadata={"thread_id": thread_id, "reply_id": reply_id, "from_user": from_username},
        )
        return

    # Fallback to replying as a new floor.
    result = await service.client.reply_thread(thread_id=thread_id, content=reply_content)
    if "error" in result:
        service.last_error = str(result.get("error"))
        service.memory.add_memory(
            "auto_reply",
            f"我尝试在帖子《{thread_title}》(ID:{thread_id}) 回复 @{from_username} 但失败了：{result['error']}",
            metadata={"thread_id": thread_id, "from_user": from_username},
        )
        return

    service.memory.add_memory(
        "replied",
        f"我在帖子《{thread_title}》(ID:{thread_id}) 回复了 @{from_username}: {_truncate(reply_content, 60)}",
        metadata={"thread_id": thread_id, "from_user": from_username},
    )


async def browse_once(service: AstrBookService) -> None:
    """One scheduled browse session: browse threads then optionally reply at most N times."""

    # Choose a category (optional allowlist).
    category = None
    allowlist = service.get_config_list_str("browse.categories_allowlist")
    if allowlist:
        category = random.choice(allowlist)

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
    prompt = f"""
{persona_block}

你正在 AstrBook 论坛闲逛，现在是一次定时逛帖任务。

下面是论坛的帖子列表（text 格式输出）：
{_truncate(browse_text, 3500)}

你最多可以在一个帖子下回复 1 次（不要发新帖）。为了避免“没看内容就回”，你需要先选择一个帖子去阅读，然后再决定是否回复。

请避免选择你最近已经参与过的帖子（避免重复），以下是你最近参与过的 thread_id 列表：
{skip_thread_ids}

请输出严格 JSON（不要输出其他内容）：\n
{{\"action\":\"none\"|\"reply_thread\",\"thread_id\": 123, \"thread_title\":\"...\", \"diary\":\"...\"}}

字段说明：
- action: none 表示只浏览不回复；reply_thread 表示你想打开并阅读某个帖子，然后再决定是否回复
- thread_id: 当 action=reply_thread 时必填
- thread_title: 可选，帖子标题（便于记录）
- diary: 逛帖日记/总结（建议填写，50-300字左右）
""".strip()

    temperature = service.get_config_float("browse.browse_temperature", default=0.6, min_value=0.0, max_value=2.0)
    max_tokens = service.get_config_int("browse.browse_max_tokens", default=8192, min_value=64, max_value=8192)

    ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
        prompt=prompt,
        model_config=model_config.model_task_config.replyer,
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

    # Read thread first, then decide whether to reply.
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

你正在 AstrBook 论坛闲逛，这是一次定时逛帖任务。

你已经打开并阅读了这个帖子：
- 帖子: 《{thread_title or '（标题未知）'}》(ID:{thread_id})

下面是帖子正文与部分楼层（text 格式输出，可能被截断）：
{thread_text}

现在请你决定是否需要回复，并给出你要发表的回复内容。

要求：
1) 只输出严格 JSON，不要输出任何多余文字。
2) JSON 格式：{{\"should_reply\": true/false, \"content\": \"...\", \"diary\": \"...\"}}
3) should_reply=false 时，content 为空字符串。
4) 回复需有实质内容，避免纯水；语气自然、友好；不要发新帖。
5) diary 为逛帖日记/总结（建议填写，50-300字左右）。
""".strip()

    ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
        prompt=reply_prompt,
        model_config=model_config.model_task_config.replyer,
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
    if not should_reply or not reply_content:
        service.memory.add_memory(
            "browsed",
            f"我逛论坛时读完帖子ID:{thread_id} 后决定不回复。",
            metadata={"thread_id": thread_id, "category": category},
        )
        return

    reply_content = await service.rewrite_outgoing_text(reply_content, purpose="browse_reply", title=thread_title or None)

    post = await service.client.reply_thread(thread_id=thread_id, content=reply_content)
    if "error" in post:
        service.last_error = str(post.get("error"))
        service.memory.add_memory(
            "browsed",
            f"我逛论坛时尝试回复帖子ID:{thread_id}但失败了：{post['error']}",
            metadata={"thread_id": thread_id, "category": category},
        )
        return

    service.memory.add_memory(
        "replied",
        f"我逛论坛时在帖子ID:{thread_id} 回复了一段内容：{_truncate(reply_content, 60)}",
        metadata={"thread_id": thread_id, "category": category},
    )
