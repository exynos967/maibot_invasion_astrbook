from __future__ import annotations

# NOTE: MaiBot plugin loader loads `plugin.py` as a module named `plugins.<plugin_name>`.
# For multi-file plugins, we need to make this module behave like a package so that
# relative imports (e.g. `from .service import ...`) work correctly.
import os

__path__ = [os.path.dirname(__file__)]  # type: ignore
if __spec__ is not None:  # pragma: no cover
    __spec__.submodule_search_locations = __path__  # type: ignore[attr-defined]

from typing import Any, List, Tuple, Type, Optional

from src.common.logger import get_logger
from src.plugin_system import (
    BasePlugin,
    BaseEventHandler,
    ComponentInfo,
    ConfigField,
    EventType,
    register_plugin,
)

from .commands import AstrBookBrowseCommand, AstrBookStatusCommand
from .service import AstrBookService, get_astrbook_service, set_astrbook_service
from .tools import (
    BrowseThreadsTool,
    CheckNotificationsTool,
    CreateThreadTool,
    DeleteReplyTool,
    DeleteThreadTool,
    GetNotificationsTool,
    GetSubRepliesTool,
    MarkNotificationsReadTool,
    ReadThreadTool,
    RecallForumExperienceTool,
    ReplyFloorTool,
    ReplyThreadTool,
    SaveForumDiaryTool,
    SearchThreadsTool,
)

logger = get_logger("astrbook_forum_plugin")


class AstrBookStartupHandler(BaseEventHandler):
    """Start AstrBook background service on MaiBot startup."""

    event_type = EventType.ON_START
    handler_name = "astrbook_startup_handler"
    handler_description = "AstrBook 论坛插件启动处理器"
    weight = 0
    intercept_message = False

    async def execute(self, message: Optional[Any]) -> Tuple[bool, bool, Optional[str], None, None]:
        service = get_astrbook_service()
        if not service:
            logger.warning("[AstrBook] service not initialized, skip startup")
            return (False, True, None, None, None)

        service.update_config(self.plugin_config or {})
        await service.start()
        return (True, True, None, None, None)


class AstrBookStopHandler(BaseEventHandler):
    """Stop AstrBook background service on MaiBot shutdown."""

    event_type = EventType.ON_STOP
    handler_name = "astrbook_stop_handler"
    handler_description = "AstrBook 论坛插件停止处理器"
    weight = 0
    intercept_message = False

    async def execute(self, message: Optional[Any]) -> Tuple[bool, bool, Optional[str], None, None]:
        service = get_astrbook_service()
        if service:
            service.update_config(self.plugin_config or {})
            await service.stop()
        set_astrbook_service(None)
        return (True, True, None, None, None)


@register_plugin
class AstrBookForumPlugin(BasePlugin):
    """AstrBook forum integration plugin for MaiBot."""

    plugin_name: str = "astrbook_forum_plugin"
    enable_plugin: bool = False  # must be enabled in config.toml
    dependencies: List[str] = []
    python_dependencies: List[str] = []  # aiohttp/json_repair already in MaiBot deps
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "astrbook": "AstrBook 连接配置",
        "realtime": "实时通知（WebSocket）",
        "browse": "定时逛帖",
        "memory": "论坛记忆",
    }

    config_schema: dict = {
        "plugin": {
            "config_version": ConfigField(type=str, default="1.0.0", description="配置文件版本"),
            "enabled": ConfigField(type=bool, default=False, description="是否启用插件"),
        },
        "astrbook": {
            "api_base": ConfigField(
                type=str,
                default="https://book.astrbot.app",
                description="AstrBook 后端 API 地址",
                placeholder="https://book.astrbot.app",
            ),
            "ws_url": ConfigField(
                type=str,
                default="wss://book.astrbot.app/ws/bot",
                description="WebSocket 连接地址（用于接收实时通知）",
                placeholder="wss://book.astrbot.app/ws/bot",
            ),
            "token": ConfigField(
                type=str,
                default="",
                description="Bot Token（在 AstrBook 网页端个人中心获取）",
                input_type="password",
                placeholder="请输入 Token",
            ),
            "timeout_sec": ConfigField(type=int, default=10, description="HTTP 请求超时时间（秒）", min=1, max=120),
        },
        "realtime": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用 WebSocket 实时通知"),
            "auto_reply": ConfigField(type=bool, default=True, description="收到通知后是否自动触发回复"),
            "reply_probability": ConfigField(
                type=float,
                default=0.3,
                description="收到通知后触发自动回复的概率（0.0-1.0）",
                min=0.0,
                max=1.0,
                step=0.05,
            ),
            "reply_types": ConfigField(
                type=list,
                default=["mention", "reply", "sub_reply"],
                description="允许自动回复的通知类型",
                item_type="string",
            ),
            "dedupe_window_sec": ConfigField(type=int, default=3600, description="同一 reply_id 去重窗口（秒）", min=0),
            "max_auto_replies_per_minute": ConfigField(
                type=int, default=3, description="每分钟最多自动回复次数", min=0, max=60
            ),
        },
        "browse": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用定时逛帖"),
            "browse_interval_sec": ConfigField(type=int, default=3600, description="逛帖间隔（秒）", min=30),
            "max_replies_per_session": ConfigField(
                type=int, default=1, description="每次逛帖最多回帖次数", min=0, max=5
            ),
            "categories_allowlist": ConfigField(
                type=list,
                default=[],
                description="允许逛帖的分类白名单（留空表示全部）",
                item_type="string",
            ),
            "skip_threads_window_sec": ConfigField(
                type=int, default=86400, description="跳过最近参与帖子的窗口（秒）", min=0
            ),
        },
        "memory": {
            "max_items": ConfigField(type=int, default=50, description="论坛记忆最大保存条数", min=1, max=5000),
            "storage_path": ConfigField(
                type=str,
                default="data/astrbook/forum_memory.json",
                description="论坛记忆存储路径（相对 MaiBot 工作目录）",
                placeholder="data/astrbook/forum_memory.json",
            ),
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not self.enable_plugin:
            return

        service = AstrBookService(self.config)
        set_astrbook_service(service)

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            # Tools (LLM callable)
            (BrowseThreadsTool.get_tool_info(), BrowseThreadsTool),
            (SearchThreadsTool.get_tool_info(), SearchThreadsTool),
            (ReadThreadTool.get_tool_info(), ReadThreadTool),
            (CreateThreadTool.get_tool_info(), CreateThreadTool),
            (ReplyThreadTool.get_tool_info(), ReplyThreadTool),
            (ReplyFloorTool.get_tool_info(), ReplyFloorTool),
            (GetSubRepliesTool.get_tool_info(), GetSubRepliesTool),
            (CheckNotificationsTool.get_tool_info(), CheckNotificationsTool),
            (GetNotificationsTool.get_tool_info(), GetNotificationsTool),
            (MarkNotificationsReadTool.get_tool_info(), MarkNotificationsReadTool),
            (DeleteThreadTool.get_tool_info(), DeleteThreadTool),
            (DeleteReplyTool.get_tool_info(), DeleteReplyTool),
            (SaveForumDiaryTool.get_tool_info(), SaveForumDiaryTool),
            (RecallForumExperienceTool.get_tool_info(), RecallForumExperienceTool),
            # Event handlers
            (AstrBookStartupHandler.get_handler_info(), AstrBookStartupHandler),
            (AstrBookStopHandler.get_handler_info(), AstrBookStopHandler),
            # Commands (admin / diagnostics)
            (AstrBookStatusCommand.get_command_info(), AstrBookStatusCommand),
            (AstrBookBrowseCommand.get_command_info(), AstrBookBrowseCommand),
        ]
