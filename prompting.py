from __future__ import annotations

import random
import re

from src.config.config import global_config


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

def build_forum_profile_block(profile: dict | None, *, stale_hint: str | None = None) -> str:
    """Build profile context block from `/api/auth/me` payload."""

    if not isinstance(profile, dict) or not profile:
        return ""

    username = str(profile.get("username", "") or "").strip() or "unknown"
    nickname = str(profile.get("nickname", "") or "").strip() or username
    level = profile.get("level", "unknown")
    exp = profile.get("exp", "unknown")
    persona = str(profile.get("persona", "") or "").strip() or "未设置"

    if len(persona) > 120:
        persona = persona[:117] + "..."

    stale_line = ""
    if stale_hint:
        stale_text = " ".join(str(stale_hint).split())
        if len(stale_text) > 80:
            stale_text = stale_text[:77] + "..."
        stale_line = f"- 注：个人信息接口当前不可用，以下内容可能为缓存（{stale_text}）\n"

    return (
        "\n[论坛账号画像]\n"
        f"{stale_line}"
        f"- 论坛用户名：@{username}\n"
        f"- 昵称：{nickname}\n"
        f"- 等级：Lv.{level}\n"
        f"- 经验：{exp} EXP\n"
        f"- 论坛设定人设：{persona}\n"
        "在生成发帖/回帖时，请尽量与以上论坛账号画像保持一致。\n"
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
