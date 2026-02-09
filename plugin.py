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

from .actions import (
    AstrBookBrowseThreadsAction,
    AstrBookCheckBlockStatusAction,
    AstrBookCheckNotificationsAction,
    AstrBookBlockUserAction,
    AstrBookCreateThreadAction,
    AstrBookDeleteReplyAction,
    AstrBookDeleteThreadAction,
    AstrBookGetBlockListAction,
    AstrBookGetMyProfileAction,
    AstrBookGetNotificationsAction,
    AstrBookGetSubRepliesAction,
    AstrBookLikeContentAction,
    AstrBookReadThreadAction,
    AstrBookRecallForumExperienceAction,
    AstrBookReplyFloorAction,
    AstrBookReplyThreadAction,
    AstrBookSaveForumDiaryAction,
    AstrBookSearchThreadsAction,
    AstrBookSearchUsersAction,
    AstrBookUnblockUserAction,
)
from .commands import AstrBookBrowseCommand, AstrBookPostCommand, AstrBookStatusCommand
from .service import AstrBookService, get_astrbook_service, set_astrbook_service

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
        "realtime": "实时通知（SSE）",
        "browse": "定时逛帖",
        "posting": "定时主动发帖（风控）",
        "memory": "论坛记忆",
        "llm": "模型槽位路由（映射到 MaiBot 的 model_task_config）",
    }

    config_schema: dict = {
        "plugin": {
            "config_version": ConfigField(type=str, default="1.0.11", description="配置文件版本"),
            "enabled": ConfigField(type=bool, default=False, description="是否启用插件"),
        },
        "astrbook": {
            "api_base": ConfigField(
                type=str,
                default="https://book.astrbot.app",
                description="AstrBook 后端 API 地址",
                placeholder="https://book.astrbot.app",
            ),
            "token": ConfigField(
                type=str,
                default="",
                description="Bot Token（在 AstrBook 网页端个人中心获取）",
                input_type="password",
                placeholder="请输入 Token",
            ),
            "timeout_sec": ConfigField(type=int, default=40, description="HTTP 请求超时时间（秒）", min=1, max=120),
        },
        "realtime": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用 SSE 实时通知"),
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
            "reply_max_tokens": ConfigField(
                type=int,
                default=8192,
                description="自动回帖/自动生成回复最大输出 tokens",
                min=64,
                max=8192,
            ),
            "autonomous_social_actions": ConfigField(
                type=bool,
                default=True,
                description="自动回复流程是否允许自主点赞（默认开启）",
            ),
            "autonomous_block": ConfigField(
                type=bool,
                default=False,
                description="自动回复流程是否允许自主拉黑（高风险，默认关闭）",
            ),
            "auto_mark_read": ConfigField(
                type=bool,
                default=True,
                description="是否启用自动将通知标记为已读",
            ),
            "auto_mark_read_on_auto_reply": ConfigField(
                type=bool,
                default=True,
                description="触发自动回复后，是否自动标记通知为已读",
            ),
            "auto_mark_read_on_fetch": ConfigField(
                type=bool,
                default=True,
                description="调用 get_notifications 后是否自动标记通知为已读",
            ),
            "auto_mark_read_cooldown_sec": ConfigField(
                type=int,
                default=2,
                description="自动标记已读的最小间隔（秒）",
                min=0,
                max=300,
            ),
        },
        "browse": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用定时逛帖"),
            "browse_interval_sec": ConfigField(type=int, default=3600, description="逛帖间隔（秒）", min=30),
            "max_replies_per_session": ConfigField(
                type=int, default=1, description="每次逛帖最多回帖次数", min=0, max=5
            ),
            "browse_max_tokens": ConfigField(
                type=int,
                default=8192,
                description="逛帖决策/逛帖回帖生成最大输出 tokens",
                min=64,
                max=8192,
            ),
            "autonomous_social_actions": ConfigField(
                type=bool,
                default=True,
                description="定时逛帖流程是否允许自主点赞（默认开启）",
            ),
            "autonomous_block": ConfigField(
                type=bool,
                default=False,
                description="定时逛帖流程是否允许自主拉黑（高风险，默认关闭）",
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
            "max_tokens": ConfigField(
                type=int,
                default=8192,
                description="发帖生成最大输出 tokens",
                min=64,
                max=8192,
            ),
        },
        "llm": {
            "default_slot": ConfigField(
                type=str,
                default="replyer",
                description="默认模型槽位（映射到 MaiBot model_task_config，例如 replyer/planner/tool_use/utils）",
            ),
            "realtime_auto_reply_slot": ConfigField(
                type=str,
                default="replyer",
                description="实时通知自动回帖使用的模型槽位",
            ),
            "browse_decision_slot": ConfigField(
                type=str,
                default="replyer",
                description="定时逛帖-读帖前决策使用的模型槽位",
            ),
            "browse_reply_slot": ConfigField(
                type=str,
                default="replyer",
                description="定时逛帖-读帖后是否回复/回复内容生成使用的模型槽位",
            ),
            "proactive_post_slot": ConfigField(
                type=str,
                default="replyer",
                description="定时主动发帖使用的模型槽位",
            ),
            "action_create_thread_draft_slot": ConfigField(
                type=str,
                default="replyer",
                description="astrbook_create_thread 自动补全标题/正文使用的模型槽位",
            ),
            "action_reply_thread_slot": ConfigField(
                type=str,
                default="replyer",
                description="astrbook_reply_thread 自动生成回帖使用的模型槽位",
            ),
            "action_reply_floor_slot": ConfigField(
                type=str,
                default="replyer",
                description="astrbook_reply_floor 自动生成楼中楼回复使用的模型槽位",
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
            "record_notification_events": ConfigField(
                type=bool,
                default=True,
                description="是否把通知事件写入论坛记忆",
            ),
            "record_new_thread_events": ConfigField(
                type=bool,
                default=True,
                description="是否把 new_thread 实时事件写入论坛记忆",
            ),
        },
    }

    def _migrate_config_values(self, old_config: dict[str, Any], new_config: dict[str, Any]) -> dict[str, Any]:
        """Plugin-specific config migration.

        - v1.0.2 -> v1.0.3: posting.post_interval_sec (seconds) -> posting.post_interval_min (minutes)
        - v1.0.3 -> v1.0.4: bump max_tokens defaults to 8192 (posting) and add browse/realtime max_tokens
        - v1.0.5 -> v1.0.6: remove writing.* config and "rewrite/polish" stage (always post directly)
        - v1.0.6 -> v1.0.7: add autonomous_social_actions switches for realtime/browse
        - v1.0.7 -> v1.0.8: keep autonomous_social_actions default-on for likes and add autonomous_block switches
        - v1.0.8 -> v1.0.9: realtime transport migrated from WebSocket to SSE
        - v1.0.9 -> v1.0.10: add llm.* model slot routing config
        - v1.0.10 -> v1.0.11: add auto-mark-read and notification memory controls
        """

        migrated = super()._migrate_config_values(old_config, new_config)

        try:
            old_posting = old_config.get("posting", {}) if isinstance(old_config.get("posting"), dict) else {}
            old_interval_sec = old_posting.get("post_interval_sec", None)
            if old_interval_sec is not None:
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
            pass

        # Update max_tokens defaults (only when user hasn't changed them).
        try:
            old_posting = old_config.get("posting", {}) if isinstance(old_config.get("posting"), dict) else {}
            posting = migrated.get("posting", {}) if isinstance(migrated.get("posting"), dict) else {}
            if "max_tokens" in posting:
                old_val = old_posting.get("max_tokens", None)
                if old_val is None or int(old_val) in {800, 2048}:
                    posting["max_tokens"] = 8192
            migrated["posting"] = posting
        except Exception:
            pass

        return migrated

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if not self.enable_plugin:
            return

        service = AstrBookService(self.config)
        set_astrbook_service(service)

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            # Actions (user-interactive forum operations)
            (AstrBookBrowseThreadsAction.get_action_info(), AstrBookBrowseThreadsAction),
            (AstrBookSearchThreadsAction.get_action_info(), AstrBookSearchThreadsAction),
            (AstrBookReadThreadAction.get_action_info(), AstrBookReadThreadAction),
            (AstrBookGetMyProfileAction.get_action_info(), AstrBookGetMyProfileAction),
            (AstrBookLikeContentAction.get_action_info(), AstrBookLikeContentAction),
            (AstrBookGetBlockListAction.get_action_info(), AstrBookGetBlockListAction),
            (AstrBookBlockUserAction.get_action_info(), AstrBookBlockUserAction),
            (AstrBookUnblockUserAction.get_action_info(), AstrBookUnblockUserAction),
            (AstrBookCheckBlockStatusAction.get_action_info(), AstrBookCheckBlockStatusAction),
            (AstrBookSearchUsersAction.get_action_info(), AstrBookSearchUsersAction),
            (AstrBookCreateThreadAction.get_action_info(), AstrBookCreateThreadAction),
            (AstrBookReplyThreadAction.get_action_info(), AstrBookReplyThreadAction),
            (AstrBookReplyFloorAction.get_action_info(), AstrBookReplyFloorAction),
            (AstrBookGetSubRepliesAction.get_action_info(), AstrBookGetSubRepliesAction),
            (AstrBookCheckNotificationsAction.get_action_info(), AstrBookCheckNotificationsAction),
            (AstrBookGetNotificationsAction.get_action_info(), AstrBookGetNotificationsAction),
            (AstrBookDeleteThreadAction.get_action_info(), AstrBookDeleteThreadAction),
            (AstrBookDeleteReplyAction.get_action_info(), AstrBookDeleteReplyAction),
            (AstrBookSaveForumDiaryAction.get_action_info(), AstrBookSaveForumDiaryAction),
            (AstrBookRecallForumExperienceAction.get_action_info(), AstrBookRecallForumExperienceAction),
            # Event handlers
            (AstrBookStartupHandler.get_handler_info(), AstrBookStartupHandler),
            (AstrBookStopHandler.get_handler_info(), AstrBookStopHandler),
            # Commands (admin / diagnostics)
            (AstrBookStatusCommand.get_command_info(), AstrBookStatusCommand),
            (AstrBookBrowseCommand.get_command_info(), AstrBookBrowseCommand),
            (AstrBookPostCommand.get_command_info(), AstrBookPostCommand),
        ]
