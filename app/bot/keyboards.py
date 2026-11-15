"""
لوحات المفاتيح (Inline / Reply Keyboards) المستخدمة في بوت CV-MAKER
مصممة وفق معايير Telegram Bot API و aiogram 3.x لتقديم تجربة مستخدم (UI/UX) تفاعلية وسلسة.
"""
from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import settings


# =====================================================================
# 1. القائمة السفلية الدائمة (Persistent ReplyKeyboardMarkup)
# =====================================================================

def persistent_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """
    القائمة السفلية الدائمة (Bottom Navigation Bar)
    تظهر دائماً في أسفل الشاشة وتتيح للمستخدم الوصول السريع لكافة وظائف البوت.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📄 بناء CV جديد"),
                KeyboardButton(text="ℹ️ تعليمات الاستخدام"),
            ],
            [
                KeyboardButton(text="💳 أسعار النسخ"),
                KeyboardButton(text="📞 الدعم الفني"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="اختر من القائمة أو أرسل نبذة عن خبراتك...",
    )


# =====================================================================
# 2. لوحات المفاتيح الشفافة لمراحل بناء السيرة (Navigation Keyboards)
# =====================================================================

def cv_style_keyboard() -> InlineKeyboardMarkup:
    """لوحة اختيار أسلوب الصياغة مع أزرار التنقل والرجوع والإلغاء."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖊️ خليها متل ما كتبتها بالضبط", callback_data="cv_style:raw")],
            [InlineKeyboardButton(text="✨ خلّي الذكاء الاصطناعي يلمّعها", callback_data="cv_style:enhanced")],
            [
                InlineKeyboardButton(text="⬅️ رجوع", callback_data="nav:back_to_text"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="action:cancel"),
            ],
        ]
    )


def cv_language_keyboard() -> InlineKeyboardMarkup:
    """لوحة اختيار لغة السيرة الذاتية مع أزرار الرجوع والإلغاء."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇸🇦 عربي", callback_data="cv_lang:ar"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="cv_lang:en"),
            ],
            [
                InlineKeyboardButton(text="⬅️ رجوع", callback_data="nav:back_to_style"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="action:cancel"),
            ],
        ]
    )


def more_info_choice_keyboard(options: list[str]) -> InlineKeyboardMarkup:
    """خيارات الأسئلة التكميلية متعددة الخيارات مع زر التخطي والإلغاء."""
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=option[:50], callback_data=f"cvq:{idx}")]
        for idx, option in enumerate(options[:4])
    ]
    buttons.append(
        [
            InlineKeyboardButton(text="⏭️ تخطي وانتقل للمعاينة", callback_data="cv_info:skip"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="action:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def more_info_text_keyboard() -> InlineKeyboardMarkup:
    """لوحة الأسئلة التكميلية النصية التي تتيح التخطي أو الإلغاء في أي وقت."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏭️ تخطي وانتقل للمعاينة", callback_data="cv_info:skip"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="action:cancel"),
            ]
        ]
    )


# =====================================================================
# 3. لوحة التحكم التفاعلية لمعاينة السيرة وتعديل الأقسام (Dashboard)
# =====================================================================

def cv_preview_dashboard_keyboard(style: str = "enhanced", lang: str = "ar") -> InlineKeyboardMarkup:
    """
    لوحة تحكم تفاعلية متطورة للمستخدم:
    تمكنه من مراجعة كل قسم وتعديله على حدة، وتغيير اللغة أو الأسلوب وإعادة الصياغة بضغطة زر
    قبل الاعتماد والتوليد النهائي لملف PDF.
    """
    style_label = "✨ النمط: ملمّع (AI)" if style == "enhanced" else "🖊️ النمط: أصلي"
    lang_label = "🌐 اللغة: عربي" if lang == "ar" else "🌐 Language: English"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👤 الاسم والاتصال", callback_data="cv_edit:contact"),
                InlineKeyboardButton(text="💼 المسمى الوظيفي", callback_data="cv_edit:title"),
            ],
            [
                InlineKeyboardButton(text="📝 النبذة الشخصية", callback_data="cv_edit:summary"),
                InlineKeyboardButton(text="🏢 الخبرات العملية", callback_data="cv_edit:experience"),
            ],
            [
                InlineKeyboardButton(text="🎓 المؤهلات والتعليم", callback_data="cv_edit:education"),
                InlineKeyboardButton(text="🛠 المهارات", callback_data="cv_edit:skills"),
            ],
            [
                InlineKeyboardButton(text="🌐 اللغات", callback_data="cv_edit:languages"),
                InlineKeyboardButton(text="🔄 إعادة صياغة ذكية", callback_data="cv_edit:regenerate"),
            ],
            [
                InlineKeyboardButton(text=style_label, callback_data="cv_edit:toggle_style"),
                InlineKeyboardButton(text=lang_label, callback_data="cv_edit:toggle_lang"),
            ],
            [
                InlineKeyboardButton(text="🚀 اعتماد وتوليد الـ PDF المجاني 📄", callback_data="cv_generate:confirm"),
            ],
            [
                InlineKeyboardButton(text="❌ إلغاء وبدء من جديد", callback_data="action:cancel"),
            ],
        ]
    )


def cv_edit_cancel_keyboard() -> InlineKeyboardMarkup:
    """زر للتراجع عن تعديل قسم والعودة للوحة المعاينة الرئيسية."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ إلغاء التعديل والعودة للمعاينة", callback_data="cv_edit:back_to_dashboard")]
        ]
    )


# =====================================================================
# 4. لوحات المفاتيح الخاصة بالمعلومات والأسعار والدعم
# =====================================================================

def pricing_keyboard() -> InlineKeyboardMarkup:
    """لوحة تفاعلية لرسالة الأسعار توفر إجراءات سريعة."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 ابدأ بناء سيرة ذاتية مجاناً", callback_data="action:start_new_cv")],
            [InlineKeyboardButton(text="💬 استفسار عن الدفع", callback_data="action:support_info")],
        ]
    )


def support_keyboard() -> InlineKeyboardMarkup:
    """لوحة خيارات الدعم الفني والمساعدة."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📄 بناء CV جديد الآن", callback_data="action:start_new_cv")],
            [InlineKeyboardButton(text="ℹ️ قراءة التعليمات", callback_data="action:how_it_works")],
        ]
    )


# =====================================================================
# 5. لوحات المفاتيح الخاصة بالدفع ونسخة Word (DOCX)
# =====================================================================

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


# =====================================================================
# 6. لوحات تحكم المشرفين (Admin Panels)
# =====================================================================

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
