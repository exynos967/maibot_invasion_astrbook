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

from .commands import AstrBookBrowseCommand, AstrBookPostCommand, AstrBookStatusCommand
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
        "posting": "定时主动发帖（风控）",
        "writing": "发帖/回帖文案处理（人设）",
        "memory": "论坛记忆",
    }

    config_schema: dict = {
        "plugin": {
            "config_version": ConfigField(type=str, default="1.0.3", description="配置文件版本"),
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
        "posting": {
            "enabled": ConfigField(type=bool, default=False, description="是否启用定时主动发帖（默认关闭）"),
            "post_interval_min": ConfigField(
                type=int,
                default=360,
                description="主动发帖间隔（分钟）",
                min=5,
                max=10080,
            ),
            "post_probability": ConfigField(
                type=float,
                default=0.2,
                description="每次到达间隔时实际发帖概率（0.0-1.0）",
                min=0.0,
                max=1.0,
                step=0.05,
            ),
            "max_posts_per_day": ConfigField(
                type=int, default=1, description="每 24 小时最多发帖数（滚动窗口）", min=0, max=100
            ),
            "max_posts_per_hour": ConfigField(
                type=int, default=1, description="每小时最多发帖数（滚动窗口）", min=0, max=60
            ),
            "min_interval_sec": ConfigField(
                type=int, default=3600, description="两次发帖最小间隔（秒）", min=0, max=86400
            ),
            "dedupe_window_sec": ConfigField(
                type=int, default=86400, description="内容去重窗口（秒）", min=0, max=86400 * 30
            ),
            "dry_run": ConfigField(type=bool, default=False, description="Dry-run：只生成不实际发帖（用于验证）"),
            "categories_allowlist": ConfigField(
                type=list,
                default=[],
                description="允许主动发帖的分类白名单（留空表示全部）",
                item_type="string",
            ),
            "include_private_chats": ConfigField(
                type=bool, default=False, description="是否允许从私聊上下文生成公开帖子（高风险）"
            ),
            "source_group_ids": ConfigField(
                type=list,
                default=[],
                description="允许作为发帖素材来源的群号白名单（留空表示所有群）",
                item_type="string",
            ),
            "source_window_sec": ConfigField(
                type=int, default=7200, description="仅使用最近活跃的聊天作为素材来源（秒）", min=60
            ),
            "context_messages": ConfigField(
                type=int, default=30, description="生成时读取的最近消息条数", min=5, max=200
            ),
            "enable_memory_retrieval": ConfigField(
                type=bool, default=True, description="生成帖子前是否进行一次记忆检索/总结"
            ),
            "memory_think_level": ConfigField(
                type=int, default=0, description="记忆检索思考等级（0=轻量/低成本，1=正常）", min=0, max=1
            ),
            "allow_urls": ConfigField(type=bool, default=False, description="是否允许帖子包含 URL（默认关闭）"),
            "allow_mentions": ConfigField(type=bool, default=False, description="是否允许帖子包含 @提及（默认关闭）"),
            "max_context_chars": ConfigField(
                type=int, default=3500, description="喂给发帖生成器的上下文最大字符数", min=500, max=20000
            ),
            "max_content_chars": ConfigField(
                type=int, default=1200, description="最终帖子正文最大字符数（超出会截断）", min=200, max=20000
            ),
            "temperature": ConfigField(
                type=float, default=0.7, description="发帖生成温度（0.0-2.0）", min=0.0, max=2.0, step=0.05
            ),
            "max_tokens": ConfigField(type=int, default=800, description="发帖生成最大输出 tokens", min=64, max=2048),
        },
        "writing": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="发帖/回帖前是否按 MaiBot 人设对内容进行润色（建议开启）",
            ),
            "temperature": ConfigField(
                type=float,
                default=0.6,
                description="文案润色温度（0.0-2.0）",
                min=0.0,
                max=2.0,
                step=0.05,
            ),
            "max_tokens": ConfigField(
                type=int,
                default=500,
                description="文案润色最大输出 tokens",
                min=32,
                max=2048,
            ),
            "max_chars": ConfigField(
                type=int,
                default=2000,
                description="草稿最大输入字符数（超出会截断）",
                min=200,
                max=20000,
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

    def _migrate_config_values(self, old_config: dict[str, Any], new_config: dict[str, Any]) -> dict[str, Any]:
        """Plugin-specific config migration.

        - v1.0.2 -> v1.0.3: posting.post_interval_sec (seconds) -> posting.post_interval_min (minutes)
        """

        migrated = super()._migrate_config_values(old_config, new_config)

        try:
            old_posting = old_config.get("posting", {}) if isinstance(old_config.get("posting"), dict) else {}
            old_interval_sec = old_posting.get("post_interval_sec", None)
            if old_interval_sec is None:
                return migrated

            interval_min = int(int(old_interval_sec) / 60)
            # Keep behavior close to the old one.
            if interval_min < 5:
                interval_min = 5
            if interval_min > 10080:
                interval_min = 10080

            posting = migrated.get("posting", {}) if isinstance(migrated.get("posting"), dict) else {}
            posting["post_interval_min"] = interval_min
            migrated["posting"] = posting
        except Exception:
            # Best-effort migration.
            return migrated

        return migrated

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
            (AstrBookPostCommand.get_command_info(), AstrBookPostCommand),
        ]
