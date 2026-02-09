from __future__ import annotations

from typing import Any

from src.common.logger import get_logger
from src.config.config import model_config

from .service import AstrBookService

logger = get_logger("astrbook_forum_model_slots")

FALLBACK_MODEL_SLOT = "replyer"


def _resolve_slot_name(service: AstrBookService, key: str, default_slot: str) -> str:
    configured = service.get_config_str(key, default=default_slot).strip()
    return configured or default_slot


def resolve_model_slot(
    service: AstrBookService,
    *,
    task_key: str,
    task_default_slot: str = FALLBACK_MODEL_SLOT,
) -> tuple[str, Any]:
    """Resolve model task slot from plugin config with safe fallback.

    Resolution order:
    1) llm.<task_key>
    2) llm.default_slot
    3) task_default_slot
    4) hard fallback: replyer
    """

    default_slot = _resolve_slot_name(service, "llm.default_slot", task_default_slot)
    slot_name = _resolve_slot_name(service, task_key, default_slot)

    slot_cfg = getattr(model_config.model_task_config, slot_name, None)
    if slot_cfg is not None:
        return slot_name, slot_cfg

    logger.warning(
        "[llm-slot] invalid slot for %s: %s, fallback to %s",
        task_key,
        slot_name,
        FALLBACK_MODEL_SLOT,
    )
    fallback_cfg = getattr(model_config.model_task_config, FALLBACK_MODEL_SLOT)
    return FALLBACK_MODEL_SLOT, fallback_cfg
