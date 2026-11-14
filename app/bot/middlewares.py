"""
Middlewares خاصة بـ aiogram 3.x
- UserRegistrationMiddleware: يسجّل المستخدمين مع كاش محلي لمنع الضغط على Turso
- AdminOnlyMiddleware: يقيّد أوامر لوحة التحكم بالأدمن مع معالجة سليمة لـ CallbackQuery
- ThrottlingMiddleware: مكافحة الإغراق والتحكم بمعدل الطلبات (Anti-Spam & Rate Limiting)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.config import settings
from app.services.db import db

logger = logging.getLogger(__name__)

# كاش في الذاكرة لتفادي إعادة تسجيل المستخدم في كل رسالة (صلاحية 10 دقائق)
_USER_CACHE: dict[int, float] = {}
CACHE_TTL_SECONDS = 600
MAX_CACHE_ENTRIES = 2000

# سجل تتبع الطلبات لمنع الإغراق (Anti-Spam Rate Limiter)
_THROTTLE_RECORDS: dict[int, list[float]] = {}
_LAST_WARNED: dict[int, float] = {}
THROTTLE_WINDOW_SECONDS = 10.0
THROTTLE_MAX_REQUESTS = 6


def _prune_caches_if_needed(now: float) -> None:
    """تنظيف دوري لمنع تسريب الذاكرة وتراكم السجلات في بيئة 512MB RAM."""
    if len(_USER_CACHE) > MAX_CACHE_ENTRIES:
        expired = [uid for uid, ts in _USER_CACHE.items() if now - ts > CACHE_TTL_SECONDS]
        for uid in expired:
            _USER_CACHE.pop(uid, None)
        if len(_USER_CACHE) > MAX_CACHE_ENTRIES:
            oldest = sorted(_USER_CACHE.items(), key=lambda x: x[1])[:500]
            for uid, _ in oldest:
                _USER_CACHE.pop(uid, None)

    if len(_THROTTLE_RECORDS) > MAX_CACHE_ENTRIES:
        _THROTTLE_RECORDS.clear()
        _LAST_WARNED.clear()


class ThrottlingMiddleware(BaseMiddleware):
    """
    ميدلوير متقدم لمكافحة الإغراق وحظر الهجمات التكرارية (Rate Limiting).
    يحمي موارد السيرفر، وقاعدة البيانات، وحصص Gemini API من الاستنزاف.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.is_bot:
            return await handler(event, data)

        # استثناء المشرفين من قيود المعدل
        if user.id in settings.admin_user_ids:
            return await handler(event, data)

        now = time.monotonic()
        _prune_caches_if_needed(now)
        history = _THROTTLE_RECORDS.get(user.id, [])
        # إزالة الطوابع الزمنية خارج نافذة الفحص
        history = [ts for ts in history if now - ts < THROTTLE_WINDOW_SECONDS]
        _THROTTLE_RECORDS[user.id] = history

        if len(history) >= THROTTLE_MAX_REQUESTS:
            # حظر الطلب وإشعار المستخدم مرة واحدة كل 5 ثوانٍ لتفادي إغراق الردود
            last_warn = _LAST_WARNED.get(user.id, 0.0)
            if now - last_warn > 5.0:
                _LAST_WARNED[user.id] = now
                warning_text = (
                    "⚠️ <b>تنبيه:</b> لقد أرسلت عدداً كبيراً من الطلبات في وقت قصير.\n"
                    "يرجى الانتظار بضع ثوانٍ قبل المتابعة لمنع حظر الخدمة مؤقتاً."
                )
                if isinstance(event, Message):
                    try:
                        await event.answer(warning_text)
                    except Exception:
                        pass
                elif isinstance(event, CallbackQuery):
                    try:
                        await event.answer(
                            "⚠️ لقد أرسلت طلبات كثيرة جداً، انتظر ثوانٍ معدودة.",
                            show_alert=True,
                        )
                    except Exception:
                        pass
            return None

        # تسجيل وقت الطلب الحالي
        history.append(now)
        _THROTTLE_RECORDS[user.id] = history
        return await handler(event, data)


class UserRegistrationMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        
        if user is not None and not user.is_bot:
            now = time.monotonic()
            last_seen = _USER_CACHE.get(user.id, 0)
            
            if now - last_seen > CACHE_TTL_SECONDS:
                try:
                    await db.upsert_user(user.id, user.username, user.full_name)
                    _USER_CACHE[user.id] = now
                except Exception:
                    logger.exception(
                        "فشل تسجيل/تحديث المستخدم %s بجدول users",
                        user.id,
                    )
        
        return await handler(event, data)


class AdminOnlyMiddleware(BaseMiddleware):
    """يُطبَّق فقط على الراوترات الخاصة بلوحة تحكم الأدمن."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        
        if user is None or user.id not in settings.admin_user_ids:
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ هذا الإجراء مخصص لمشرفي النظام فقط.", show_alert=True)
            return None

        return await handler(event, data)
