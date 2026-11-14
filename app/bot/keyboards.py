"""
لوحات المفاتيح (Inline / Reply Keyboards) المستخدمة في البوت
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings


def cv_style_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖊️ خليها متل ما كتبتها بالضبط", callback_data="cv_style:raw")],
            [InlineKeyboardButton(text="✨ خلّي الذكاء الاصطناعي يلمّعها", callback_data="cv_style:enhanced")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="action:cancel")],
        ]
    )


def more_info_choice_keyboard(options: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=option[:50], callback_data=f"cvq:{idx}")]
        for idx, option in enumerate(options[:4])
    ]
    buttons.append([InlineKeyboardButton(text="❌ إلغاء وبدء من جديد", callback_data="action:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cv_language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇸🇦 عربي", callback_data="cv_lang:ar"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="cv_lang:en"),
            ],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="action:cancel")],
        ]
    )


def docx_offer_keyboard(cv_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💳 شراء نسخة Word ({settings.docx_price_usd}$ / {settings.docx_price_syp} ل.س)",
                    callback_data=f"buy_docx:{cv_id}",
                )
            ],
            [InlineKeyboardButton(text="لا شكراً، تكفيني نسخة PDF 👍", callback_data="skip_docx")],
        ]
    )


def payment_instructions_keyboard(cv_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 رفع إيصال التحويل", callback_data=f"upload_receipt:{cv_id}")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="action:cancel")],
        ]
    )


def cancel_action_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء العملية", callback_data="action:cancel")]
        ]
    )


def admin_review_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ قبول واعتماد", callback_data=f"approve:{payment_id}"),
                InlineKeyboardButton(text="❌ رفض الإيصال", callback_data=f"reject:{payment_id}"),
            ]
        ]
    )


def admin_reject_reasons_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📷 صورة الإيصال غير واضحة", callback_data=f"rej_quick:{payment_id}:unclear")],
            [InlineKeyboardButton(text="💰 المبلغ المحول غير مطابق", callback_data=f"rej_quick:{payment_id}:wrong_amount")],
            [InlineKeyboardButton(text="🔢 رقم العملية مكرر أو غير موجود", callback_data=f"rej_quick:{payment_id}:invalid_tx")],
            [InlineKeyboardButton(text="✍️ كتابة سبب مخصص...", callback_data=f"rej_custom:{payment_id}")],
            [InlineKeyboardButton(text="↩️ تراجع", callback_data=f"rej_cancel:{payment_id}")],
        ]
    )


def admin_broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 نعم، ابدأ البث الآن", callback_data="broadcast:confirm"),
                InlineKeyboardButton(text="❌ تراجع وإلغاء", callback_data="action:cancel"),
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
