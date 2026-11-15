"""
إعداد قائمة أوامر البوت وزر المنيو الجانبي (Telegram Bot Menu Button & Commands Scope)
يوفر:
- قائمة أوامر مخصصة للمستخدمين العاديين تظهر عند الضغط على زر Menu
- قائمة أوامر متقدمة خاصة بالمشرفين (Admins) تشمل لوحة الإدارة والإحصائيات والبث
- ضبط زر MenuButtonCommands في تطبيق تيليجرام
"""
from __future__ import annotations

import logging
from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
)
from app.config import settings

logger = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="start", description="🚀 بدء الاستخدام والترحيب"),
    BotCommand(command="new", description="📄 بناء سيرة ذاتية جديدة"),
    BotCommand(command="pricing", description="💳 باقات وأسعار السير وWord"),
    BotCommand(command="help", description="ℹ️ دليل وتعليمات الاستخدام"),
    BotCommand(command="support", description="📞 التواصل مع الدعم الفني"),
    BotCommand(command="cancel", description="❌ إلغاء العملية الحالية"),
]

ADMIN_COMMANDS = [
    BotCommand(command="start", description="🚀 بدء الاستخدام والترحيب"),
    BotCommand(command="new", description="📄 بناء سيرة ذاتية جديدة"),
    BotCommand(command="pricing", description="💳 باقات وأسعار السير وWord"),
    BotCommand(command="admin", description="🛠 لوحة تحكم الإدارة"),
    BotCommand(command="stats", description="📊 إحصائيات البوت اللحظية"),
    BotCommand(command="broadcast", description="📢 إرسال رسالة جماعية"),
    BotCommand(command="cancel", description="❌ إلغاء العملية الحالية"),
]


async def setup_bot_commands(bot: Bot) -> None:
    """تهيئة زر المنيو الجانبي (Menu Button) وقائمة الأوامر للمستخدمين والمشرفين."""
    try:
        # 1. ضبط أوامر المستخدمين العامة (Default Scope)
        await bot.set_my_commands(commands=USER_COMMANDS, scope=BotCommandScopeDefault())

        # 2. تفعيل زر القائمة الجانبي (Menu Button)
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

        # 3. ضبط أوامر المشرفين الخاصة (Admin Scope) لكل مشرف مسجل
        for admin_id in settings.admin_user_ids:
            if admin_id and admin_id != 0:
                try:
                    await bot.set_my_commands(
                        commands=ADMIN_COMMANDS,
                        scope=BotCommandScopeChat(chat_id=admin_id),
                    )
                    await bot.set_chat_menu_button(
                        chat_id=admin_id,
                        menu_button=MenuButtonCommands(),
                    )
                except Exception as e:
                    logger.warning("تعذر ضبط أوامر الأدمن للمشرف %s: %s", admin_id, e)

        # ضبط أوامر مجموعة الإدارة إن وجدت
        if settings.admin_group_id and settings.admin_group_id != 0:
            try:
                await bot.set_my_commands(
                    commands=ADMIN_COMMANDS,
                    scope=BotCommandScopeChat(chat_id=settings.admin_group_id),
                )
            except Exception as e:
                logger.warning("تعذر ضبط أوامر الأدمن لمجموعة الإدارة (%s): %s", settings.admin_group_id, e)

        logger.info("✅ تم إعداد زر المنيو الجانبي وقوائم الأوامر للمستخدمين والمشرفين بنجاح")
    except Exception as e:
        logger.warning("تنبيه أثناء ضبط أوامر البوت وزر المنيو: %s", e)
