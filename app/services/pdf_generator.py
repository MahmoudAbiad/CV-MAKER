"""
مولّد PDF خفيف باستخدام reportlab فقط (بدون متصفحات ثقيلة)
يُنتج سيرة ذاتية بتنسيق ATS بسيط وواضح
"""
from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ملاحظة: للحصول على عرض عربي سليم للحروف المتصلة داخل PDF، يُنصح بتضمين خط
# TrueType يدعم العربية (مثل Amiri أو Cairo) وتسجيله عبر reportlab.pdfbase.ttfonts.
# هنا نفترض توفر الخط في ./assets/fonts/Cairo-Regular.ttf ونسجله إن وجد.

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_ARABIC_FONT_NAME = "Cairo"
_ARABIC_FONT_PATH = "./assets/fonts/Cairo-Regular.ttf"
_font_registered = False


def _ensure_font_registered() -> str:
    global _font_registered
    if _font_registered:
        return _ARABIC_FONT_NAME
    try:
        pdfmetrics.registerFont(TTFont(_ARABIC_FONT_NAME, _ARABIC_FONT_PATH))
        _font_registered = True
        return _ARABIC_FONT_NAME
    except Exception:
        # في حال عدم توفر الخط، نعود للخط الافتراضي (لن يدعم العربية بشكل مثالي)
        return "Helvetica"


def _reshape(text: str) -> str:
    """يعيد تشكيل النص العربي واتجاهه ليظهر بشكل صحيح داخل reportlab."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text


def generate_cv_pdf(cv_data: dict[str, Any]) -> bytes:
    """يولّد ملف PDF بالكامل في الذاكرة (BytesIO) دون كتابة ملفات مؤقتة على القرص."""
    font_name = _ensure_font_registered()
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )

    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "ArabicBase", parent=styles["Normal"], fontName=font_name,
        fontSize=10.5, leading=15, alignment=TA_RIGHT,
    )
    name_style = ParagraphStyle(
        "ArabicName", parent=base, fontSize=20, leading=24,
        textColor=colors.HexColor("#123A5E"), spaceAfter=2,
    )
    title_style = ParagraphStyle(
        "ArabicTitle", parent=base, fontSize=13,
        textColor=colors.HexColor("#3B6EA5"), spaceAfter=10,
    )
    section_style = ParagraphStyle(
        "ArabicSection", parent=base, fontSize=13,
        textColor=colors.white, backColor=colors.HexColor("#123A5E"),
        spaceBefore=10, spaceAfter=6, borderPadding=(4, 6, 4, 6),
    )
    bullet_style = ParagraphStyle("ArabicBullet", parent=base, fontSize=10, leftIndent=0, spaceAfter=2)

    story: list[Any] = []

    story.append(Paragraph(_reshape(cv_data.get("full_name", "")), name_style))
    if cv_data.get("title"):
        story.append(Paragraph(_reshape(cv_data["title"]), title_style))

    contact = cv_data.get("contact") or {}
    contact_line = "  |  ".join(
        filter(None, [contact.get("phone"), contact.get("email"), contact.get("location")])
    )
    if contact_line:
        story.append(Paragraph(_reshape(contact_line), base))
    story.append(Spacer(1, 8))

    if cv_data.get("summary"):
        story.append(Paragraph(_reshape("نبذة مهنية"), section_style))
        story.append(Paragraph(_reshape(cv_data["summary"]), base))

    experience = cv_data.get("experience") or []
    if experience:
        story.append(Paragraph(_reshape("الخبرات العملية"), section_style))
        for exp in experience:
            header = f"{exp.get('role', '')} — {exp.get('company', '')} ({exp.get('period', '')})"
            story.append(Paragraph(_reshape(header), ParagraphStyle(
                "ExpHeader", parent=base, fontSize=11.5, textColor=colors.HexColor("#123A5E"), spaceBefore=6,
            )))
            for bullet in exp.get("bullets", []):
                story.append(Paragraph(_reshape(f"•  {bullet}"), bullet_style))

    education = cv_data.get("education") or []
    if education:
        story.append(Paragraph(_reshape("التعليم"), section_style))
        for edu in education:
            line = f"{edu.get('degree', '')} — {edu.get('institution', '')} ({edu.get('period', '')})"
            story.append(Paragraph(_reshape(line), base))

    skills = cv_data.get("skills") or []
    if skills:
        story.append(Paragraph(_reshape("المهارات"), section_style))
        story.append(Paragraph(_reshape("  •  ".join(skills)), base))

    languages = cv_data.get("languages") or []
    if languages:
        story.append(Paragraph(_reshape("اللغات"), section_style))
        story.append(Paragraph(_reshape("  •  ".join(languages)), base))

    doc.build(story)
    return buffer.getvalue()
