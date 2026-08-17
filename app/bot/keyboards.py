"""
لوحات المفاتيح (Inline / Reply Keyboards) المستخدمة في البوت
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings


def cv_style_keyboard() -> InlineKeyboardMarkup:
    """يسأل المستخدم إن كان يفضّل نصه كما كتبه (مع تنظيم بسيط) أو يريد لمسة احترافية من الذكاء الاصطناعي."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖊️ خليها متل ما كتبتها بالضبط", callback_data="cv_style:raw")],
            [InlineKeyboardButton(text="✨ خلّي الذكاء الاصطناعي يلمّعها ويطوّرها", callback_data="cv_style:enhanced")],
        ]
    )


def more_info_choice_keyboard(options: list[str]) -> InlineKeyboardMarkup:
    """يبني أزرار ديناميكية لسؤال استكمال معلومات ذو خيارات محدودة (2-4)، حسب ما يقرره Gemini.
    كل زر ثابت بـ index الخيار داخل قائمة options نفسها (المخزّنة بالـ FSM state) بدل نص الخيار
    الكامل، تجنّباً لتجاوز الحد الأقصى لطول callback_data ولتفادي مشاكل الأحرف الخاصة/العربية."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=option[:64], callback_data=f"cvq:{idx}")]
            for idx, option in enumerate(options[:4])
        ]
    )


def cv_language_keyboard() -> InlineKeyboardMarkup:
    """يسأل المستخدم عن لغة السيرة الذاتية الناتجة قبل إرسالها لـ Gemini."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇸🇦 عربي", callback_data="cv_lang:ar"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="cv_lang:en"),
            ]
        ]
    )


def docx_offer_keyboard(cv_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💳 احصل على نسخة Word قابلة للتعديل ({settings.docx_price_usd}$ / {settings.docx_price_syp} ل.س)",
                    callback_data=f"buy_docx:{cv_id}",
                )
            ],
            [InlineKeyboardButton(text="لا شكراً، تكفيني نسخة PDF", callback_data="skip_docx")],
        ]
    )


def payment_instructions_keyboard(cv_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 لقد أرسلت المبلغ - رفع إيصال الدفع", callback_data=f"upload_receipt:{cv_id}")]
        ]
    )


def admin_review_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ قبول", callback_data=f"approve:{payment_id}"),
                InlineKeyboardButton(text="❌ رفض", callback_data=f"reject:{payment_id}"),
            ]
        ]
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 الإحصائيات", callback_data="admin_stats")],
            [InlineKeyboardButton(text="📢 إرسال رسالة جماعية", callback_data="admin_broadcast")],
        ]
    )
