from __future__ import annotations

import random
import re

from src.common.logger import get_logger
from src.config.config import global_config, model_config
from src.plugin_system.apis import llm_api

logger = get_logger("maibot_invasion_astrbook.prompting")


def build_maibot_identity_prompt() -> str:
    """Build MaiBot identity/personality text similar to the main replyer."""

    bot_name = global_config.bot.nickname
    alias_names = list(getattr(global_config.bot, "alias_names", []) or [])
    bot_nickname = f",也有人叫你{','.join(alias_names)}" if alias_names else ""

    prompt_personality = str(getattr(global_config.personality, "personality", "") or "")

    # Optional random state replacement (same behavior as the replyer).
    states = list(getattr(global_config.personality, "states", []) or [])
    state_probability = float(getattr(global_config.personality, "state_probability", 0.0) or 0.0)
    if states and state_probability > 0 and random.random() < state_probability:
        try:
            prompt_personality = str(random.choice(states))
        except Exception:
            pass

    prompt_personality = f"{prompt_personality};"
    return f"你的名字是{bot_name}{bot_nickname}，你{prompt_personality}"


def choose_maibot_reply_style() -> str:
    reply_style = str(getattr(global_config.personality, "reply_style", "") or "")

    # Optional multiple styles.
    multi_styles = getattr(global_config.personality, "multiple_reply_style", None) or []
    multi_prob = float(getattr(global_config.personality, "multiple_probability", 0.0) or 0.0)
    if multi_styles and multi_prob > 0 and random.random() < multi_prob:
        try:
            reply_style = str(random.choice(list(multi_styles)))
        except Exception:
            reply_style = str(getattr(global_config.personality, "reply_style", "") or "")

    return reply_style


def build_forum_persona_block() -> str:
    identity = build_maibot_identity_prompt()
    reply_style = choose_maibot_reply_style()
    return (
        f"{identity}\n"
        f"你的说话风格/回复风格参考：{reply_style}\n"
        "请始终以以上身份与风格在论坛发言，避免出现“作为AI/作为语言模型”等免责声明。\n"
    )


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\\n?", "", stripped)
        stripped = re.sub(r"\\n?```$", "", stripped)
    return stripped.strip()


def normalize_plain_text(text: str) -> str:
    out = _strip_code_fences(text).strip()
    # Some models may wrap a single-line answer in quotes.
    if len(out) >= 2 and out[0] == out[-1] and out[0] in {'"', "'"}:
        out = out[1:-1].strip()
    return out


async def rewrite_forum_text(
    draft: str,
    purpose: str,
    *,
    title: str | None = None,
    temperature: float = 0.6,
    max_tokens: int = 8192,
    max_chars: int = 2000,
) -> str:
    """Rewrite outgoing forum text with MaiBot persona (best-effort).

    This is mainly used for tools like create_thread/reply_thread/reply_floor where tool-call
    arguments are produced by the tool-use model (which may not include persona).
    """

    draft = str(draft or "").strip()
    if not draft:
        return draft

    persona = build_forum_persona_block()
    title_block = f"- 标题: {title}\n" if title else ""
    draft = draft[:max_chars]

    prompt = f"""
{persona}

你将要在 AstrBook 论坛发布一段内容（用途：{purpose}）。
{title_block}
这是草稿（请保留事实/含义，但按你的人设与说话风格润色；不要新增与原意无关的信息；不要输出分析过程）：
{draft}

要求：
1) 只输出最终要发布的正文（纯文本），不要输出标题、不要输出 JSON、不要输出多余解释。
2) 内容自然、像正常论坛用户发言；可适度口语化。
3) 不要出现“作为AI/作为语言模型”等措辞。
""".strip()

    ok, resp, _reasoning, model_name = await llm_api.generate_with_model(
        prompt=prompt,
        model_config=model_config.model_task_config.replyer,
        request_type="astrbook.rewrite",
        temperature=max(0.0, min(2.0, float(temperature))),
        max_tokens=max(32, min(8192, int(max_tokens))),
    )
    if not ok:
        logger.warning(f"[rewrite] LLM failed: {resp}")
        return draft

    out = normalize_plain_text(resp)
    if not out:
        logger.warning(f"[rewrite] empty output from model={model_name}")
        return draft
    return out
