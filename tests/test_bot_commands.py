"""
اختبارات تهيئة قائمة أوامر البوت وزر المنيو الجانبي (Telegram Menu Button)
"""
from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock
import pytest
from aiogram import Bot
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault

from app.bot.commands import (
    ADMIN_COMMANDS,
    USER_COMMANDS,
    setup_bot_commands,
)
from app.config import settings


@pytest.mark.asyncio
async def test_setup_bot_commands_scopes_and_menu_button(monkeypatch: pytest.MonkeyPatch):
    """التحقق من تسجيل أوامر المستخدمين العامة وأوامر المشرفين وزر المنيو الجانبي"""
    mock_bot = AsyncMock(spec=Bot)

    # استبدال كائن settings بكائن تجريبي يحتوي على معرّفات مشرفين
    mock_settings = dataclasses.replace(
        settings,
        admin_user_ids=[12345678, 87654321],
        admin_group_id=-100123456789,
    )
    monkeypatch.setattr("app.bot.commands.settings", mock_settings)

    await setup_bot_commands(mock_bot)

    # 1. التحقق من تسجيل أوامر المستخدمين العامة
    mock_bot.set_my_commands.assert_any_call(
        commands=USER_COMMANDS,
        scope=BotCommandScopeDefault(),
    )

    # 2. التحقق من تفعيل زر المنيو الجانبي
    mock_bot.set_chat_menu_button.assert_called()

    # 3. التحقق من تخصيص أوامر المشرفين لكل أدمن
    mock_bot.set_my_commands.assert_any_call(
        commands=ADMIN_COMMANDS,
        scope=BotCommandScopeChat(chat_id=12345678),
    )
    mock_bot.set_my_commands.assert_any_call(
        commands=ADMIN_COMMANDS,
        scope=BotCommandScopeChat(chat_id=87654321),
    )
    mock_bot.set_my_commands.assert_any_call(
        commands=ADMIN_COMMANDS,
        scope=BotCommandScopeChat(chat_id=-100123456789),
    )


def test_command_definitions_integrity():
    """التحقق من صحة واكتمال الأوامر المعرفة للمستخدمين والمشرفين"""
    user_cmds = {cmd.command for cmd in USER_COMMANDS}
    admin_cmds = {cmd.command for cmd in ADMIN_COMMANDS}

    assert {"start", "new", "pricing", "help", "support", "cancel"}.issubset(user_cmds)
    assert {"start", "new", "pricing", "admin", "stats", "broadcast", "cancel"}.issubset(admin_cmds)
