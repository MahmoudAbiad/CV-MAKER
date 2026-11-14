"""
Middlewares خاصة بـ aiogram 3.x
- UserRegistrationMiddleware: يسجّل المستخدمين مع كاش محلي لمنع الضغط على Turso
- AdminOnlyMiddleware: يقيّد أوامر لوحة التحكم بالأدمن مع معالجة سليمة لـ CallbackQuery
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, User

from app.config import settings
from app.services.db import db

logger = logging.getLogger(__name__)

# كاش في الذاكرة لتفادي إعادة تسجيل المستخدم في كل رسالة (صلاحية 10 دقائق)
_USER_CACHE: dict[int, float] = {}
CACHE_TTL_SECONDS = 600


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
