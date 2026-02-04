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


class AstrBookPostCommand(BaseCommand):
    """Manually trigger a proactive post session."""

    command_name = "astrbook_post"
    command_description = "手动触发一次 AstrBook 主动发帖：/astrbook post"
    command_pattern = r"^/astrbook\s+post$"

    async def execute(self) -> Tuple[bool, Optional[str], int]:
        svc = get_astrbook_service()
        if not svc:
            await self.send_text("AstrBook 论坛插件未初始化或未启用。", storage_message=False)
            return False, None, 2

        svc.update_config(self.plugin_config)

        stream_id = ""
        try:
            if self.message.chat_stream and getattr(self.message.chat_stream, "stream_id", None):
                stream_id = str(self.message.chat_stream.stream_id)
        except Exception:
            stream_id = ""

        # Manual trigger uses `force=True` to bypass posting.enabled/probability gates.
        await self.send_text("AstrBook 主动发帖任务开始执行（可能需要 10-30 秒）…", storage_message=False)
        try:
            result = await svc.trigger_post_once(force=True, preferred_stream_id=stream_id or None)
        except Exception as e:
            logger.exception("[AstrBook] proactive post failed")
            await self.send_text(f"AstrBook 主动发帖执行失败：{e}", storage_message=False)
            return False, None, 2

        if result.status == "posted":
            if result.dry_run:
                await self.send_text(
                    f"主动发帖 dry_run 完成：已生成《{result.title or '（无标题）'}》分类:{result.category or 'N/A'}，但未实际发布。",
                    storage_message=False,
                )
            else:
                tid = str(result.thread_id) if result.thread_id is not None else "N/A"
                await self.send_text(
                    f"主动发帖成功：ID:{tid}《{result.title or '（无标题）'}》分类:{result.category or 'N/A'}",
                    storage_message=False,
                )
        else:
            await self.send_text(f"本次未发帖：{result.reason}", storage_message=False)
        return True, None, 2
