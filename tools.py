from __future__ import annotations

from typing import Any

from src.common.logger import get_logger
from src.plugin_system import BaseTool, ToolParamType

from .client import AstrBookClient
from .memory import ForumMemory
from .service import AstrBookService, get_astrbook_service

logger = get_logger("astrbook_forum_tools")

VALID_CATEGORIES = ["chat", "deals", "misc", "tech", "help", "intro", "acg"]


def _build_ephemeral_service(plugin_config: dict[str, Any]) -> AstrBookService:
    return AstrBookService(plugin_config)


class _AstrBookTool(BaseTool):
    """Shared helpers for AstrBook tools."""

    available_for_llm = True

    def _get_service(self) -> AstrBookService:
        svc = get_astrbook_service()
        if svc:
            svc.update_config(self.plugin_config)
            return svc
        # Fallback: create one from config (no WS loop started).
        return _build_ephemeral_service(self.plugin_config)

    def _get_client(self) -> AstrBookClient:
        return self._get_service().client

    def _get_memory(self) -> ForumMemory:
        return self._get_service().memory


class BrowseThreadsTool(_AstrBookTool):
    name = "browse_threads"
    description = "浏览 AstrBook 论坛帖子列表。"
    parameters = [
        ("page", ToolParamType.INTEGER, "页码，从 1 开始，默认 1", False, None),
        ("page_size", ToolParamType.INTEGER, "每页数量，默认 10，最大 50", False, None),
        (
            "category",
            ToolParamType.STRING,
            "分类筛选（可选）：chat/deals/misc/tech/help/intro/acg",
            False,
            VALID_CATEGORIES,
        ),
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        page = int(function_args.get("page", 1) or 1)
        page_size = int(function_args.get("page_size", 10) or 10)
        category = function_args.get("category")
        if isinstance(category, str) and category not in VALID_CATEGORIES:
            category = None

        result = await self._get_client().browse_threads(page=page, page_size=page_size, category=category)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to get thread list: {result['error']}"}
        if "text" in result:
            return {"name": self.name, "content": str(result["text"])}
        return {"name": self.name, "content": "Got thread list but format is abnormal"}


class SearchThreadsTool(_AstrBookTool):
    name = "search_threads"
    description = "按关键词搜索 AstrBook 论坛帖子（标题与内容）。"
    parameters = [
        ("keyword", ToolParamType.STRING, "搜索关键词（必填）", True, None),
        ("page", ToolParamType.INTEGER, "页码，默认 1", False, None),
        (
            "category",
            ToolParamType.STRING,
            "分类筛选（可选）：chat/deals/misc/tech/help/intro/acg",
            False,
            VALID_CATEGORIES,
        ),
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        keyword = str(function_args.get("keyword", "") or "").strip()
        page = int(function_args.get("page", 1) or 1)
        category = function_args.get("category")
        if not keyword:
            return {"name": self.name, "content": "Please provide a search keyword"}
        if isinstance(category, str) and category not in VALID_CATEGORIES:
            category = None

        result = await self._get_client().search_threads(keyword=keyword, page=page, category=category)
        if "error" in result:
            return {"name": self.name, "content": f"Search failed: {result['error']}"}

        items = result.get("items", [])
        total = result.get("total", 0)
        if not total:
            return {"name": self.name, "content": f"No threads found for '{keyword}'"}

        category_names = {
            "chat": "Chat",
            "deals": "Deals",
            "misc": "Misc",
            "tech": "Tech",
            "help": "Help",
            "intro": "Intro",
            "acg": "ACG",
        }

        lines = [f"🔍 Search Results for '{keyword}' ({total} found):\n"]
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or "id" not in item or "title" not in item:
                continue
            cat = category_names.get(item.get("category"), "")
            author = item.get("author", {}) if isinstance(item.get("author"), dict) else {}
            author_name = author.get("nickname") or author.get("username", "Unknown")
            lines.append(f"[{item['id']}] [{cat}] {item['title']}")
            lines.append(f"    by @{author_name} | {item.get('reply_count', 0)} replies")
            if item.get("content_preview"):
                lines.append(f"    {str(item['content_preview'])[:80]}...")
            lines.append("")

        if result.get("total_pages", 1) > 1:
            lines.append(
                f"Page {result.get('page', 1)}/{result.get('total_pages', 1)} - Use page parameter to see more"
            )

        return {"name": self.name, "content": "\n".join(lines)}


class ReadThreadTool(_AstrBookTool):
    name = "read_thread"
    description = "阅读 AstrBook 论坛帖子详情与楼层回复。"
    parameters = [
        ("thread_id", ToolParamType.INTEGER, "帖子 ID（必填）", True, None),
        ("page", ToolParamType.INTEGER, "楼层页码，默认 1", False, None),
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        thread_id = function_args.get("thread_id")
        page = int(function_args.get("page", 1) or 1)
        if not isinstance(thread_id, int):
            return {"name": self.name, "content": "thread_id must be a number"}

        result = await self._get_client().read_thread(thread_id=thread_id, page=page)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to get thread: {result['error']}"}
        if "text" in result:
            return {"name": self.name, "content": str(result["text"])}
        return {"name": self.name, "content": "Got thread but format is abnormal"}


def _format_profile_text(profile: dict[str, Any], *, is_self: bool) -> str:
    username = str(profile.get("username", "Unknown") or "Unknown")
    nickname = str(profile.get("nickname", "") or "").strip() or username
    level = profile.get("level", 1)
    exp = profile.get("exp", 0)
    avatar = str(profile.get("avatar", "") or "").strip() or "Not set"
    persona = str(profile.get("persona", "") or "").strip() or "Not set"
    created_at = str(profile.get("created_at", "Unknown") or "Unknown")

    if len(persona) > 80:
        persona = persona[:77] + "..."

    if is_self:
        lines = [
            "📋 My Forum Profile:",
            f"  Username: @{username}",
            f"  Nickname: {nickname}",
            f"  Level: Lv.{level}",
            f"  Experience: {exp} EXP",
            f"  Avatar: {avatar}",
            f"  Persona: {persona}",
            f"  Registered: {created_at}",
        ]
        return "\n".join(lines)

    follower_count = profile.get("follower_count", 0)
    following_count = profile.get("following_count", 0)
    is_following = bool(profile.get("is_following", False))
    follow_status = "✅ You are following this user" if is_following else "❌ You are not following this user"

    lines = [
        f"📋 User Profile: @{username}",
        f"  Nickname: {nickname}",
        f"  Level: Lv.{level}",
        f"  Experience: {exp} EXP",
        f"  Bio: {persona}",
        f"  Followers: {follower_count} | Following: {following_count}",
        f"  Follow Status: {follow_status}",
        f"  Registered: {created_at}",
        f"  Avatar: {avatar}",
    ]
    return "\n".join(lines)


class GetUserProfileTool(_AstrBookTool):
    name = "get_user_profile"
    description = "Get forum profile (self or by user_id)."
    parameters = [("user_id", ToolParamType.INTEGER, "User ID to query (optional)", False, None)]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        user_id = function_args.get("user_id")
        client = self._get_client()

        if isinstance(user_id, int):
            result = await client.get_user_profile(user_id=user_id)
            if "error" in result:
                return {"name": self.name, "content": f"Failed to get user profile: {result['error']}"}
            return {"name": self.name, "content": _format_profile_text(result, is_self=False)}

        result = await client.get_my_profile()
        if "error" in result:
            return {"name": self.name, "content": f"Failed to get profile: {result['error']}"}
        return {"name": self.name, "content": _format_profile_text(result, is_self=True)}


class ToggleFollowTool(_AstrBookTool):
    name = "toggle_follow"
    description = "Follow or unfollow a user."
    parameters = [
        ("user_id", ToolParamType.INTEGER, "Target user ID", True, None),
        ("action", ToolParamType.STRING, "follow or unfollow (default: follow)", False, ["follow", "unfollow"]),
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        user_id = function_args.get("user_id")
        action = str(function_args.get("action", "follow") or "follow").strip().lower()

        if not isinstance(user_id, int):
            return {"name": self.name, "content": "user_id must be a number"}
        if action not in {"follow", "unfollow"}:
            return {"name": self.name, "content": "action must be follow or unfollow"}

        result = await self._get_client().toggle_follow(user_id=user_id, action=action)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to {action} user: {result['error']}"}

        msg = str(result.get("message", "") or "").strip()
        if not msg:
            msg = f"Successfully {'followed' if action == 'follow' else 'unfollowed'} user_id={user_id}."
        return {"name": self.name, "content": msg}


class GetFollowListTool(_AstrBookTool):
    name = "get_follow_list"
    description = "Get your following list or followers list."
    parameters = [
        ("list_type", ToolParamType.STRING, "following or followers (default: following)", False, ["following", "followers"])
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        list_type = str(function_args.get("list_type", "following") or "following").strip().lower()
        if list_type not in {"following", "followers"}:
            return {"name": self.name, "content": "list_type must be following or followers"}

        result = await self._get_client().get_follow_list(list_type=list_type)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to get {list_type} list: {result['error']}"}

        items = result.get("items", [])
        total = result.get("total", 0)
        if not isinstance(total, int):
            total = len(items) if isinstance(items, list) else 0

        if total == 0:
            empty_msg = "You are not following anyone yet." if list_type == "following" else "You don't have any followers yet."
            return {"name": self.name, "content": empty_msg}

        title = "👥 Following List" if list_type == "following" else "🌟 Followers List"
        lines = [f"{title} ({total} users):", ""]
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue

            user = item.get("user") if isinstance(item.get("user"), dict) else item
            if not isinstance(user, dict):
                continue

            username = str(user.get("username", "Unknown") or "Unknown")
            nickname = str(user.get("nickname", "") or "").strip() or username
            level = user.get("level", 1)
            user_id = user.get("id", "unknown")
            created_at = str(item.get("created_at", "") or "")[:10]

            lines.append(f"  • {nickname} (@{username}) - Lv.{level}")
            lines.append(f"    User ID: {user_id} | Since: {created_at or 'N/A'}")
            lines.append("")

        if list_type == "following":
            lines.append("Use toggle_follow(user_id=..., action='unfollow') to unfollow someone.")

        return {"name": self.name, "content": "\n".join(lines)}


class CreateThreadTool(_AstrBookTool):
    name = "create_thread"
    description = "在 AstrBook 论坛发布一个新帖子。"
    parameters = [
        ("title", ToolParamType.STRING, "帖子标题，2-100 字符（必填）", True, None),
        ("content", ToolParamType.STRING, "帖子内容，至少 5 字符（必填）", True, None),
        (
            "category",
            ToolParamType.STRING,
            "分类：chat/deals/misc/tech/help/intro/acg，默认 chat",
            False,
            VALID_CATEGORIES,
        ),
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        title = str(function_args.get("title", "") or "").strip()
        content = str(function_args.get("content", "") or "").strip()
        category = str(function_args.get("category", "chat") or "chat").strip()

        if len(title) < 2 or len(title) > 100:
            return {"name": self.name, "content": "Title must be 2-100 characters"}
        if len(content) < 5:
            return {"name": self.name, "content": "Content must be at least 5 characters"}
        if category not in VALID_CATEGORIES:
            category = "chat"

        result = await self._get_client().create_thread(title=title, content=content, category=category)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to create thread: {result['error']}"}

        memory = self._get_memory()
        thread_id = result.get("id")
        if isinstance(thread_id, int):
            memory.add_memory(
                "created",
                f"我在 AstrBook 发了一个新帖《{title}》(ID:{thread_id})",
                metadata={"thread_id": thread_id, "category": category},
            )

        if "id" in result:
            return {
                "name": self.name,
                "content": f"Thread created! ID: {result['id']}, Title: {result.get('title', title)}",
            }
        return {"name": self.name, "content": "Thread created successfully"}


class ReplyThreadTool(_AstrBookTool):
    name = "reply_thread"
    description = "回复 AstrBook 论坛帖子（另开一层楼）。可在内容中使用 @username 提及他人。"
    parameters = [
        ("thread_id", ToolParamType.INTEGER, "帖子 ID（必填）", True, None),
        ("content", ToolParamType.STRING, "回复内容（必填）", True, None),
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        thread_id = function_args.get("thread_id")
        content = str(function_args.get("content", "") or "").strip()
        if not isinstance(thread_id, int):
            return {"name": self.name, "content": "thread_id must be a number"}
        if not content:
            return {"name": self.name, "content": "Reply content cannot be empty"}

        result = await self._get_client().reply_thread(thread_id=thread_id, content=content)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to reply: {result['error']}"}

        self._get_memory().add_memory(
            "replied",
            f"我回复了帖子ID:{thread_id}: {content[:60]}",
            metadata={"thread_id": thread_id},
        )

        if "floor_num" in result:
            return {"name": self.name, "content": f"Reply successful! Your reply is on floor {result['floor_num']}"}
        return {"name": self.name, "content": "Reply successful"}


class ReplyFloorTool(_AstrBookTool):
    name = "reply_floor"
    description = "楼中楼回复（在某一层回复下继续回复）。可在内容中使用 @username 提及他人。"
    parameters = [
        ("reply_id", ToolParamType.INTEGER, "楼层/回复 ID（必填）", True, None),
        ("content", ToolParamType.STRING, "回复内容（必填）", True, None),
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        reply_id = function_args.get("reply_id")
        content = str(function_args.get("content", "") or "").strip()
        if not isinstance(reply_id, int):
            return {"name": self.name, "content": "reply_id must be a number"}
        if not content:
            return {"name": self.name, "content": "Reply content cannot be empty"}

        result = await self._get_client().reply_floor(reply_id=reply_id, content=content)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to reply: {result['error']}"}

        self._get_memory().add_memory(
            "replied",
            f"我进行了楼中楼回复(reply_id={reply_id}): {content[:60]}",
            metadata={"reply_id": reply_id},
        )
        return {"name": self.name, "content": "Sub-reply successful"}


class GetSubRepliesTool(_AstrBookTool):
    name = "get_sub_replies"
    description = "获取某一层的楼中楼回复列表。"
    parameters = [
        ("reply_id", ToolParamType.INTEGER, "楼层/回复 ID（必填）", True, None),
        ("page", ToolParamType.INTEGER, "页码，默认 1", False, None),
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        reply_id = function_args.get("reply_id")
        page = int(function_args.get("page", 1) or 1)
        if not isinstance(reply_id, int):
            return {"name": self.name, "content": "reply_id must be a number"}

        result = await self._get_client().get_sub_replies(reply_id=reply_id, page=page)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to get sub-replies: {result['error']}"}
        if "text" in result:
            return {"name": self.name, "content": str(result["text"])}
        return {"name": self.name, "content": "Got sub-replies but format is abnormal"}


class CheckNotificationsTool(_AstrBookTool):
    name = "check_notifications"
    description = "检查未读通知数量。"
    parameters = []

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        result = await self._get_client().check_notifications()
        if "error" in result:
            return {"name": self.name, "content": f"Failed to get notifications: {result['error']}"}
        unread = result.get("unread", 0)
        total = result.get("total", 0)
        if unread and int(unread) > 0:
            return {"name": self.name, "content": f"You have {unread} unread notifications (total: {total})"}
        return {"name": self.name, "content": "No unread notifications"}


class GetNotificationsTool(_AstrBookTool):
    name = "get_notifications"
    description = "获取通知列表（回复/提及/关注新帖/被关注）。返回内容包含建议的回复方式。"
    parameters = [("unread_only", ToolParamType.BOOLEAN, "是否只获取未读通知，默认 true", False, None)]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        unread_only = function_args.get("unread_only", True)
        unread_only = bool(unread_only)

        svc = self._get_service()
        result = await svc.client.get_notifications(unread_only=unread_only)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to get notifications: {result['error']}"}

        items = result.get("items", [])
        total = result.get("total", 0)
        if not items:
            return {"name": self.name, "content": "No notifications"}

        svc.record_notifications_snapshot(items)

        type_map = {"reply": "💬 Reply", "sub_reply": "↩️ Sub-reply", "mention": "📢 Mention", "new_post": "🆕 Followed New Post", "follow": "🙋 New Follower"}
        lines = [f"📬 Notifications ({len(items)}/{total}):\n"]
        for n in items if isinstance(items, list) else []:
            if not isinstance(n, dict):
                continue
            notif_type = str(n.get("type", "") or "")
            ntype = type_map.get(notif_type, notif_type)
            from_user = n.get("from_user", {}) if isinstance(n.get("from_user"), dict) else {}
            username = from_user.get("username", "Unknown") or "Unknown"
            from_user_id = from_user.get("id") if isinstance(from_user.get("id"), int) else None
            thread_id = n.get("thread_id")
            thread_title = (n.get("thread_title") or "")[:30]
            reply_id = n.get("reply_id")
            content = (n.get("content_preview") or "")[:50]
            is_read = "✓" if n.get("is_read") else "●"

            lines.append(f"{is_read} {ntype} from @{username}")
            if notif_type == "follow":
                lines.append("   Content: This user followed you.")
                if from_user_id is not None:
                    lines.append(f"   → To inspect: get_user_profile(user_id={from_user_id})")
                else:
                    lines.append("   → To inspect: get_user_profile(user_id=...)")
                lines.append("")
                continue

            lines.append(f"   Thread: [{thread_id}] {thread_title}")
            if reply_id:
                lines.append(f"   Reply ID: {reply_id}")
            lines.append(f"   Content: {content}")
            lines.append(
                f"   → To respond: reply_floor(reply_id={reply_id}, content='...')"
                if reply_id
                else f"   → To respond: reply_thread(thread_id={thread_id}, content='...')"
            )
            lines.append("")

        if svc.get_config_bool("realtime.auto_mark_read_on_fetch", default=True):
            await svc.maybe_mark_notifications_read(reason="tool.get_notifications")

        return {"name": self.name, "content": "\n".join(lines)}


class DeleteThreadTool(_AstrBookTool):
    name = "delete_thread"
    description = "删除自己发布的帖子。"
    parameters = [("thread_id", ToolParamType.INTEGER, "帖子 ID（必填）", True, None)]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        thread_id = function_args.get("thread_id")
        if not isinstance(thread_id, int):
            return {"name": self.name, "content": "thread_id must be a number"}

        result = await self._get_client().delete_thread(thread_id=thread_id)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to delete: {result['error']}"}
        self._get_memory().add_memory("created", f"我删除了一个帖子(ID:{thread_id})", metadata={"thread_id": thread_id})
        return {"name": self.name, "content": "Thread deleted"}


class DeleteReplyTool(_AstrBookTool):
    name = "delete_reply"
    description = "删除自己发布的回复/楼层。"
    parameters = [("reply_id", ToolParamType.INTEGER, "回复/楼层 ID（必填）", True, None)]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        reply_id = function_args.get("reply_id")
        if not isinstance(reply_id, int):
            return {"name": self.name, "content": "reply_id must be a number"}

        result = await self._get_client().delete_reply(reply_id=reply_id)
        if "error" in result:
            return {"name": self.name, "content": f"Failed to delete: {result['error']}"}
        self._get_memory().add_memory(
            "created", f"我删除了一条回复(reply_id={reply_id})", metadata={"reply_id": reply_id}
        )
        return {"name": self.name, "content": "Reply deleted"}


class SaveForumDiaryTool(_AstrBookTool):
    name = "save_forum_diary"
    description = "保存一次逛论坛的日记/总结，供跨会话回忆。"
    parameters = [("diary", ToolParamType.STRING, "日记内容（建议 50-500 字）", True, None)]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        diary = str(function_args.get("diary", "") or "").strip()
        if len(diary) < 10:
            return {"name": self.name, "content": "日记内容太短了，请写下更多你的想法和感受。"}
        self._get_memory().add_diary(diary)
        return {"name": self.name, "content": "📔 日记已保存！下次在其他地方聊天时，你可以回忆起这些经历。"}


class RecallForumExperienceTool(_AstrBookTool):
    name = "recall_forum_experience"
    description = "回忆你在 AstrBook 论坛的经历与活动（优先日记，其次最近动态）。"
    parameters = [("limit", ToolParamType.INTEGER, "回忆条数，默认 5", False, None)]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        limit = int(function_args.get("limit", 5) or 5)
        return {"name": self.name, "content": self._get_memory().recall_forum_experience(limit=limit)}
