"""
لوحات المفاتيح (Inline / Reply Keyboards) المستخدمة في البوت
"""
from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings


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
