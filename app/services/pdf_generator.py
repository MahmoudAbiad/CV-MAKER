"""
مولّد PDF خفيف باستخدام reportlab فقط (بدون متصفحات ثقيلة)
يدعم لغتين: العربية (RTL) والإنجليزية (LTR) بحسب cv_data["_language"]
"""
from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ملاحظة: خط Noto Sans Arabic يدعم العربية والحروف اللاتينية معاً، لذا نستخدمه للغتين
# إن كان متوفراً في assets/fonts/. إن لم يتوفر، نعود لخط Helvetica الافتراضي
# (يعمل جيداً مع الإنجليزية فقط - يُنصح بشدة بإضافة الخط لدعم العربية).
# تم اختيار Noto Sans Arabic بدلاً من Cairo لأن Cairo ينقصه بعض أشكال الحروف
# العربية (Presentation Forms) التي تحتاجها مكتبة arabic_reshaper مع reportlab،
# مما كان يسبب ظهور مربعات فارغة (tofu) بدل بعض الحروف.
_ARABIC_FONT_NAME = "NotoSansArabic"
_ARABIC_FONT_PATH = "./assets/fonts/NotoSansArabic-Regular.ttf"
_font_registered = False

# تسميات الأقسام الثابتة بكل لغة
_LABELS = {
    "ar": {
        "summary": "نبذة مهنية",
        "experience": "الخبرات العملية",
        "education": "التعليم",
        "skills": "المهارات",
        "languages": "اللغات",
    },
    "en": {
        "summary": "Professional Summary",
        "experience": "Experience",
        "education": "Education",
        "skills": "Skills",
        "languages": "Languages",
    },
}


def _ensure_font_registered() -> str:
    global _font_registered
    if _font_registered:
        return _ARABIC_FONT_NAME
    try:
        pdfmetrics.registerFont(TTFont(_ARABIC_FONT_NAME, _ARABIC_FONT_PATH))
        _font_registered = True
        return _ARABIC_FONT_NAME
    except Exception:
        # في حال عدم توفر الخط، نعود للخط الافتراضي
        return "Helvetica"


def _reshape(text: str, is_arabic: bool) -> str:
    """يعيد تشكيل النص العربي واتجاهه ليظهر بشكل صحيح داخل reportlab. لا يفعل شيئاً للإنجليزية."""
    if not is_arabic or not text:
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text


def generate_cv_pdf(cv_data: dict[str, Any]) -> bytes:
    """يولّد ملف PDF بالكامل في الذاكرة (BytesIO) دون كتابة ملفات مؤقتة على القرص."""
    language = cv_data.get("_language", "ar")
    is_arabic = language == "ar"
    labels = _LABELS.get(language, _LABELS["ar"])

    font_name = _ensure_font_registered()
    alignment = TA_RIGHT if is_arabic else TA_LEFT

    def t(text: str) -> str:
        return _reshape(text, is_arabic)

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
        "Base", parent=styles["Normal"], fontName=font_name,
        fontSize=10.5, leading=15, alignment=alignment,
    )
    name_style = ParagraphStyle(
        "Name", parent=base, fontSize=20, leading=24,
        textColor=colors.HexColor("#123A5E"), spaceAfter=2,
    )
    title_style = ParagraphStyle(
        "Title", parent=base, fontSize=13,
        textColor=colors.HexColor("#3B6EA5"), spaceAfter=10,
    )
    section_style = ParagraphStyle(
        "Section", parent=base, fontSize=13,
        textColor=colors.white, backColor=colors.HexColor("#123A5E"),
        spaceBefore=10, spaceAfter=6, borderPadding=(4, 6, 4, 6),
    )
    bullet_style = ParagraphStyle("Bullet", parent=base, fontSize=10, leftIndent=0, spaceAfter=2)
    exp_header_style = ParagraphStyle(
        "ExpHeader", parent=base, fontSize=11.5, textColor=colors.HexColor("#123A5E"), spaceBefore=6,
    )

    story: list[Any] = []

    story.append(Paragraph(t(cv_data.get("full_name", "")), name_style))
    if cv_data.get("title"):
        story.append(Paragraph(t(cv_data["title"]), title_style))

    contact = cv_data.get("contact") or {}
    contact_line = "  |  ".join(
        filter(None, [contact.get("phone"), contact.get("email"), contact.get("location")])
    )
    if contact_line:
        story.append(Paragraph(t(contact_line), base))
    story.append(Spacer(1, 8))

    if cv_data.get("summary"):
        story.append(Paragraph(t(labels["summary"]), section_style))
        story.append(Paragraph(t(cv_data["summary"]), base))

    experience = cv_data.get("experience") or []
    if experience:
        story.append(Paragraph(t(labels["experience"]), section_style))
        for exp in experience:
            header = f"{exp.get('role', '')} — {exp.get('company', '')} ({exp.get('period', '')})"
            story.append(Paragraph(t(header), exp_header_style))
            for bullet in exp.get("bullets", []):
                story.append(Paragraph(t(f"•  {bullet}"), bullet_style))

    education = cv_data.get("education") or []
    if education:
        story.append(Paragraph(t(labels["education"]), section_style))
        for edu in education:
            line = f"{edu.get('degree', '')} — {edu.get('institution', '')} ({edu.get('period', '')})"
            story.append(Paragraph(t(line), base))

    skills = cv_data.get("skills") or []
    if skills:
        story.append(Paragraph(t(labels["skills"]), section_style))
        story.append(Paragraph(t("  •  ".join(skills)), base))

    languages = cv_data.get("languages") or []
    if languages:
        story.append(Paragraph(t(labels["languages"]), section_style))
        story.append(Paragraph(t("  •  ".join(languages)), base))

    doc.build(story)
    return buffer.getvalue()
