"""
معالجات (Handlers) بوت تيليجرام - aiogram 3.x
يغطي:
  أ. بناء سيرة ذاتية مجانية بصيغة PDF من نص حر
  ب. شراء نسخة DOCX عبر Sham Cash
  ج. فحص ومعالجة إيصالات الدفع من قبل الأدمن
  د. لوحة تحكم الأدمن /admin
"""
from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    Message,
)

from app.bot.keyboards import (
    admin_panel_keyboard,
    admin_review_keyboard,
    cv_language_keyboard,
    cv_style_keyboard,
    docx_offer_keyboard,
    payment_instructions_keyboard,
)
from app.config import settings
from app.queue.redis_client import enqueue
from app.services import gemini_service
from app.services.db import db

logger = logging.getLogger(__name__)

user_router = Router(name="user")
admin_router = Router(name="admin")

# أقصى عدد جولات لطلب معلومات إضافية من المستخدم قبل المتابعة إجبارياً بالمتوفر لدينا
MAX_INFO_ROUNDS = 4


class CvBuildStates(StatesGroup):
    waiting_for_style = State()
    waiting_for_language = State()
    waiting_for_more_info = State()


class PaymentStates(StatesGroup):
    waiting_for_receipt = State()


class BroadcastStates(StatesGroup):
    waiting_for_message = State()


class RejectStates(StatesGroup):
    waiting_for_reason = State()


# =====================================================================
# أ. البداية وبناء السيرة الذاتية المجانية
# =====================================================================

WELCOME_TEXT = (
    "👋 أهلاً فيك بوت إنشاء السير الذاتية الاحترافية!\n\n"
    "فقط أرسل لي نبذة عن خبراتك ودراستك ومهاراتك بأسلوبك الخاص "
    "(حتى لو باللهجة العامية، وسواء كتبت بالعربية أو الإنجليزية).\n\n"
    "بعدها رح أسألك إذا حابب سيرتك تطلع متل ما كتبتها بالضبط، ولا تحب الذكاء الاصطناعي "
    "يلمّعها ويحسّن صياغتها، وبعدها عن لغة السيرة الذاتية الناتجة (عربي / English). "
    "وإذا لاحظت إنو في معلومة أساسية ناقصة (متل الاسم أو المهارات)، رح إسألك عنها "
    "قبل ما نكمل، حتى تطلع سيرتك متكاملة واحترافية 📄✨\n\n"
    "مثال: \"أنا محمد، عندي خبرة 3 سنين مبيعات بشركة اتصالات، بعرف "
    "إدارة فريق واستخدام برامج CRM، تخرجت من كلية إدارة أعمال 2020\""
)


@user_router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME_TEXT)


@user_router.message(F.text, ~F.text.startswith("/"), StateFilter(None))
async def handle_free_text_cv(message: Message, state: FSMContext) -> None:
    if not message.text or len(message.text.strip()) < 15:
        await message.answer(
            "🤔 النص قصير جداً لبناء سيرة ذاتية متكاملة. "
            "أرسل لي وصفاً أوسع لخبراتك ومهاراتك من فضلك."
        )
        return

    # نخزّن النص المُرسل مؤقتاً وننتظر اختيار المستخدم لأسلوب الصياغة
    await state.update_data(raw_text=message.text)
    await state.set_state(CvBuildStates.waiting_for_style)

    await message.answer(
        "تمام، وصلتني المعلومات 👌\n\n"
        "قبل ما نكمل، حابب سيرتك الذاتية تطلع بالضبط متل ما كتبتها (بس منظّمة ومرتبة)، "
        "ولا تحب الذكاء الاصطناعي يضيف عليها لمسة احترافية ويحسّن الصياغة؟",
        reply_markup=cv_style_keyboard(),
    )


@user_router.callback_query(
    StateFilter(CvBuildStates.waiting_for_style), F.data.startswith("cv_style:")
)
async def handle_cv_style_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    style = callback.data.split(":")[1]  # "raw" أو "enhanced"

    await state.update_data(style=style)
    await state.set_state(CvBuildStates.waiting_for_language)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "🌐 باي لغة تحب تكون سيرتك الذاتية؟",
        reply_markup=cv_language_keyboard(),
    )


@user_router.message(StateFilter(CvBuildStates.waiting_for_style))
async def handle_style_choice_fallback(message: Message) -> None:
    await message.answer("⬆️ من فضلك اختر أحد الخيارين بالضغط على الأزرار أعلاه.")


@user_router.callback_query(
    StateFilter(CvBuildStates.waiting_for_language), F.data.startswith("cv_lang:")
)
async def handle_cv_language_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    language = callback.data.split(":")[1]  # "ar" أو "en"

    data = await state.get_data()
    raw_text = data.get("raw_text")
    style = data.get("style", "enhanced")

    if not raw_text:
        await state.clear()
        await callback.message.answer("⚠️ انتهت صلاحية الجلسة، من فضلك أعد إرسال وصف خبراتك من جديد.")
        return

    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data(language=language, attempts=0)
    await _run_completeness_check(callback.message, state, raw_text, language, style, attempts=0)


async def _run_completeness_check(
    message: Message,
    state: FSMContext,
    raw_text: str,
    language: str,
    style: str,
    attempts: int,
) -> None:
    """يستدعي Gemini للحكم على اكتمال المعلومات: إمّا يطلب من المستخدم إكمالها بلهجة ودّية،
    أو يعتبرها كافية ويرسل الطلب لطابور توليد الـ PDF."""
    await message.answer("⏳ لحظة، عم راجع المعلومات...")

    force_complete = attempts >= MAX_INFO_ROUNDS
    try:
        result = await gemini_service.check_and_extract_cv(
            raw_text, language=language, style=style, force_complete=force_complete
        )
    except Exception:
        logger.exception("فشل فحص/استخراج بيانات السيرة الذاتية عبر Gemini")
        await state.clear()
        await message.answer("⚠️ صار في خطأ أثناء تحليل بياناتك. من فضلك حاول مرة أخرى بعد قليل.")
        return

    if result.get("status") != "complete":
        follow_up = result.get("follow_up_message") or (
            "ممكن ترسللي كم معلومة إضافية عن خبراتك أو دراستك ومهاراتك؟"
        )
        await state.update_data(attempts=attempts + 1)
        await state.set_state(CvBuildStates.waiting_for_more_info)
        await message.answer(follow_up)
        return

    # اكتملت المعلومات: ننتقل لتوليد ملف PDF عبر عامل الخلفية
    await state.clear()
    await message.answer("تمام كتير، وصلتني كل المعلومات اللازمة ✅ جارٍ بناء سيرتك الذاتية، لحظات من فضلك...")

    await enqueue(
        settings.queue_cv_generation,
        {
            "chat_id": message.chat.id,
            "user_id": message.from_user.id if message.from_user else message.chat.id,
            "cv_data": result,
        },
    )


@user_router.message(StateFilter(CvBuildStates.waiting_for_more_info), F.text)
async def handle_more_info_reply(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    raw_text = data.get("raw_text", "")
    language = data.get("language", "ar")
    style = data.get("style", "enhanced")
    attempts = data.get("attempts", 0)

    combined_text = f"{raw_text}\n{message.text}".strip()
    await state.update_data(raw_text=combined_text)

    await _run_completeness_check(message, state, combined_text, language, style, attempts=attempts)


@user_router.message(StateFilter(CvBuildStates.waiting_for_more_info))
async def handle_more_info_wrong_type(message: Message) -> None:
    await message.answer("✍️ من فضلك أرسل المعلومات الناقصة كنص.")


# =====================================================================
# ب. عرض شراء نسخة DOCX
# =====================================================================

@user_router.message(StateFilter(CvBuildStates.waiting_for_language))
async def handle_language_choice_fallback(message: Message) -> None:
    await message.answer("⬆️ من فضلك اختر لغة السيرة الذاتية بالضغط على أحد الزرين أعلاه.")


@user_router.callback_query(F.data == "skip_docx")
async def handle_skip_docx(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("تمام! نتمنى لك التوفيق في التقديم على الوظائف 🍀")


@user_router.callback_query(F.data.startswith("buy_docx:"))
async def handle_buy_docx(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    cv_id = int(callback.data.split(":")[1])
    await state.update_data(cv_id=cv_id)

    caption = (
        f"💳 لإتمام عملية الشراء ({settings.docx_price_usd}$ / {settings.docx_price_syp} ل.س) عبر Sham Cash:\n\n"
        f"🔢 كود التحويل: `{settings.sham_cash_code}`\n\n"
        "1️⃣ افتح تطبيق Sham Cash\n"
        "2️⃣ حوّل المبلغ إلى الكود أعلاه أو امسح رمز QR المرفق\n"
        "3️⃣ اضغط الزر أدناه بعد إتمام التحويل وأرسل صورة الإيصال"
    )

    try:
        await callback.message.answer_photo(
            photo=FSInputFile(settings.qr_image_path),
            caption=caption,
            parse_mode="Markdown",
            reply_markup=payment_instructions_keyboard(cv_id),
        )
    except Exception:
        logger.exception("تعذر إرسال صورة QR")
        await callback.message.answer(caption, parse_mode="Markdown", reply_markup=payment_instructions_keyboard(cv_id))


@user_router.callback_query(F.data.startswith("upload_receipt:"))
async def handle_upload_receipt_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    cv_id = int(callback.data.split(":")[1])
    await state.update_data(cv_id=cv_id)
    await state.set_state(PaymentStates.waiting_for_receipt)
    await callback.message.answer("📸 من فضلك أرسل الآن صورة إيصال التحويل.")


@user_router.message(StateFilter(PaymentStates.waiting_for_receipt), F.photo)
async def handle_receipt_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cv_id = data.get("cv_id")

    receipt_file_id = message.photo[-1].file_id
    payment_id = await db.insert_payment(
        user_id=message.from_user.id,
        amount=float(settings.docx_price_usd),
        currency="USD",
        receipt_file_id=receipt_file_id,
        cv_record_id=cv_id,
    )
    await state.clear()

    await message.answer("✅ تم استلام الإيصال، جارٍ التحقق منه... سنُعلمك فور المراجعة.")

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
    await message.answer("⚠️ من فضلك أرسل صورة الإيصال (وليس نصاً أو ملفاً آخر).")


# =====================================================================
# ج. تفاعل الأدمن مع طلبات الدفع (قبول / رفض)
# =====================================================================

@admin_router.callback_query(F.data.startswith("approve:"))
async def handle_admin_approve(callback: CallbackQuery) -> None:
    payment_id = int(callback.data.split(":")[1])
    payment = await db.get_payment(payment_id)
    if payment is None:
        await callback.answer("لم يتم العثور على الدفعة", show_alert=True)
        return
    if payment["status"] != "pending":
        await callback.answer("تمت معالجة هذه الدفعة مسبقاً", show_alert=True)
        return

    await db.update_payment_status(payment_id, "approved")
    await callback.answer("تم القبول ✅")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ تمت الموافقة على الدفعة #{payment_id} - جارٍ توليد ملف Word للمستخدم.")

    await enqueue(
        settings.queue_docx_generation,
        {
            "payment_id": payment_id,
            "user_id": payment["user_id"],
            "cv_record_id": payment["cv_record_id"],
        },
    )


@admin_router.callback_query(F.data.startswith("reject:"))
async def handle_admin_reject_start(callback: CallbackQuery, state: FSMContext) -> None:
    payment_id = int(callback.data.split(":")[1])
    payment = await db.get_payment(payment_id)
    if payment is None:
        await callback.answer("لم يتم العثور على الدفعة", show_alert=True)
        return
    if payment["status"] != "pending":
        await callback.answer("تمت معالجة هذه الدفعة مسبقاً", show_alert=True)
        return

    await state.update_data(reject_payment_id=payment_id)
    await state.set_state(RejectStates.waiting_for_reason)
    await callback.answer()
    await callback.message.answer(
        f"✍️ من فضلك أرسل سبب رفض الدفعة #{payment_id} (سيُرسل هذا النص كما هو للمستخدم)."
    )


@admin_router.message(StateFilter(RejectStates.waiting_for_reason), F.text)
async def handle_admin_reject_reason(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    payment_id = data.get("reject_payment_id")
    reason = message.text.strip()

    payment = await db.get_payment(payment_id)
    await db.update_payment_status(payment_id, "rejected", admin_note=reason)
    await state.clear()

    await message.answer(f"❌ تم رفض الدفعة #{payment_id} وإبلاغ المستخدم بالسبب.")

    from app.main import bot  # استيراد مؤجل لتفادي الاستيراد الدائري

    try:
        await bot.send_message(
            chat_id=payment["user_id"],
            text=(
                "❌ لم تتم الموافقة على إيصال الدفع الخاص بك.\n\n"
                f"📝 السبب: {reason}\n\n"
                "يمكنك إعادة المحاولة برفع إيصال صحيح."
            ),
        )
    except Exception:
        logger.exception("تعذر إرسال إشعار الرفض للمستخدم %s", payment["user_id"])


# =====================================================================
# د. لوحة تحكم الأدمن
# =====================================================================

@admin_router.message(Command("admin"))
async def cmd_admin_panel(message: Message) -> None:
    await message.answer("🛠 لوحة تحكم الأدمن", reply_markup=admin_panel_keyboard())


@admin_router.callback_query(F.data == "admin_stats")
async def handle_admin_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    stats = await db.get_stats()
    text = (
        "📊 *إحصائيات البوت*\n\n"
        f"👥 إجمالي المستخدمين: {stats['total_users']}\n"
        f"📄 سير ذاتية PDF: {stats['pdf_generated']}\n"
        f"📝 سير ذاتية DOCX: {stats['docx_generated']}\n"
        f"⏳ دفعات قيد الانتظار: {stats['payments_pending']}\n"
        f"✅ دفعات مقبولة: {stats['payments_approved']}\n"
        f"❌ دفعات مرفوضة: {stats['payments_rejected']}\n"
    )
    await callback.message.answer(text, parse_mode="Markdown")


@admin_router.callback_query(F.data == "admin_broadcast")
async def handle_admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(BroadcastStates.waiting_for_message)
    await callback.message.answer("✍️ أرسل الآن نص الرسالة الجماعية التي تريد بثّها لجميع المستخدمين.")


@admin_router.message(StateFilter(BroadcastStates.waiting_for_message), F.text)
async def handle_admin_broadcast_send(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = message.text
    user_ids = await db.get_all_user_ids()

    status_msg = await message.answer(f"📢 جارٍ الإرسال إلى {len(user_ids)} مستخدم...")

    from app.main import bot  # استيراد مؤجل لتفادي الاستيراد الدائري
    import asyncio

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # تجنّب تجاوز حدود Telegram Flood Control

    await status_msg.edit_text(f"✅ اكتمل البث: نجح {sent} - فشل {failed}")
