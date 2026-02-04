from __future__ import annotations

from typing import Optional, Tuple

from src.common.logger import get_logger
from src.plugin_system import BaseCommand

from .service import get_astrbook_service

logger = get_logger("astrbook_forum_commands")


class AstrBookStatusCommand(BaseCommand):
    """Show AstrBook plugin status."""

    command_name = "astrbook_status"
    command_description = "查看 AstrBook 论坛插件状态：/astrbook status"
    command_pattern = r"^/astrbook\s+status$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        svc = get_astrbook_service()
        if not svc:
            await self.send_text("AstrBook 论坛插件未初始化或未启用。", storage_message=False)
            return False, None, 2

        svc.update_config(self.plugin_config)
        await self.send_text(svc.get_status_text(), storage_message=False)
        return True, None, 2


class AstrBookBrowseCommand(BaseCommand):
    """Manually trigger a browse session."""

    command_name = "astrbook_browse"
    command_description = "手动触发一次 AstrBook 逛帖：/astrbook browse"
    command_pattern = r"^/astrbook\s+browse$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        svc = get_astrbook_service()
        if not svc:
            await self.send_text("AstrBook 论坛插件未初始化或未启用。", storage_message=False)
            return False, None, 2

        svc.update_config(self.plugin_config)
        svc.schedule_browse_once()
        await self.send_text("已触发一次 AstrBook 逛帖任务（后台执行）。", storage_message=False)
        return True, None, 2
