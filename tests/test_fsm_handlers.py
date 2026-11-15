"""
اختبارات الوحدة لمنطق الـ FSM وحالات التنقل والـ Handlers و Middlewares في البوت
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock
from typing import Any
import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, User

from app.bot.handlers import (
    CvBuildStates,
    cmd_cancel,
    cmd_start,
    handle_back_to_style,
    handle_back_to_text,
    handle_cv_style_choice,
    handle_free_text_cv,
    handle_more_info_choice,
    handle_more_info_reply,
    handle_save_edited_section,
    handle_start_edit_section,
    handle_skip_more_info,
)
from app.bot.middlewares import AdminOnlyMiddleware, UserRegistrationMiddleware
from app.config import settings


@pytest.fixture
def memory_storage():
    return MemoryStorage()


@pytest.fixture
def fsm_context(memory_storage: MemoryStorage):
    key = StorageKey(bot_id=123456789, chat_id=11223344, user_id=11223344)
    return FSMContext(storage=memory_storage, key=key)


@pytest.fixture
def mock_message():
    msg = MagicMock(spec=Message)
    msg.chat = MagicMock(spec=Chat)
    msg.chat.id = 11223344
    msg.from_user = MagicMock(spec=User)
    msg.from_user.id = 11223344
    msg.from_user.full_name = "أحمد تيست"
    msg.from_user.username = "ahmed_test"
    msg.from_user.is_bot = False
    msg.text = ""
    msg.answer = AsyncMock()
    return msg


@pytest.fixture
def mock_callback():
    cb = MagicMock(spec=CallbackQuery)
    cb.id = "cb_query_123"
    cb.data = ""
    cb.from_user = MagicMock(spec=User)
    cb.from_user.id = 11223344
    cb.from_user.full_name = "أحمد تيست"
    cb.from_user.is_bot = False
    cb.message = MagicMock(spec=Message)
    cb.message.chat = MagicMock(spec=Chat)
    cb.message.chat.id = 11223344
    cb.message.answer = AsyncMock()
    cb.message.edit_reply_markup = AsyncMock()
    cb.answer = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_cmd_start_and_cancel_clear_fsm(mock_message: Message, fsm_context: FSMContext):
    """التحقق من أن أمري /start و /cancel يصفران الـ FSM ويعيدان القائمة الترحيبية"""
    await fsm_context.set_state(CvBuildStates.waiting_for_style)
    await fsm_context.update_data(dummy="test")

    # تنفيذ /start
    await cmd_start(mock_message, fsm_context)
    assert await fsm_context.get_state() is None
    assert await fsm_context.get_data() == {}
    mock_message.answer.assert_called_once()
    assert "مرحباً بك" in mock_message.answer.call_args[0][0]

    # تعيين حالة أخرى وتجربة /cancel
    await fsm_context.set_state(CvBuildStates.waiting_for_more_info)
    mock_message.answer.reset_mock()
    await cmd_cancel(mock_message, fsm_context)
    assert await fsm_context.get_state() is None
    assert "تم إلغاء العملية" in mock_message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_free_text_cv_short_text_rejection(mock_message: Message, fsm_context: FSMContext):
    """التحقق من رفض النصوص القصيرة جداً (<15 حرف) وعدم الانتقال لحالة جديدة"""
    mock_message.text = "مرحبا انا احمد"  # أقل من 15 حرف
    await handle_free_text_cv(mock_message, fsm_context)

    # الحالة تبقى فارغة
    assert await fsm_context.get_state() is None
    mock_message.answer.assert_called_once()
    assert "النص قصير جداً" in mock_message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_free_text_cv_valid_text_transition(mock_message: Message, fsm_context: FSMContext):
    """التحقق من قبول النص الصالح وتخزينه والانتقال لحالة اختيار الأسلوب"""
    valid_text = "أنا مهندس برمجيات متخرج من جامعة دمشق 2022 خبرة في بايثون وفلاتر سنتين"
    mock_message.text = valid_text
    await handle_free_text_cv(mock_message, fsm_context)

    # التحقق من الحالة والبيانات
    assert await fsm_context.get_state() == CvBuildStates.waiting_for_style.state
    data = await fsm_context.get_data()
    assert data["raw_text"] == valid_text
    mock_message.answer.assert_called_once()
    assert "اختر أسلوب الصياغة" in mock_message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cv_style_choice_and_back_navigation(mock_callback: CallbackQuery, fsm_context: FSMContext):
    """التحقق من اختيار الأسلوب والتنقل العكسي (Back Navigation) إلى خطوة النص"""
    # 1. اختيار الأسلوب المعزز
    mock_callback.data = "cv_style:enhanced"
    await handle_cv_style_choice(mock_callback, fsm_context)

    assert await fsm_context.get_state() == CvBuildStates.waiting_for_language.state
    data = await fsm_context.get_data()
    assert data["style"] == "enhanced"

    # 2. الرجوع لاختيار الأسلوب
    mock_callback.data = "nav:back_to_style"
    await handle_back_to_style(mock_callback, fsm_context)
    assert await fsm_context.get_state() == CvBuildStates.waiting_for_style.state

    # 3. الرجوع لإدخال النص
    mock_callback.data = "nav:back_to_text"
    await handle_back_to_text(mock_callback, fsm_context)
    assert await fsm_context.get_state() == CvBuildStates.waiting_for_text.state


@pytest.mark.asyncio
async def test_handle_more_info_choice_invalid_index(mock_callback: CallbackQuery, fsm_context: FSMContext):
    """التحقق من معالجة الضغط على خيارات انتهت صلاحيتها أو معرّف غير صالح"""
    mock_callback.data = "cvq:99"  # فهرس غير موجود
    await fsm_context.update_data(pending_options=["خيار 1", "خيار 2"])

    mock_bot = AsyncMock()
    await handle_more_info_choice(mock_callback, fsm_context, mock_bot)
    mock_callback.message.answer.assert_called_once()
    assert "انتهت صلاحية هذه الخيارات" in mock_callback.message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_section_editing_in_preview_dashboard(
    mock_callback: CallbackQuery, mock_message: Message, fsm_context: FSMContext, sample_arabic_cv_data: dict[str, Any]
):
    """التحقق من تعديل حقل مخصص (مثل المسمى الوظيفي) في لوحة المعاينة التفاعلية"""
    await fsm_context.set_state(CvBuildStates.preview_dashboard)
    await fsm_context.update_data(cv_data=sample_arabic_cv_data, language="ar", style="enhanced")

    # 1. الضغط على زر تعديل المسمى الوظيفي
    mock_callback.data = "cv_edit:title"
    await handle_start_edit_section(mock_callback, fsm_context)
    assert await fsm_context.get_state() == CvBuildStates.editing_section.state
    data_during_edit = await fsm_context.get_data()
    assert data_during_edit["editing_section_name"] == "title"

    # 2. إرسال المستخدم للنص الجديد
    mock_message.text = "رئيس قسم تطوير النظم السحابية"
    await handle_save_edited_section(mock_message, fsm_context)

    # التحقق من العودة للوحة المعاينة وتحديث العنوان
    assert await fsm_context.get_state() == CvBuildStates.preview_dashboard.state
    updated_data = await fsm_context.get_data()
    assert updated_data["cv_data"]["title"] == "رئيس قسم تطوير النظم السحابية"


@pytest.mark.asyncio
async def test_admin_only_middleware_blocks_unauthorized(mock_callback: CallbackQuery):
    """التحقق من أن AdminOnlyMiddleware يمنع غير المشرفين من تنفيذ الإجراءات"""
    middleware = AdminOnlyMiddleware()
    handler = AsyncMock()

    # مستخدم غير مسجل كأدمن
    mock_callback.from_user.id = 888888
    event_data = {"event_from_user": mock_callback.from_user}

    result = await middleware(handler, mock_callback, event_data)
    assert result is None
    handler.assert_not_called()
    mock_callback.answer.assert_called_once()
    assert "مخصص لمشرفي النظام فقط" in mock_callback.answer.call_args[0][0]

    # مستخدم مسجل كأدمن (999001 موجود في ADMIN_USER_IDS)
    mock_callback.from_user.id = 999001
    mock_callback.answer.reset_mock()
    handler.return_value = "HANDLER_CALLED"

    result_admin = await middleware(handler, mock_callback, event_data)
    assert result_admin == "HANDLER_CALLED"
    handler.assert_called_once()


@pytest.mark.asyncio
async def test_handle_more_info_reply_attaches_dialogue_context(
    mock_message: Message, fsm_context: FSMContext, monkeypatch: pytest.MonkeyPatch
):
    """التحقق من ربط إجابة المستخدم بسياق السؤال المطروح لمنع التكرار والتعليق"""
    await fsm_context.set_state(CvBuildStates.waiting_for_more_info)
    await fsm_context.update_data(
        raw_text="أحمد، هاتف 0991234567",
        last_question_text="شو آخر شركة اشتغلت معها ومسماك الوظيفي؟",
        attempts=1,
    )
    mock_message.text = "محاسب في شركة الشام"

    passed_raw_text = []
    async def fake_completeness_check(msg, state, raw_text, lang, style, attempts, bot, force_complete=False):
        passed_raw_text.append(raw_text)

    monkeypatch.setattr("app.bot.handlers._run_completeness_check", fake_completeness_check)

    mock_bot = AsyncMock()
    await handle_more_info_reply(mock_message, fsm_context, mock_bot)

    data = await fsm_context.get_data()
    assert "[سؤال توضيحي: شو آخر شركة اشتغلت معها ومسماك الوظيفي؟]" in data["raw_text"]
    assert "[إجابة المستخدم: محاسب في شركة الشام]" in data["raw_text"]
    assert data["last_question_text"] is None
    assert len(passed_raw_text) == 1
    assert "شركة الشام" in passed_raw_text[0]
