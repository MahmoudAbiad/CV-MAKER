"""
Middlewares خاصة بـ aiogram 3.x
- UserRegistrationMiddleware: يسجّل/يحدّث المستخدم تلقائياً في Turso عند أي تفاعل
- AdminOnlyMiddleware: يقيّد أوامر لوحة التحكم بالأدمن فقط
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.config import settings
from app.services.db import db


class UserRegistrationMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is not None:
            try:
                await db.upsert_user(user.id, user.username, user.full_name)
            except Exception:
                # لا نمنع معالجة الرسالة إن فشل تسجيل المستخدم لأي سبب مؤقت
                pass
        return await handler(event, data)


class AdminOnlyMiddleware(BaseMiddleware):
    """يُطبَّق فقط على الراوترات الخاصة بلوحة تحكم الأدمن."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id not in settings.admin_user_ids:
            return None  # تجاهل صامت لغير المخوّلين
        return await handler(event, data)
