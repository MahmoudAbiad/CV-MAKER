"""
معالجات (Handlers) بوت تيليجرام - aiogram 3.x
خالٍ تماماً من الاستيرادات الدائرية، آمن ضد حالات السباق، ومحمي ضد التكرار.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    Message,
)

from app.bot.keyboards import (
    admin_broadcast_confirm_keyboard,
    admin_panel_keyboard,
    admin_reject_reasons_keyboard,
    admin_review_keyboard,
    cancel_action_keyboard,
    cv_language_keyboard,
    cv_style_keyboard,
    docx_offer_keyboard,
    more_info_choice_keyboard,
    payment_instructions_keyboard,
)
from app.config import settings
from app.queue.redis_client import enqueue
from app.services import gemini_service
from app.services.db import db

logger = logging.getLogger(__name__)

user_router = Router(name="user")
admin_router = Router(name="admin")

MAX_INFO_ROUNDS = 4


class CvBuildStates(StatesGroup):
    waiting_for_style = State()
    waiting_for_language = State()
    processing = State()          # قفل لمنع Race Condition أثناء تشغيل Gemini
    waiting_for_more_info = State()


class PaymentStates(StatesGroup):
    waiting_for_receipt = State()


class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    confirm_send = State()


class RejectStates(StatesGroup):
    waiting_for_reason = State()


WELCOME_TEXT = (
    "👋 <b>أهلاً بك في بوت إنشاء السير الذاتية الاحترافية!</b>\n\n"
    "فقط أرسل لي نبذة عن خبراتك ودراستك ومهاراتك بأسلوبك الخاص "
    "(حتى لو باللهجة العامية، وبالعربية أو الإنجليزية).\n\n"
    "سأقوم بصياغتها بشكل احترافي وتقديم سيرة ذاتية PDF مجانية تماماً! 📄✨\n\n"
    "<i>مثال: أنا أحمد، مهندس برمجيات خبرة سنتين في بايثون وداتابيز، خريج هندسة حواسيب 2022...</i>\n\n"
    "💡 <i>يمكنك في أي وقت كتابة /cancel للبدء من جديد.</i>"
)


# =====================================================================
# أوامر الإلغاء والبداية العامة
# =====================================================================

@user_router.message(Command("cancel"))
@user_router.callback_query(F.data == "action:cancel")
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext) -> None:
    """إلغاء أي عملية حالية وتصفير الـ FSM تماماً."""
    await state.clear()
    text = "❌ تم إلغاء العملية الحالية بنجاح. يمكنك إرسال وصف جديد للبدء متى شئت."
    if isinstance(event, CallbackQuery):
        await event.answer("تم الإلغاء")
        try:
            await event.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await event.message.answer(text)
    else:
        await event.answer(text)


@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME_TEXT)


# =====================================================================
# مسار بناء السيرة الذاتية المجانية (PDF)
# =====================================================================

@user_router.message(F.text, ~F.text.startswith("/"), StateFilter(None))
async def handle_free_text_cv(message: Message, state: FSMContext) -> None:
    if not message.text or len(message.text.strip()) < 15:
        await message.answer(
            "🤔 النص قصير جداً لبناء سيرة ذاتية متكاملة. "
            "أرسل لي وصفاً أوسع لخبراتك ومهاراتك من فضلك."
        )
        return

    await state.update_data(raw_text=message.text)
    await state.set_state(CvBuildStates.waiting_for_style)

    await message.answer(
        "تمام، وصلتني المعلومات 👌\n\n"
        "قبل أن نكمل، هل تفضل أن تظهر سيرتك كما كتبتها بالضبط، "
        "أم ترغب في أن يقوم الذكاء الاصطناعي بتلميعها وتحسين الصياغة؟",
        reply_markup=cv_style_keyboard(),
    )


@user_router.callback_query(
    StateFilter(CvBuildStates.waiting_for_style), F.data.startswith("cv_style:")
)
async def handle_cv_style_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    style = callback.data.split(":")[1]

    await state.update_data(style=style)
    await state.set_state(CvBuildStates.waiting_for_language)

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer(
        "🌐 بأي لغة تفضل أن تكون سيرتك الذاتية؟",
        reply_markup=cv_language_keyboard(),
    )


@user_router.callback_query(
    StateFilter(CvBuildStates.waiting_for_language), F.data.startswith("cv_lang:")
)
async def handle_cv_language_choice(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    language = callback.data.split(":")[1]

    data = await state.get_data()
    raw_text = data.get("raw_text")
    style = data.get("style", "enhanced")

    if not raw_text:
        await state.clear()
        await callback.message.answer("⚠️ انتهت صلاحية الجلسة، من فضلك أعد إرسال وصف خبراتك من جديد.")
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.update_data(language=language, attempts=0)
    await state.set_state(CvBuildStates.processing)
    await _run_completeness_check(callback.message, state, raw_text, language, style, attempts=0, bot=bot)


async def _run_completeness_check(
    message: Message,
    state: FSMContext,
    raw_text: str,
    language: str,
    style: str,
    attempts: int,
    bot: Bot,
) -> None:
    wait_msg = await message.answer("⏳ لحظة، نقوم بمراجعة وتحليل بياناتك...")
    
    async def _keep_typing():
        try:
            while True:
                await bot.send_chat_action(message.chat.id, "typing")
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())
    force_complete = attempts >= MAX_INFO_ROUNDS

    try:
        result = await gemini_service.check_and_extract_cv(
            raw_text, language=language, style=style, force_complete=force_complete
        )
    except Exception:
        logger.exception("فشل تحليل بيانات السيرة الذاتية عبر Gemini")
        await state.clear()
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await message.answer("⚠️ حدث خطأ أثناء تحليل البيانات. يرجى المحاولة مجدداً بعد قليل.")
        return
    finally:
        typing_task.cancel()
        try:
            await wait_msg.delete()
        except Exception:
            pass

    if result.get("status") != "complete":
        question = result.get("question") or {}
        question_text = question.get("text") or result.get("follow_up_message") or (
            "هل يمكنك تزويدنا بمعلومات إضافية عن خبراتك أو مهاراتك؟"
        )
        question_type = question.get("question_type")
        options = [o for o in (question.get("options") or []) if isinstance(o, str) and o.strip()]

        await state.update_data(attempts=attempts + 1)
        await state.set_state(CvBuildStates.waiting_for_more_info)

        if question_type == "choice" and 2 <= len(options) <= 4:
            await state.update_data(pending_question_text=question_text, pending_options=options)
            await message.answer(question_text, reply_markup=more_info_choice_keyboard(options))
        else:
            await state.update_data(pending_question_text=None, pending_options=None)
            await message.answer(question_text, reply_markup=cancel_action_keyboard())
        return

    await state.clear()
    await message.answer("✅ ممتاز! اكتملت كافة البيانات. جارٍ بناء وتصميم ملف الـ PDF الخاص بك...")

    await enqueue(
        settings.queue_cv_generation,
        {
            "chat_id": message.chat.id,
            "user_id": message.from_user.id if message.from_user else message.chat.id,
            "cv_data": result,
        },
    )


@user_router.message(StateFilter(CvBuildStates.processing))
async def handle_busy_state(message: Message) -> None:
    await message.answer("⏳ طلبك قيد المعالجة والتحليل حالياً، يرجى الانتظار ثوانٍ معدودة...")


@user_router.message(StateFilter(CvBuildStates.waiting_for_more_info), F.text, ~F.text.startswith("/"))
async def handle_more_info_reply(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    raw_text = data.get("raw_text", "")
    language = data.get("language", "ar")
    style = data.get("style", "enhanced")
    attempts = data.get("attempts", 0)

    combined_text = f"{raw_text}\n{message.text}".strip()
    await state.update_data(raw_text=combined_text, pending_question_text=None, pending_options=None)
    await state.set_state(CvBuildStates.processing)

    await _run_completeness_check(message, state, combined_text, language, style, attempts=attempts, bot=bot)


@user_router.callback_query(
    StateFilter(CvBuildStates.waiting_for_more_info), F.data.startswith("cvq:")
)
async def handle_more_info_choice(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    data = await state.get_data()
    options: list[str] = data.get("pending_options") or []
    question_text: str = data.get("pending_question_text") or ""

    try:
        option_idx = int(callback.data.split(":")[1])
        selected_option = options[option_idx]
    except (ValueError, IndexError):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("⚠️ انتهت صلاحية هذه الخيارات، يمكنك كتابة إجابتك كنص عادي.")
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    raw_text = data.get("raw_text", "")
    language = data.get("language", "ar")
    style = data.get("style", "enhanced")
    attempts = data.get("attempts", 0)

    combined_text = f"{raw_text}\n{question_text}: {selected_option}".strip()
    await state.update_data(raw_text=combined_text, pending_question_text=None, pending_options=None)
    await state.set_state(CvBuildStates.processing)

    await _run_completeness_check(callback.message, state, combined_text, language, style, attempts=attempts, bot=bot)


# =====================================================================
# مسار شراء نسخة Word (DOCX) والدفع عبر Sham Cash
# =====================================================================

@user_router.callback_query(F.data == "skip_docx")
async def handle_skip_docx(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("بالتوفيق لك في مسيرتك المهنية! 🍀 يسعدنا دائماً خدمتك.")


@user_router.callback_query(F.data.startswith("buy_docx:"))
async def handle_buy_docx(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    cv_id = int(callback.data.split(":")[1])
    await state.update_data(cv_id=cv_id)

    safe_code = html.escape(settings.sham_cash_code)
    caption = (
        f"💳 <b>للحصول على نسخة Word قابلة للتعديل ({settings.docx_price_usd}$ / {settings.docx_price_syp} ل.س):</b>\n\n"
        f"🔢 كود التحويل (Sham Cash): <code>{safe_code}</code>\n\n"
        "1️⃣ حوّل المبلغ إلى الكود أعلاه أو عبر مسح QR المرفق.\n"
        "2️⃣ بعد إتمام التحويل، اضغط على الزر أدناه وأرسل صورة الإشعار/الإيصال."
    )

    try:
        await callback.message.answer_photo(
            photo=FSInputFile(settings.qr_image_path),
            caption=caption,
            parse_mode="HTML",
            reply_markup=payment_instructions_keyboard(cv_id),
        )
    except Exception:
        logger.exception("تعذر إرسال صورة QR")
        await callback.message.answer(caption, parse_mode="HTML", reply_markup=payment_instructions_keyboard(cv_id))


@user_router.callback_query(F.data.startswith("upload_receipt:"))
async def handle_upload_receipt_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    cv_id = int(callback.data.split(":")[1])
    await state.update_data(cv_id=cv_id)
    await state.set_state(PaymentStates.waiting_for_receipt)
    await callback.message.answer(
        "📸 يرجى إرسال صورة إيصال أو إشعار التحويل الآن:",
        reply_markup=cancel_action_keyboard(),
    )


@user_router.message(StateFilter(PaymentStates.waiting_for_receipt), F.photo)
async def handle_receipt_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cv_id = data.get("cv_id")

    if not cv_id:
        await state.clear()
        await message.answer(
            "⚠️ عذراً، لم يتم العثور على السيرة الذاتية المرتبطة بهذا الإيصال (انتهت الجلسة).\n"
            "يرجى إعادة اختيار طلب نسخة Word من رسالة السيرة السابقة."
        )
        return

    receipt_file_id = message.photo[-1].file_id
    payment_id = await db.insert_payment(
        user_id=message.from_user.id,
        amount=float(settings.docx_price_usd),
        currency="USD",
        receipt_file_id=receipt_file_id,
        cv_record_id=cv_id,
    )
    await state.clear()

    await message.answer("✅ تم استلام صورة الإيصال بنجاح! جارٍ التحقق ومراجعة الطلب...")

    await enqueue(
        settings.queue_receipt_screening,
        {
            "payment_id": payment_id,
            "chat_id": message.chat.id,
            "user_id": message.from_user.id,
            "receipt_file_id": receipt_file_id,
        },
    )


@user_router.message(StateFilter(PaymentStates.waiting_for_receipt))
async def handle_receipt_wrong_type(message: Message) -> None:
    await message.answer(
        "⚠️ يرجى إرسال الإيصال كـ <b>صورة</b> (Photo) وليس كملف أو نص.",
        reply_markup=cancel_action_keyboard(),
    )


# =====================================================================
# لوحة تحكم الأدمن ومراجعة الدفعات (Admin Router)
# =====================================================================

@admin_router.callback_query(F.data.startswith("approve:"))
async def handle_admin_approve(callback: CallbackQuery) -> None:
    payment_id = int(callback.data.split(":")[1])
    payment = await db.get_payment(payment_id)
    if payment is None:
        await callback.answer("لم يتم العثور على سجل الدفعة", show_alert=True)
        return
    if payment["status"] != "pending":
        await callback.answer("⚠️ تمت معالجة هذه الدفعة مسبقاً!", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    await db.update_payment_status(payment_id, "approved")
    await callback.answer("تم قبول الدفعة بنجاح ✅")

    admin_name = callback.from_user.full_name or f"Admin {callback.from_user.id}"
    try:
        await callback.message.edit_caption(
            caption=f"{callback.message.caption}\n\n✅ <b>تمت الموافقة بواسطة:</b> {html.escape(admin_name)}"
        )
    except Exception:
        pass

    await enqueue(
        settings.queue_docx_generation,
        {
            "payment_id": payment_id,
            "user_id": payment["user_id"],
            "cv_record_id": payment["cv_record_id"],
        },
    )


@admin_router.callback_query(F.data.startswith("reject:"))
async def handle_admin_reject_menu(callback: CallbackQuery) -> None:
    payment_id = int(callback.data.split(":")[1])
    payment = await db.get_payment(payment_id)
    if not payment or payment["status"] != "pending":
        await callback.answer("⚠️ تمت معالجة الدفعة مسبقاً أو غير موجودة", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_reply_markup(
        reply_markup=admin_reject_reasons_keyboard(payment_id)
    )


@admin_router.callback_query(F.data.startswith("rej_cancel:"))
async def handle_admin_reject_cancel(callback: CallbackQuery) -> None:
    payment_id = int(callback.data.split(":")[1])
    await callback.answer("تم التراجع")
    await callback.message.edit_reply_markup(
        reply_markup=admin_review_keyboard(payment_id)
    )


@admin_router.callback_query(F.data.startswith("rej_quick:"))
async def handle_admin_reject_quick(callback: CallbackQuery, bot: Bot) -> None:
    _, payment_id_str, reason_code = callback.data.split(":")
    payment_id = int(payment_id_str)
    
    reasons_map = {
        "unclear": "صورة الإشعار غير واضحة أو غير مكتملة المعالم.",
        "wrong_amount": "المبلغ المحول في الإشعار لا يطابق قيمة الخدمة المطلوبة.",
        "invalid_tx": "رقم عملية التحويل غير صالح أو تم استخدامه مسبقاً.",
    }
    reason = reasons_map.get(reason_code, "تعذر التحقق من صحة الإشعار.")

    payment = await db.get_payment(payment_id)
    if not payment or payment["status"] != "pending":
        await callback.answer("تمت معالجة الدفعة مسبقاً", show_alert=True)
        return

    await db.update_payment_status(payment_id, "rejected", admin_note=reason)
    await callback.answer("تم رفض الدفعة وإخطار العميل ❌")

    admin_name = callback.from_user.full_name or f"Admin {callback.from_user.id}"
    try:
        await callback.message.edit_caption(
            caption=f"{callback.message.caption}\n\n❌ <b>تم الرفض بواسطة:</b> {html.escape(admin_name)}\n📝 <b>السبب:</b> {reason}"
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            chat_id=payment["user_id"],
            text=(
                "❌ <b>نعتذر، لم تتم الموافقة على إيصال الدفع:</b>\n\n"
                f"📝 <b>السبب:</b> {html.escape(reason)}\n\n"
                "يمكنك إعادة المحاولة برفع إيصال صحيح عبر البوت."
            ),
        )
    except Exception:
        logger.exception("تعذر إرسال إشعار الرفض للمستخدم %s", payment["user_id"])


@admin_router.callback_query(F.data.startswith("rej_custom:"))
async def handle_admin_reject_custom_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    payment_id = int(callback.data.split(":")[1])
    await state.update_data(reject_payment_id=payment_id)
    await state.set_state(RejectStates.waiting_for_reason)
    await callback.answer()
    await callback.message.answer(
        f"✍️ أرسل الآن سبب رفض الدفعة #{payment_id} في رسالة واحدة (أو /cancel للتراجع):"
    )


@admin_router.message(StateFilter(RejectStates.waiting_for_reason), F.text, ~F.text.startswith("/"))
async def handle_admin_reject_custom_send(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    payment_id = data.get("reject_payment_id")
    reason = message.text.strip()

    payment = await db.get_payment(payment_id)
    if not payment or payment["status"] != "pending":
        await state.clear()
        await message.answer("⚠️ الدفعة لم تعد معلقة أو تم تعديلها.")
        return

    await db.update_payment_status(payment_id, "rejected", admin_note=reason)
    await state.clear()

    await message.answer(f"❌ تم رفض الدفعة #{payment_id} بنجاح وإشعار العميل.")

    try:
        await bot.send_message(
            chat_id=payment["user_id"],
            text=(
                "❌ <b>لم تتم الموافقة على إيصال الدفع:</b>\n\n"
                f"📝 <b>السبب:</b> {html.escape(reason)}\n\n"
                "يمكنك إعادة المحاولة برفع إيصال جديد."
            ),
        )
    except Exception:
        logger.exception("تعذر إرسال إشعار الرفض للمستخدم %s", payment["user_id"])


# =====================================================================
# أوامر الإحصائيات والبث الجماعي الآمن (Admin Broadcast)
# =====================================================================

@admin_router.message(Command("admin"))
async def cmd_admin_panel(message: Message) -> None:
    await message.answer("🛠 <b>لوحة تحكم إدارة البوت</b>", reply_markup=admin_panel_keyboard())


@admin_router.callback_query(F.data == "admin_stats")
async def handle_admin_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    stats = await db.get_stats()
    text = (
        "📊 <b>إحصائيات البوت:</b>\n\n"
        f"👥 إجمالي المستخدمين: <b>{stats['total_users']}</b>\n"
        f"📄 سير ذاتية PDF مُنشأة: <b>{stats['pdf_generated']}</b>\n"
        f"📝 سير ذاتية Word مُشتراة: <b>{stats['docx_generated']}</b>\n"
        f"⏳ دفعات بانتظار المراجعة: <b>{stats['payments_pending']}</b>\n"
        f"✅ دفعات مقبولة: <b>{stats['payments_approved']}</b>\n"
        f"❌ دفعات مرفوضة: <b>{stats['payments_rejected']}</b>"
    )
    await callback.message.answer(text)


@admin_router.callback_query(F.data == "admin_broadcast")
async def handle_admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BroadcastStates.waiting_for_message)
    await callback.message.answer(
        "✍️ أرسل الآن نص الرسالة التي ترغب في بثها لجميع المستخدمين (أو /cancel للإلغاء):"
    )


@admin_router.message(StateFilter(BroadcastStates.waiting_for_message), F.text, ~F.text.startswith("/"))
async def handle_admin_broadcast_preview(message: Message, state: FSMContext) -> None:
    await state.update_data(broadcast_text=message.text)
    await state.set_state(BroadcastStates.confirm_send)
    
    user_ids = await db.get_all_user_ids()
    await message.answer(
        f"📢 <b>معاينة الرسالة الجماعية:</b>\n\n"
        f"{message.text}\n\n"
        f"⚠️ هل أنت متأكد من رغبتك بإرسال هذه الرسالة إلى <b>{len(user_ids)}</b> مستخدم؟",
        reply_markup=admin_broadcast_confirm_keyboard(),
    )


@admin_router.callback_query(StateFilter(BroadcastStates.confirm_send), F.data == "broadcast:confirm")
async def handle_admin_broadcast_execute(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    data = await state.get_data()
    text = data.get("broadcast_text")
    await state.clear()

    if not text:
        await callback.message.answer("⚠️ لا يوجد نص محفوظ للإرسال.")
        return

    user_ids = await db.get_all_user_ids()
    status_msg = await callback.message.answer(f"📢 بدأ البث الجماعي إلى {len(user_ids)} مستخدم...")

    asyncio.create_task(_run_safe_broadcast(bot, user_ids, text, status_msg))


async def _run_safe_broadcast(bot: Bot, user_ids: list[int], text: str, status_msg: Message) -> None:
    sent, failed = 0, 0
    for idx, uid in enumerate(user_ids):
        try:
            await bot.send_message(chat_id=uid, text=text)
            sent += 1
        except TelegramRetryAfter as e:
            logger.warning("تجاوز حد الإرسال لتيليجرام. انتظار %s ثانية", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(chat_id=uid, text=text)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception:
            failed += 1

        await asyncio.sleep(0.04)
        if (idx + 1) % 50 == 0:
            try:
                await status_msg.edit_text(f"📢 جارٍ البث ({idx + 1}/{len(user_ids)})... نجح: {sent} | فشل: {failed}")
            except Exception:
                pass

    try:
        await status_msg.edit_text(f"✅ اكتمل البث بنجاح!\n\n📨 تم الإرسال: {sent}\n🚫 تعذر الإرسال: {failed}")
    except Exception:
        pass
