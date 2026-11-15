"""
معالجات (Handlers) بوت تيليجرام - aiogram 3.x
نظام شامل لإدارة واجهات المستخدم وتجربة الاستخدام (UI/UX) لبوت CV-MAKER:
- قائمة سفلية دائمة (Persistent ReplyKeyboardMarkup)
- مسار بناء تفاعلي يدعم أزرار التنقل [رجوع / تخطي / إلغاء]
- لوحة تحكم تفاعلية للمعاينة وتعديل أقسام السيرة الذاتية قبل التوليد النهائي
- حماية كاملة ضد حالات السباق (Race Conditions) وتكرار الطلبات
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
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
    cv_edit_cancel_keyboard,
    cv_language_keyboard,
    cv_preview_dashboard_keyboard,
    cv_style_keyboard,
    docx_offer_keyboard,
    more_info_choice_keyboard,
    more_info_text_keyboard,
    payment_instructions_keyboard,
    persistent_main_menu_keyboard,
    pricing_keyboard,
    support_keyboard,
)
from app.config import settings
from app.queue.redis_client import enqueue
from app.services import gemini_service
from app.services.db import db

logger = logging.getLogger(__name__)

user_router = Router(name="user")
admin_router = Router(name="admin")

# تقليل جولات الاستفسار إلى جولتين كحد أقصى لمنع إرهاق المستخدم وتفادي التكرار
MAX_INFO_ROUNDS = 2

MENU_BUTTON_TEXTS = {
    "📄 بناء CV جديد",
    "ℹ️ تعليمات الاستخدام",
    "💳 أسعار النسخ",
    "📞 الدعم الفني",
}


# =====================================================================
# تعريف حالات FSM (Finite State Machine)
# =====================================================================

class CvBuildStates(StatesGroup):
    waiting_for_text = State()        # بانتظار إرسال نص الخبرات الأولي
    waiting_for_style = State()       # اختيار الأسلوب (أصلي / تحسين الذكاء الاصطناعي)
    waiting_for_language = State()    # اختيار اللغة (عربي / English)
    processing = State()              # قفل لمنع Race Condition أثناء تشغيل Gemini
    waiting_for_more_info = State()   # بانتظار الإجابة عن أسئلة الاستكمال
    preview_dashboard = State()       # لوحة التحكم التفاعلية والمعاينة قبل التوليد
    editing_section = State()         # تعديل قسم مخصص (الاسم، المهارات، الخبرات، ...)


class PaymentStates(StatesGroup):
    waiting_for_receipt = State()


class BroadcastStates(StatesGroup):
    waiting_for_message = State()
    confirm_send = State()


class RejectStates(StatesGroup):
    waiting_for_reason = State()


# =====================================================================
# النصوص الثابتة والرسائل الإرشادية
# =====================================================================

WELCOME_TEXT = (
    "👋 <b>مرحباً بك في صانع السير الذاتية الذكي (CV-MAKER)!</b>\n\n"
    "بوتك الاحترافي لتحويل خبراتك ومهاراتك إلى سيرة ذاتية عالمية متوافقة مع أنظمة "
    "<b>ATS</b> بصيغة <b>PDF مجانية تماماً</b>. 📄✨\n\n"
    "🚀 <b>كيف تبدأ؟</b>\n"
    "1️⃣ اضغط على <b>[📄 بناء CV جديد]</b> أو اكتب نبذة عن خبراتك بأسلوبك البسيط (حتى بالعامية).\n"
    "2️⃣ اختر الأسلوب المناسب ولغة السيرة.\n"
    "3️⃣ راجع مسودة سيرتك عبر <b>لوحة التحكم التفاعلية</b> وعدّل ما تشاء قبل التوليد النهائي.\n\n"
    "💡 <i>استخدم القائمة السفلية الدائمة للتنقل السريع في أي وقت.</i>"
)

USAGE_GUIDE_TEXT = (
    "ℹ️ <b>دليل وتعليمات استخدام بوت CV-MAKER:</b>\n\n"
    "🎯 <b>1. طريقة كتابة النبذة الأولية:</b>\n"
    "• لا تقلق بشأن التنسيق؛ اكتب ما تعرفه عن دراستك، عملك، وسنوات الخبرة.\n"
    "• مثال: <i>أنا سارة، خريجة إدارة أعمال 2021، عملت مسؤولة تسويق رقمي لمدة سنتين في شركة تجارية، أجيد إدارة الحملات الإعلانية وصناعة المحتوى، وإتقان تام للإنجليزية...</i>\n\n"
    "✨ <b>2. أسلوب الصياغة (Style):</b>\n"
    "• <b>النمط الملمّع:</b> يقوم الذكاء الاصطناعي بصياغة المهام بأفعال قيادية قوية ومصطلحات ATS.\n"
    "• <b>النمط الأصلي:</b> يحافظ على نصوصك كما كتبتها تماماً دون تغيير في الأسلوب.\n\n"
    "🎛 <b>3. لوحة المعاينة والتعديل:</b>\n"
    "• قبل إنشاء الملف النهائي، ستظهر لك لوحة أزرار تفاعلية تمكنك من تعديل (المهارات، التعليم، الخبرات، النبذة) أو تغيير لغة السيرة بضغطة زر.\n\n"
    "📥 <b>4. النسخ والتحميل:</b>\n"
    "• نسخة <b>PDF</b>: مجانية 100% عالية الجودة وجاهزة للتقديم.\n"
    "• نسخة <b>Word (DOCX)</b>: قابلة للتعديل ببرنامج Word، تتاح برسوم رمزية بسيطة."
)

PRICING_TEXT = (
    "💳 <b>باقات وأسعار السير الذاتية:</b>\n\n"
    "🎁 <b>النسخة المجانية (PDF):</b>\n"
    "• تصميم احترافي متوافق مع ATS.\n"
    "• غير محدودة ومجانية 100% مدى الحياة.\n\n"
    "💼 <b>النسخة القابلة للتعديل (Word / DOCX):</b>\n"
    f"• السعر الرمزي: <b>{settings.docx_price_usd}$</b> (أو <b>{settings.docx_price_syp} ليرة سورية</b>).\n"
    "• تتيح لك التعديل اليدوي المستقبلي على كامل محتوى السيرة.\n"
    "• الدفع متاح عبر محفظة <b>Sham Cash</b> مع فحص فوري للإيصال.\n\n"
    "👇 <i>ابدأ بإنشاء نسختك المجانية الآن، ويمكنك طلب نسخة Word في أي وقت!</i>"
)

SUPPORT_TEXT = (
    "📞 <b>مركز المساعدة والدعم الفني:</b>\n\n"
    "فريقنا جاهز لمساعدتك في حال واجهت أي استفسار بخصوص:\n"
    "• عمليات الدفع وتحويل شام كاش.\n"
    "• اقتراحات تطوير القوالب والتصاميم.\n"
    "• الإبلاغ عن أي مشكلة فنية في إنشاء الملفات.\n\n"
    "📩 للتواصل المباشر مع فريق الدعم: @cv_support_official\n"
    "⏰ أوقات الاستجابة: على مدار الساعة."
)


# =====================================================================
# دوال مساعدة لتنسيق العرض والمعاينة وتحليل التعديلات
# =====================================================================

def _format_cv_preview(cv_data: dict[str, Any], language: str = "ar", style: str = "enhanced") -> str:
    """تنسيق بيانات السيرة الذاتية بشكل HTML آمن وأنيق للوحة المعاينة."""
    full_name = html.escape(str(cv_data.get("full_name") or "غير محدد"))
    title = html.escape(str(cv_data.get("title") or "غير محدد"))

    contact = cv_data.get("contact") or {}
    email = html.escape(str(contact.get("email") or ""))
    phone = html.escape(str(contact.get("phone") or ""))
    loc = html.escape(str(contact.get("location") or ""))
    contact_parts = [p for p in [phone, email, loc] if p]
    contact_str = " | ".join(contact_parts) if contact_parts else "غير متوفر"

    summary = html.escape(str(cv_data.get("summary") or "لا توجد نبذة"))

    exp_lines = []
    for exp in (cv_data.get("experience") or [])[:4]:
        role = html.escape(str(exp.get("role") or ""))
        comp = html.escape(str(exp.get("company") or ""))
        period = html.escape(str(exp.get("period") or ""))
        if role and comp:
            header = f"• <b>{role}</b> في <i>{comp}</i>"
        else:
            header = f"• <b>{role or comp}</b>"
        if period:
            header += f" ({period})"
        exp_lines.append(header)
        for b in (exp.get("bullets") or [])[:3]:
            exp_lines.append(f"   - {html.escape(str(b))}")
    exp_str = "\n".join(exp_lines) if exp_lines else "لا توجد خبرات مسجلة"

    edu_lines = []
    for edu in (cv_data.get("education") or [])[:3]:
        deg = html.escape(str(edu.get("degree") or ""))
        inst = html.escape(str(edu.get("institution") or ""))
        period = html.escape(str(edu.get("period") or ""))
        line = f"• <b>{deg}</b> - <i>{inst}</i>" if (deg and inst) else f"• <b>{deg or inst}</b>"
        if period:
            line += f" ({period})"
        edu_lines.append(line)
    edu_str = "\n".join(edu_lines) if edu_lines else "لا توجد مؤهلات مسجلة"

    skills = cv_data.get("skills") or []
    skills_str = html.escape(", ".join(skills)) if skills else "لا توجد مهارات مسجلة"

    langs = cv_data.get("languages") or []
    langs_str = html.escape(", ".join(langs)) if langs else "لا توجد لغات مسجلة"

    style_desc = "✨ تلميع بالذكاء الاصطناعي" if style == "enhanced" else "🖊️ الصياغة الأصلية"
    lang_desc = "🇸🇦 العربية" if language == "ar" else "🇬🇧 الإنجليزية"

    preview = (
        "📋 <b>لوحة مراجعة ومعاينة السيرة الذاتية</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>الاسم:</b> {full_name}\n"
        f"💼 <b>المسمى الوظيفي:</b> {title}\n"
        f"📞 <b>الاتصال:</b> {contact_str}\n\n"
        f"📝 <b>النبذة المهنية:</b>\n{summary}\n\n"
        f"🏢 <b>الخبرات العملية:</b>\n{exp_str}\n\n"
        f"🎓 <b>التعليم والمؤهلات:</b>\n{edu_str}\n\n"
        f"🛠 <b>المهارات:</b> {skills_str}\n"
        f"🌐 <b>اللغات:</b> {langs_str}\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>النمط:</b> {style_desc} | <b>اللغة:</b> {lang_desc}\n\n"
        "👇 <i>اضغط على أي قسم أدناه لتعديله أو تبديل النمط، ثم اضغط على زر الاعتماد لتوليد ملف الـ PDF:</i>"
    )

    if len(preview) > 3800:
        preview = preview[:3750] + "\n... [تم اختصار العرض للمعاينة]"
    return preview


def _parse_experience_edit(raw_text: str) -> list[dict[str, Any]]:
    """يحلل نص تعديل الخبرات بمرونة (يدعم صيغة المسمى | الشركة | الفترة والأسطر النقطية)."""
    lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if not lines:
        return []

    first = lines[0].lstrip("•-* ")
    bullets = [ln.lstrip("•-* ").strip() for ln in lines[1:] if ln.lstrip("•-* ").strip()]

    if "|" in first:
        parts = [p.strip() for p in first.split("|")]
        role = parts[0] if len(parts) > 0 else ""
        company = parts[1] if len(parts) > 1 else ""
        period = parts[2] if len(parts) > 2 else ""
    elif "—" in first or " - " in first:
        sep = "—" if "—" in first else " - "
        parts = [p.strip() for p in first.split(sep, 1)]
        role = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        m = re.search(r"\(([^)]+)\)\s*$", rest)
        if m:
            period = m.group(1).strip()
            company = rest[: m.start()].strip()
        else:
            company = rest
            period = ""
    else:
        role = first[:100]
        company = ""
        period = ""

    return [{"role": role, "company": company, "period": period, "bullets": bullets}]


def _parse_education_edit(raw_text: str) -> list[dict[str, Any]]:
    """يحلل نص تعديل المؤهلات العلمية بمرونة (يدعم الشهادة | الجامعة | الفترة أو الأسطر المتعددة)."""
    entries: list[dict[str, Any]] = []
    for ln in raw_text.splitlines():
        line = ln.strip().lstrip("•-* ").strip()
        if not line:
            continue
        if "|" in line:
            parts = [p.strip() for p in line.split("|")]
            degree = parts[0] if len(parts) > 0 else ""
            inst = parts[1] if len(parts) > 1 else ""
            period = parts[2] if len(parts) > 2 else ""
        elif "—" in line or " - " in line:
            sep = "—" if "—" in line else " - "
            parts = [p.strip() for p in line.split(sep, 1)]
            degree = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
            m = re.search(r"\(([^)]+)\)\s*$", rest)
            if m:
                period = m.group(1).strip()
                inst = rest[: m.start()].strip()
            else:
                inst = rest
                period = ""
        else:
            degree = line
            inst = ""
            period = ""
        entries.append({"degree": degree, "institution": inst, "period": period})
    return entries


# =====================================================================
# أوامر القائمة السفلية الدائمة والبداية العامة
# =====================================================================

@user_router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        WELCOME_TEXT,
        reply_markup=persistent_main_menu_keyboard(),
    )


@user_router.message(Command("cancel"), StateFilter("*"))
@user_router.callback_query(F.data == "action:cancel", StateFilter("*"))
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext) -> None:
    """إلغاء أي عملية حالية وتصفير الـ FSM تماماً."""
    await state.clear()
    text = "❌ تم إلغاء العملية بنجاح. يمكنك البدء من جديد عبر القائمة السفلية في أي وقت."
    if isinstance(event, CallbackQuery):
        await event.answer("تم الإلغاء")
        try:
            await event.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await event.message.answer(text, reply_markup=persistent_main_menu_keyboard())
    else:
        await event.answer(text, reply_markup=persistent_main_menu_keyboard())


@user_router.message(F.text == "📄 بناء CV جديد", StateFilter("*"))
@user_router.callback_query(F.data == "action:start_new_cv", StateFilter("*"))
async def handle_menu_new_cv(event: Message | CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    prompt = (
        "📄 <b>بدء بناء سيرة ذاتية جديدة:</b>\n\n"
        "أرسل لي نبذة شاملة عن خبراتك، دراستك، ومهاراتك (سواء بالعربية أو الإنجليزية، أو حتى باللهجة العامية).\n\n"
        "<i>مثال: أنا محمد، خريج هندسة برمجيات 2022، أعمل مطور تطبيقات Full-stack باستخدام Flutter وNode.js منذ سنتين...</i>"
    )
    await state.set_state(CvBuildStates.waiting_for_text)
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.answer(prompt, reply_markup=cancel_action_keyboard())
    else:
        await event.answer(prompt, reply_markup=cancel_action_keyboard())


@user_router.message(F.text == "ℹ️ تعليمات الاستخدام", StateFilter("*"))
@user_router.callback_query(F.data == "action:how_it_works", StateFilter("*"))
async def handle_menu_instructions(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.answer(USAGE_GUIDE_TEXT)
    else:
        await event.answer(USAGE_GUIDE_TEXT)


@user_router.message(F.text == "💳 أسعار النسخ", StateFilter("*"))
async def handle_menu_pricing(message: Message) -> None:
    await message.answer(PRICING_TEXT, reply_markup=pricing_keyboard())


@user_router.message(F.text == "📞 الدعم الفني", StateFilter("*"))
@user_router.callback_query(F.data == "action:support_info", StateFilter("*"))
async def handle_menu_support(event: Message | CallbackQuery) -> None:
    if isinstance(event, CallbackQuery):
        await event.answer()
        await event.message.answer(SUPPORT_TEXT, reply_markup=support_keyboard())
    else:
        await event.answer(SUPPORT_TEXT, reply_markup=support_keyboard())


# =====================================================================
# مسار بناء السيرة الذاتية (CV Building Flow) مع أزرار التنقل
# =====================================================================

@user_router.message(
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MENU_BUTTON_TEXTS),
    StateFilter(None, CvBuildStates.waiting_for_text),
)
async def handle_free_text_cv(message: Message, state: FSMContext) -> None:
    if not message.text or len(message.text.strip()) < 15:
        await message.answer(
            "🤔 النص قصير جداً لبناء سيرة ذاتية متكاملة. "
            "أرسل لي وصفاً أوسع لخبراتك ومهاراتك من فضلك (15 حرفاً على الأقل)."
        )
        return

    if len(message.text.strip()) > 5000:
        await message.answer(
            "⚠️ النص المرسل طويل جداً (الحد الأقصى المسموح 5000 حرف).\n"
            "يرجى تلخيص نبذة الخبرات والمهارات لتوليد سيرة ذاتية متناسقة."
        )
        return

    await state.update_data(raw_text=message.text.strip())
    await state.set_state(CvBuildStates.waiting_for_style)

    await message.answer(
        "تمام، وصلتني المعلومات بنجاح 👌\n\n"
        "الآن اختر أسلوب الصياغة المفضل لديك:\n"
        "• <b>النمط الأصلي:</b> كما كتبته أنت تماماً.\n"
        "• <b>تلميع الذكاء الاصطناعي:</b> صياغة احترافية بكلمات مفتاحية قوية لأنظمة ATS.",
        reply_markup=cv_style_keyboard(),
    )


# --- الرجوع إلى خطوة النص الأولي ---
@user_router.callback_query(
    StateFilter(CvBuildStates.waiting_for_style), F.data == "nav:back_to_text"
)
async def handle_back_to_text(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("الرجوع لإدخال النص")
    await state.set_state(CvBuildStates.waiting_for_text)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "✍️ يمكنك الآن إعادة إرسال نص جديد لخبراتك ومؤهلاتك:",
        reply_markup=cancel_action_keyboard(),
    )


# --- اختيار الأسلوب والتنقل للغة ---
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


# --- الرجوع من اختيار اللغة إلى اختيار الأسلوب ---
@user_router.callback_query(
    StateFilter(CvBuildStates.waiting_for_language), F.data == "nav:back_to_style"
)
async def handle_back_to_style(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("الرجوع لاختيار الأسلوب")
    await state.set_state(CvBuildStates.waiting_for_style)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "اختر أسلوب الصياغة المفضل لديك:",
        reply_markup=cv_style_keyboard(),
    )


# --- اختيار اللغة وبدء الفحص والتحليل ---
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
    force_complete: bool = False,
) -> None:
    wait_msg = await message.answer("⏳ لحظة، نقوم بمراجعة وتحليل بياناتك وتنسيق السيرة...")

    async def _keep_typing():
        try:
            while True:
                await bot.send_chat_action(message.chat.id, "typing")
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            pass

    typing_task = asyncio.create_task(_keep_typing())
    is_forced = force_complete or (attempts >= MAX_INFO_ROUNDS)

    try:
        result = await gemini_service.check_and_extract_cv(
            raw_text, language=language, style=style, force_complete=is_forced
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

    if result.get("status") != "complete" and not is_forced:
        question = result.get("question") or {}
        question_text = question.get("text") or result.get("follow_up_message") or (
            "هل يمكنك تزويدنا بمعلومات إضافية عن خبراتك أو مهاراتك؟"
        )
        question_type = question.get("question_type")
        options = [o for o in (question.get("options") or []) if isinstance(o, str) and o.strip()]

        await state.update_data(
            attempts=attempts + 1,
            last_question_text=question_text,
        )
        await state.set_state(CvBuildStates.waiting_for_more_info)

        if question_type == "choice" and 2 <= len(options) <= 4:
            await state.update_data(pending_question_text=question_text, pending_options=options)
            await message.answer(question_text, reply_markup=more_info_choice_keyboard(options))
        else:
            await state.update_data(pending_question_text=question_text, pending_options=None)
            await message.answer(question_text, reply_markup=more_info_text_keyboard())
        return

    result["_language"] = language
    result["_style"] = style
    await state.update_data(cv_data=result, style=style, language=language)
    await state.set_state(CvBuildStates.preview_dashboard)

    preview_text = _format_cv_preview(result, language=language, style=style)
    await message.answer(preview_text, reply_markup=cv_preview_dashboard_keyboard(style=style, lang=language))


# --- زر التخطي والانتقال للمعاينة الفورية ---
@user_router.callback_query(
    StateFilter(CvBuildStates.waiting_for_more_info), F.data == "cv_info:skip"
)
async def handle_skip_more_info(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer("تخطي الأسئلة والانتقال للمعاينة")
    data = await state.get_data()
    raw_text = data.get("raw_text", "")
    language = data.get("language", "ar")
    style = data.get("style", "enhanced")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.set_state(CvBuildStates.processing)
    await _run_completeness_check(
        callback.message, state, raw_text, language, style, attempts=MAX_INFO_ROUNDS, bot=bot, force_complete=True
    )


@user_router.message(StateFilter(CvBuildStates.processing))
async def handle_busy_state(message: Message) -> None:
    await message.answer("⏳ طلبك قيد المعالجة والتحليل حالياً، يرجى الانتظار ثوانٍ معدودة...")


@user_router.message(
    StateFilter(CvBuildStates.waiting_for_more_info),
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MENU_BUTTON_TEXTS),
)
async def handle_more_info_reply(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text and len(message.text.strip()) > 2000:
        await message.answer("⚠️ الإجابة طويلة جداً (الحد الأقصى 2000 حرف). يرجى إرسال إجابة مختصرة ومباشرة.")
        return

    data = await state.get_data()
    raw_text = data.get("raw_text", "")
    language = data.get("language", "ar")
    style = data.get("style", "enhanced")
    attempts = data.get("attempts", 0)
    last_question = data.get("last_question_text")

    # إلحاق الإجابة مصحوبة بسياق السؤال المطروح ليفهم الذكاء الاصطناعي دلالة الردود المقتضبة
    if last_question:
        combined_text = f"{raw_text}\n[سؤال توضيحي: {last_question}]\n[إجابة المستخدم: {message.text}]".strip()
    else:
        combined_text = f"{raw_text}\n{message.text}".strip()

    if len(combined_text) > 6000:
        combined_text = combined_text[:6000]

    await state.update_data(
        raw_text=combined_text,
        last_question_text=None,
        pending_question_text=None,
        pending_options=None,
    )
    await state.set_state(CvBuildStates.processing)

    await _run_completeness_check(message, state, combined_text, language, style, attempts=attempts, bot=bot)


@user_router.callback_query(
    StateFilter(CvBuildStates.waiting_for_more_info), F.data.startswith("cvq:")
)
async def handle_more_info_choice(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    data = await state.get_data()
    options: list[str] = data.get("pending_options") or []
    question_text: str = data.get("pending_question_text") or data.get("last_question_text") or ""

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

    if question_text:
        combined_text = f"{raw_text}\n[سؤال توضيحي: {question_text}]\n[إجابة المستخدم: {selected_option}]".strip()
    else:
        combined_text = f"{raw_text}\n{selected_option}".strip()

    if len(combined_text) > 6000:
        combined_text = combined_text[:6000]

    await state.update_data(
        raw_text=combined_text,
        last_question_text=None,
        pending_question_text=None,
        pending_options=None,
    )
    await state.set_state(CvBuildStates.processing)

    await _run_completeness_check(callback.message, state, combined_text, language, style, attempts=attempts, bot=bot)


# =====================================================================
# لوحة التحكم التفاعلية للمعاينة وتعديل الأقسام (Dashboard & Editing)
# =====================================================================

@user_router.callback_query(
    StateFilter(CvBuildStates.preview_dashboard), F.data == "cv_edit:toggle_style"
)
async def handle_toggle_style(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current_style = data.get("style", "enhanced")
    new_style = "raw" if current_style == "enhanced" else "enhanced"
    language = data.get("language", "ar")
    cv_data = dict(data.get("cv_data") or {})
    cv_data["_style"] = new_style
    cv_data["_language"] = language

    await state.update_data(style=new_style, cv_data=cv_data)
    label = "✨ تم تفعيل نمط التلميع (اضغط 'إعادة صياغة ذكية' لتطبيقه على النص)" if new_style == "enhanced" else "🖊️ تم تفعيل النمط الأصلي"
    await callback.answer(label, show_alert=False)

    preview_text = _format_cv_preview(cv_data, language=language, style=new_style)
    try:
        await callback.message.edit_text(
            text=preview_text,
            reply_markup=cv_preview_dashboard_keyboard(style=new_style, lang=language),
        )
    except TelegramBadRequest:
        pass


@user_router.callback_query(
    StateFilter(CvBuildStates.preview_dashboard), F.data == "cv_edit:toggle_lang"
)
async def handle_toggle_language(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current_lang = data.get("language", "ar")
    new_lang = "en" if current_lang == "ar" else "ar"
    style = data.get("style", "enhanced")
    cv_data = dict(data.get("cv_data") or {})
    cv_data["_language"] = new_lang
    cv_data["_style"] = style

    await state.update_data(language=new_lang, cv_data=cv_data)
    label = (
        "🌐 تم تحويل اللغة إلى English (اضغط 'إعادة صياغة ذكية' لترجمة المحتوى)"
        if new_lang == "en"
        else "🌐 تم تحويل اللغة إلى العربية (اضغط 'إعادة صياغة ذكية' لترجمة المحتوى)"
    )
    await callback.answer(label, show_alert=False)

    preview_text = _format_cv_preview(cv_data, language=new_lang, style=style)
    try:
        await callback.message.edit_text(
            text=preview_text,
            reply_markup=cv_preview_dashboard_keyboard(style=style, lang=new_lang),
        )
    except TelegramBadRequest:
        pass


@user_router.callback_query(
    StateFilter(CvBuildStates.preview_dashboard), F.data == "cv_edit:regenerate"
)
async def handle_regenerate_cv_from_dashboard(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """إعادة توليد وصياغة السيرة الذاتية عبر Gemini باللغة والأسلوب المحددين حالياً في لوحة المعاينة."""
    await callback.answer("جارٍ إعادة الصياغة بالذكاء الاصطناعي...")
    data = await state.get_data()
    raw_text = data.get("raw_text", "")
    language = data.get("language", "ar")
    style = data.get("style", "enhanced")

    if not raw_text:
        cv_data = data.get("cv_data") or {}
        raw_text = json.dumps(cv_data, ensure_ascii=False) if cv_data else ""

    if not raw_text:
        await callback.message.answer("⚠️ لا توجد بيانات كافية لإعادة الصياغة، يرجى البدء من جديد.")
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.set_state(CvBuildStates.processing)
    await _run_completeness_check(
        callback.message,
        state,
        raw_text=raw_text,
        language=language,
        style=style,
        attempts=MAX_INFO_ROUNDS,
        bot=bot,
        force_complete=True,
    )


@user_router.callback_query(
    StateFilter(CvBuildStates.preview_dashboard), F.data.startswith("cv_edit:")
)
async def handle_start_edit_section(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    section = callback.data.split(":")[1]

    section_prompts = {
        "contact": (
            "👤 <b>تعديل الاسم ومعلومات الاتصال:</b>\n\n"
            "أرسل اسمك ورقم هاتفك والبريد الإلكتروني والمدينة في رسالة واحدة.\n"
            "<i>مثال: أحمد المحمود | +963912345678 | ahmed@mail.com | دمشق</i>"
        ),
        "title": (
            "💼 <b>تعديل المسمى الوظيفي (Job Title):</b>\n\n"
            "أرسل المسمى الوظيفي الجديد الذي ترغب بظهوره تحت اسمك مباشرة:\n"
            "<i>مثال: مهندس برمجيات أول | Senior Backend Developer</i>"
        ),
        "summary": (
            "📝 <b>تعديل النبذة الشخصية (Summary):</b>\n\n"
            "أرسل النص الجديد للنبذة المهنية التي ترغب بظهورها في أعلى سيرتك الذاتية:"
        ),
        "experience": (
            "🏢 <b>تعديل الخبرات العملية:</b>\n\n"
            "أرسل المسمى الوظيفي والشركة والفترة في السطر الأول، ثم المهام في الأسطر التالية:\n"
            "<i>مثال:\nمحاسب أول | شركة الشام | 2021 - 2024\n- إعداد القوائم المالية والتقارير الضريبية\n- إدارة فريق المحاسبة</i>"
        ),
        "education": (
            "🎓 <b>تعديل التعليم والمؤهلات:</b>\n\n"
            "أرسل مؤهلاتك العلمية بالصيغة (الشهادة | الجامعة أو المعهد | الفترة):\n"
            "<i>مثال: إجازة في الهندسة المعلوماتية | جامعة حلب | 2019 - 2024</i>"
        ),
        "skills": (
            "🛠 <b>تعديل المهارات:</b>\n\n"
            "أرسل المهارات الجديدة مفصولة بفواصل أو في أسطر مستقلة.\n"
            "<i>مثال: Python, FastAPI, Docker, SQL, حل المشكلات, إدارة الوقت</i>"
        ),
        "languages": (
            "🌐 <b>تعديل اللغات:</b>\n\n"
            "أرسل اللغات ومستوى إتقانك لها.\n"
            "<i>مثال: العربية (اللغة الأم)، الإنجليزية (متقدم C1)</i>"
        ),
    }

    prompt = section_prompts.get(section, "أرسل التعديلات المطلوبة:")
    await state.update_data(editing_section=section, editing_section_name=section)
    await state.set_state(CvBuildStates.editing_section)

    await callback.message.answer(prompt, reply_markup=cv_edit_cancel_keyboard())


@user_router.callback_query(
    StateFilter(CvBuildStates.editing_section), F.data == "cv_edit:back_to_dashboard"
)
async def handle_cancel_editing_section(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("تم إلغاء التعديل")
    data = await state.get_data()
    cv_data = data.get("cv_data") or {}
    style = data.get("style", "enhanced")
    language = data.get("language", "ar")

    await state.set_state(CvBuildStates.preview_dashboard)
    preview_text = _format_cv_preview(cv_data, language=language, style=style)
    await callback.message.answer(
        preview_text,
        reply_markup=cv_preview_dashboard_keyboard(style=style, lang=language),
    )


@user_router.message(
    StateFilter(CvBuildStates.editing_section),
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MENU_BUTTON_TEXTS),
)
async def handle_save_edited_section(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    section = data.get("editing_section_name") or data.get("editing_section")
    cv_data = dict(data.get("cv_data") or {})
    style = data.get("style", "enhanced")
    language = data.get("language", "ar")
    new_text = message.text.strip()

    if section == "summary":
        cv_data["summary"] = new_text
    elif section in ("title", "job_title"):
        cv_data["title"] = new_text
    elif section == "skills":
        items = [s.strip() for s in re.split(r"[,،\n]+", new_text) if s.strip()]
        cv_data["skills"] = items
    elif section == "languages":
        items = [s.strip() for s in re.split(r"[,،\n]+", new_text) if s.strip()]
        cv_data["languages"] = items
    elif section == "contact":
        parts = [p.strip() for p in new_text.split("|") if p.strip()]
        if len(parts) >= 2:
            cv_data["full_name"] = parts[0]
            contact = cv_data.setdefault("contact", {})
            for p in parts[1:]:
                if "@" in p:
                    contact["email"] = p
                elif any(c.isdigit() for c in p):
                    contact["phone"] = p
                else:
                    contact["location"] = p
        else:
            contact = cv_data.setdefault("contact", {})
            if "@" in new_text:
                contact["email"] = new_text
            elif any(c.isdigit() for c in new_text):
                contact["phone"] = new_text
            else:
                cv_data["full_name"] = new_text
    elif section == "experience":
        cv_data["experience"] = _parse_experience_edit(new_text)
    elif section == "education":
        cv_data["education"] = _parse_education_edit(new_text)

    sanitized_cv = gemini_service._sanitize_cv_result(cv_data)
    sanitized_cv["_language"] = language
    sanitized_cv["_style"] = style
    await state.update_data(cv_data=sanitized_cv)
    await state.set_state(CvBuildStates.preview_dashboard)

    await message.answer("✅ <b>تم تحديث القسم بنجاح!</b>")
    preview_text = _format_cv_preview(sanitized_cv, language=language, style=style)
    await message.answer(
        preview_text,
        reply_markup=cv_preview_dashboard_keyboard(style=style, lang=language),
    )


# --- الاعتماد النهائي وبدء التوليد ---
@user_router.callback_query(
    StateFilter(CvBuildStates.preview_dashboard), F.data == "cv_generate:confirm"
)
async def handle_confirm_cv_generation(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("تم اعتماد السيرة بنجاح!")
    data = await state.get_data()
    cv_data = data.get("cv_data")

    if not cv_data:
        await state.clear()
        await callback.message.answer("⚠️ حدث خطأ أو انتهت صلاحية الجلسة، يرجى البدء من جديد.")
        return

    cv_data = dict(cv_data)
    cv_data["_language"] = data.get("language") or cv_data.get("_language") or "ar"
    cv_data["_style"] = data.get("style") or cv_data.get("_style") or "enhanced"

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.clear()
    await callback.message.answer(
        "🚀 <b>تم اعتماد بيانات السيرة الذاتية بنجاح!</b>\n\n"
        "⏳ جاري الآن تصميم وبناء ملف الـ PDF عالي الدقة، سيصلك الملف هنا خلال ثوانٍ معدودة..."
    )

    await enqueue(
        settings.queue_cv_generation,
        {
            "chat_id": callback.message.chat.id,
            "user_id": callback.from_user.id if callback.from_user else callback.message.chat.id,
            "cv_data": cv_data,
        },
    )


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
    await callback.message.answer(
        "بالتوفيق لك في مسيرتك المهنية! 🍀 يسعدنا دائماً خدمتك.",
        reply_markup=persistent_main_menu_keyboard(),
    )


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
        "📸 يرجى إرسال صورة إيصال التحويل أو ملف <b>PDF</b> الآن:",
        reply_markup=cancel_action_keyboard(),
    )


@user_router.message(
    StateFilter(PaymentStates.waiting_for_receipt),
    F.photo | F.document,
)
async def handle_receipt_file(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    cv_id = data.get("cv_id")

    if not cv_id:
        await state.clear()
        await message.answer(
            "⚠️ عذراً، لم يتم العثور على السيرة الذاتية المرتبطة بهذا الإيصال (انتهت الجلسة).\n"
            "يرجى إعادة اختيار طلب نسخة Word من رسالة السيرة السابقة."
        )
        return

    is_pdf = False
    if message.photo:
        receipt_file_id = message.photo[-1].file_id
    elif message.document:
        doc = message.document
        mime = (doc.mime_type or "").lower()
        file_name = (doc.file_name or "").lower()
        if mime == "application/pdf" or file_name.endswith(".pdf"):
            is_pdf = True
            receipt_file_id = doc.file_id
        elif mime.startswith("image/") or file_name.endswith((".jpg", ".jpeg", ".png", ".webp")):
            receipt_file_id = doc.file_id
        else:
            await message.answer(
                "⚠️ نوع الملف غير مدعوم. يرجى إرسال الإيصال كـ <b>صورة</b> (JPG / PNG) أو <b>ملف PDF</b> صالح.",
                reply_markup=cancel_action_keyboard(),
            )
            return
    else:
        await message.answer(
            "⚠️ يرجى إرسال الإيصال كـ صورة أو ملف PDF.",
            reply_markup=cancel_action_keyboard(),
        )
        return

    try:
        if await db.has_pending_payment(message.from_user.id):
            await message.answer(
                "⚠️ لديك طلب دفع سابق قيد المراجعة حالياً من قبل الإدارة.\n"
                "يرجى انتظار قرار المراجعة قبل إرسال طلب جديد."
            )
            return
    except Exception:
        logger.exception("فشل فحص الدفعات المعلقة للمستخدم %s", message.from_user.id)

    try:
        if await db.is_receipt_reused(receipt_file_id):
            await message.answer(
                "❌ نعتذر، هذا الإيصال تم استخدامه مسبقاً في عملية دفع سابقة.\n"
                "يرجى إرسال إيصال جديد وصحيح."
            )
            return
    except Exception:
        logger.exception("فشل فحص تكرار الإيصال للمستخدم %s", message.from_user.id)

    payment_id = await db.insert_payment(
        user_id=message.from_user.id,
        amount=float(settings.docx_price_usd),
        currency="USD",
        receipt_file_id=receipt_file_id,
        cv_record_id=cv_id,
    )
    await state.clear()

    await message.answer("✅ تم استلام الإيصال بنجاح! جارٍ التحقق ومراجعة الطلب...")

    await enqueue(
        settings.queue_receipt_screening,
        {
            "payment_id": payment_id,
            "chat_id": message.chat.id,
            "user_id": message.from_user.id,
            "receipt_file_id": receipt_file_id,
            "is_pdf": is_pdf,
        },
    )


@user_router.message(StateFilter(PaymentStates.waiting_for_receipt))
async def handle_receipt_wrong_type(message: Message) -> None:
    await message.answer(
        "⚠️ يرجى إرسال الإيصال كـ <b>صورة</b> (JPG / PNG) أو <b>ملف PDF</b> حصراً.",
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
