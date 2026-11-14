"""
مولّد PDF خفيف واحترافي باستخدام ReportLab
معالجة متقدمة للطباعة العربية (RTL & BiDi)، منع تداخل النصوص،
إدارة حكيمة للهوامش والمساحات، ونظام متعدد الطبقات للخطوط البديلة.
"""
from __future__ import annotations

import html
import gc
import html
import io
import os
from pathlib import Path
from typing import Any

import arabic_reshaper
from bidi.algorithm import get_display

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
_FONTS_DIR = _BASE_DIR / "assets" / "fonts"

_FONT_CANDIDATES = [
    ("NotoSansArabic", _FONTS_DIR / "NotoSansArabic-Regular.ttf"),
    ("Cairo", _FONTS_DIR / "Cairo-Regular.ttf"),
]

_LABELS = {
    "ar": {
        "summary": "نبذة مهنية",
        "experience": "الخبرات العملية",
        "education": "التعليم",
        "skills": "المهارات",
        "languages": "اللغات",
        "contact": "معلومات التواصل",
    },
    "en": {
        "summary": "Professional Summary",
        "experience": "Experience",
        "education": "Education",
        "skills": "Skills",
        "languages": "Languages",
        "contact": "Contact",
    },
}

_active_font_name: str | None = None


def _get_registered_font() -> str:
    """يسجل أفضل خط عربي متوفر من قائمة الخطوط البديلة ويعيد اسمه."""
    global _active_font_name
    if _active_font_name:
        return _active_font_name

    registered = set(pdfmetrics.getRegisteredFontNames())

    # 1. فحص الخطوط المرفقة في assets/fonts
    for name, path in _FONT_CANDIDATES:
        if name in registered:
            _active_font_name = name
            return _active_font_name
        if path.is_file():
            try:
                pdfmetrics.registerFont(TTFont(name, str(path)))
                _active_font_name = name
                return _active_font_name
            except Exception:
                continue

    # 2. فحص خطوط النظام البديلة
    system_fallbacks = [
        ("Arial", "C:/Windows/Fonts/arial.ttf"),
        ("SegoeUI", "C:/Windows/Fonts/segoeui.ttf"),
        ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for name, path_str in system_fallbacks:
        if name in registered:
            _active_font_name = name
            return _active_font_name
        if os.path.isfile(path_str):
            try:
                pdfmetrics.registerFont(TTFont(name, path_str))
                _active_font_name = name
                return _active_font_name
            except Exception:
                continue

    _active_font_name = "Helvetica"
    return _active_font_name


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


_ARABIC_CONFIG = {
    "delete_harakat": False,
    "support_ligatures": True,
}
_SHARED_RESHAPER = arabic_reshaper.ArabicReshaper(configuration=_ARABIC_CONFIG)


def _reshape_text(text: Any, is_arabic: bool) -> str:
    """يعيد تشكيل النص العربي ومعالجة اتجاه BiDi لفقرات ReportLab بأمان وسرعة."""
    s = _clean_str(text)
    if not is_arabic or not s:
        return s
    try:
        reshaped = _SHARED_RESHAPER.reshape(s)
        return get_display(reshaped)
    except Exception:
        return s


def generate_cv_pdf(cv_data: dict[str, Any]) -> bytes:
    """يولّد ملف PDF احترافي وعالي التوافق في الذاكرة دون كتابة ملفات مؤقتة."""
    language = _clean_str(cv_data.get("_language")) or "ar"
    is_arabic = language == "ar"
    labels = _LABELS.get(language, _LABELS["ar"])

    font_name = _get_registered_font()
    alignment = TA_RIGHT if is_arabic else TA_LEFT

    def t(text: Any) -> str:
        reshaped = _reshape_text(text, is_arabic)
        return html.escape(reshaped)

    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
    )

    styles = getSampleStyleSheet()

    base = ParagraphStyle(
        "Base",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=10,
        leading=14,
        alignment=alignment,
        textColor=colors.HexColor("#2C3E50"),
    )
    name_style = ParagraphStyle(
        "Name",
        parent=base,
        fontSize=18,
        leading=22,
        textColor=colors.HexColor("#123A5E"),
        spaceAfter=3,
    )
    title_style = ParagraphStyle(
        "Title",
        parent=base,
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#2980B9"),
        spaceAfter=8,
    )
    contact_style = ParagraphStyle(
        "Contact",
        parent=base,
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#555555"),
        spaceAfter=10,
    )
    section_title_style = ParagraphStyle(
        "SectionTitle",
        parent=base,
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#123A5E"),
        spaceBefore=8,
        spaceAfter=3,
        keepWithNext=True,
    )
    exp_header_style = ParagraphStyle(
        "ExpHeader",
        parent=base,
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor("#123A5E"),
        spaceBefore=5,
        spaceAfter=2,
        keepWithNext=True,
    )
    bullet_style = ParagraphStyle(
        "Bullet",
        parent=base,
        fontSize=9.5,
        leading=13.5,
        spaceAfter=2,
        leftIndent=0 if is_arabic else 12,
        rightIndent=12 if is_arabic else 0,
    )

    story: list[Any] = []

    # 1. الترويسة
    full_name = _clean_str(cv_data.get("full_name"))
    if full_name:
        story.append(Paragraph(t(full_name), name_style))

    job_title = _clean_str(cv_data.get("title"))
    if job_title:
        story.append(Paragraph(t(job_title), title_style))

    # 2. معلومات الاتصال
    contact = cv_data.get("contact")
    if isinstance(contact, dict):
        parts = [
            _clean_str(contact.get("phone")),
            _clean_str(contact.get("email")),
            _clean_str(contact.get("location")),
        ]
        valid_parts = [p for p in parts if p]
        if valid_parts:
            contact_line = "  •  ".join(valid_parts)
            story.append(Paragraph(t(contact_line), contact_style))

    def add_section_header(title_text: str):
        story.append(Paragraph(t(title_text), section_title_style))
        story.append(
            HRFlowable(
                width="100%",
                thickness=1.2,
                color=colors.HexColor("#123A5E"),
                spaceBefore=1,
                spaceAfter=5,
            )
        )

    # 3. النبذة المهنية
    summary = _clean_str(cv_data.get("summary"))
    if summary:
        add_section_header(labels["summary"])
        story.append(Paragraph(t(summary), base))

    # 4. الخبرات العملية
    experience = cv_data.get("experience")
    if isinstance(experience, list) and experience:
        valid_exp = [e for e in experience if isinstance(e, dict)]
        if valid_exp:
            add_section_header(labels["experience"])
            for exp in valid_exp:
                role = _clean_str(exp.get("role"))
                company = _clean_str(exp.get("company"))
                period = _clean_str(exp.get("period"))

                header_parts = []
                if role and company:
                    header_parts.append(f"{role} — {company}")
                elif role or company:
                    header_parts.append(role or company)

                if period:
                    header_parts.append(f"({period})")

                header_text = " ".join(header_parts)
                if header_text:
                    story.append(Paragraph(t(header_text), exp_header_style))

                bullets = exp.get("bullets")
                if isinstance(bullets, list):
                    for b in bullets:
                        b_str = _clean_str(b)
                        if b_str:
                            story.append(Paragraph(f"• {t(b_str)}", bullet_style))

    # 5. التعليم
    education = cv_data.get("education")
    if isinstance(education, list) and education:
        valid_edu = [e for e in education if isinstance(e, dict)]
        if valid_edu:
            add_section_header(labels["education"])
            for edu in valid_edu:
                degree = _clean_str(edu.get("degree"))
                inst = _clean_str(edu.get("institution"))
                period = _clean_str(edu.get("period"))

                edu_parts = []
                if degree and inst:
                    edu_parts.append(f"{degree} — {inst}")
                elif degree or inst:
                    edu_parts.append(degree or inst)

                if period:
                    edu_parts.append(f"({period})")

                edu_text = " ".join(edu_parts)
                if edu_text:
                    story.append(Paragraph(t(edu_text), base))

    # 6. المهارات
    skills = cv_data.get("skills")
    if isinstance(skills, list):
        clean_skills = [_clean_str(s) for s in skills if _clean_str(s)]
        if clean_skills:
            add_section_header(labels["skills"])
            story.append(Paragraph(t("  •  ".join(clean_skills)), base))

    # 7. اللغات
    languages = cv_data.get("languages")
    if isinstance(languages, list):
        clean_langs = [_clean_str(l) for l in languages if _clean_str(l)]
        if clean_langs:
            add_section_header(labels["languages"])
            story.append(Paragraph(t("  •  ".join(clean_langs)), base))

    try:
        doc.build(story)
        pdf_bytes = buffer.getvalue()
        return pdf_bytes
    finally:
        buffer.close()
        del story
        del doc
        gc.collect(1)
