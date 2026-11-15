"""
مولّد ملف Word (.docx) قابل للتعديل ومتوافق 100% مع مواصفات OpenXML و Microsoft Word.
يضمن ضبط سمات w:bidi و w:rtl و w:rFonts بدون تكرار أو تشويه.
"""
from __future__ import annotations

import gc
import io
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

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

_ARABIC_FONT = "Traditional Arabic"
_LATIN_FONT = "Calibri"


def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _set_paragraph_bidi(paragraph, is_arabic: bool) -> None:
    """يضبط اتجاه ومحاذاة الفقرة وفق معيار OpenXML الصحيح."""
    pPr = paragraph._p.get_or_add_pPr()
    if is_arabic:
        bidi = pPr.find(qn("w:bidi"))
        if bidi is None:
            bidi = OxmlElement("w:bidi")
            bidi.set(qn("w:val"), "1")
            pPr.insert(0, bidi)
        else:
            bidi.set(qn("w:val"), "1")
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    else:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT


def _format_run(
    run,
    is_arabic: bool,
    font_name: str | None = None,
    size_pt: float = 11,
    bold: bool = False,
    color: RGBColor | None = None,
) -> None:
    """يضبط خصائص المقطع النصي (Run) وخطوط Complex Scripts مع منع تكرار w:rFonts."""
    run.bold = bold
    run.font.size = Pt(size_pt)
    if color:
        run.font.color.rgb = color

    rPr = run._element.get_or_add_rPr()

    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.append(rFonts)

    latin = font_name or _LATIN_FONT
    cs = _ARABIC_FONT if is_arabic else latin

    rFonts.set(qn("w:ascii"), latin)
    rFonts.set(qn("w:hAnsi"), latin)
    rFonts.set(qn("w:cs"), cs)

    if is_arabic:
        rtl = rPr.find(qn("w:rtl"))
        if rtl is None:
            rtl = OxmlElement("w:rtl")
            rtl.set(qn("w:val"), "1")
            rPr.append(rtl)
        else:
            rtl.set(qn("w:val"), "1")

        lang = rPr.find(qn("w:lang"))
        if lang is None:
            lang = OxmlElement("w:lang")
            lang.set(qn("w:bidi"), "ar-SA")
            rPr.append(lang)
        else:
            lang.set(qn("w:bidi"), "ar-SA")


def _add_styled_paragraph(
    doc: Document,
    text: str,
    is_arabic: bool,
    size_pt: float = 11,
    bold: bool = False,
    color: RGBColor | None = None,
    space_before: float = 0,
    space_after: float = 4,
    keep_with_next: bool = False,
):
    p = doc.add_paragraph()
    _set_paragraph_bidi(p, is_arabic)
    p.paragraph_format.space_before = Pt(space_before)
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.keep_with_next = keep_with_next

    if text:
        run = p.add_run(text)
        _format_run(run, is_arabic, size_pt=size_pt, bold=bold, color=color)
    return p


def _add_section_heading(doc: Document, text: str, is_arabic: bool, color: RGBColor):
    """إضافة ترويسة قسم منسقة مع خط سفلي فاصل ومسافات متوازنة."""
    p = _add_styled_paragraph(
        doc,
        text,
        is_arabic,
        size_pt=13,
        bold=True,
        color=color,
        space_before=10,
        space_after=3,
        keep_with_next=True,
    )
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "12")  # 1.5 pt
    bottom.set(qn("w:space"), "4")
    bottom.set(qn("w:color"), "123A5E")
    pBdr.append(bottom)
    pPr.append(pBdr)
    return p


def generate_cv_docx(cv_data: dict[str, Any]) -> bytes:
    """يولّد ملف DOCX بالكامل في الذاكرة بتنسيق Word متوافق ونظيف."""
    language = _clean_str(cv_data.get("_language")) or "ar"
    is_arabic = language == "ar"
    labels = _LABELS.get(language, _LABELS["ar"])
    item_sep = "\u200F  •  \u200F" if is_arabic else "  •  "

    doc = Document()

    section = doc.sections[0]
    section.page_width = Inches(8.27)
    section.page_height = Inches(11.69)
    section.top_margin = Inches(0.7)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.75)
    section.right_margin = Inches(0.75)

    if is_arabic:
        sectPr = section._sectPr
        bidi = sectPr.find(qn("w:bidi"))
        if bidi is None:
            bidi = OxmlElement("w:bidi")
            bidi.set(qn("w:val"), "1")
            sectPr.append(bidi)

    accent_color = RGBColor(0x12, 0x3A, 0x5E)
    sub_color = RGBColor(0x29, 0x80, 0xB9)

    # 1. الاسم
    full_name = _clean_str(cv_data.get("full_name"))
    if full_name:
        _add_styled_paragraph(
            doc, full_name, is_arabic, size_pt=20, bold=True, color=accent_color, space_after=2
        )

    # 2. المسمى الوظيفي
    job_title = _clean_str(cv_data.get("title"))
    if job_title:
        _add_styled_paragraph(
            doc, job_title, is_arabic, size_pt=12.5, bold=True, color=sub_color, space_after=6
        )

    # 3. معلومات التواصل
    contact = cv_data.get("contact")
    if isinstance(contact, dict):
        parts = [
            _clean_str(contact.get("phone")),
            _clean_str(contact.get("email")),
            _clean_str(contact.get("location")),
        ]
        valid_parts = [p for p in parts if p]
        if valid_parts:
            contact_line = item_sep.join(valid_parts)
            _add_styled_paragraph(doc, contact_line, is_arabic, size_pt=10, space_after=10)

    # 4. النبذة المهنية
    summary = _clean_str(cv_data.get("summary"))
    if summary:
        _add_section_heading(doc, labels["summary"], is_arabic, accent_color)
        _add_styled_paragraph(doc, summary, is_arabic, size_pt=10.5, space_after=6)

    # 5. الخبرات العملية
    experience = cv_data.get("experience")
    if isinstance(experience, list) and experience:
        valid_exp = [e for e in experience if isinstance(e, dict)]
        if valid_exp:
            _add_section_heading(doc, labels["experience"], is_arabic, accent_color)
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
                    _add_styled_paragraph(
                        doc, header_text, is_arabic, size_pt=11, bold=True, space_before=4, space_after=2
                    )

                bullets = exp.get("bullets")
                if isinstance(bullets, list):
                    for b in bullets:
                        b_str = _clean_str(b)
                        if b_str:
                            _add_styled_paragraph(
                                doc, f"•  {b_str}", is_arabic, size_pt=10, space_after=2
                            )

    # 6. التعليم
    education = cv_data.get("education")
    if isinstance(education, list) and education:
        valid_edu = [e for e in education if isinstance(e, dict)]
        if valid_edu:
            _add_section_heading(doc, labels["education"], is_arabic, accent_color)
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
                    _add_styled_paragraph(doc, edu_text, is_arabic, size_pt=10.5, space_after=4)

    # 7. المهارات
    skills = cv_data.get("skills")
    if isinstance(skills, list):
        clean_skills = [_clean_str(s) for s in skills if _clean_str(s)]
        if clean_skills:
            _add_section_heading(doc, labels["skills"], is_arabic, accent_color)
            _add_styled_paragraph(doc, item_sep.join(clean_skills), is_arabic, size_pt=10.5, space_after=4)

    # 8. اللغات
    languages = cv_data.get("languages")
    if isinstance(languages, list):
        clean_langs = [_clean_str(l) for l in languages if _clean_str(l)]
        if clean_langs:
            _add_section_heading(doc, labels["languages"], is_arabic, accent_color)
            _add_styled_paragraph(doc, item_sep.join(clean_langs), is_arabic, size_pt=10.5, space_after=4)

    # ضمان وجود فقرة واحدة على الأقل لسلامة مستند Word
    if len(doc.paragraphs) == 0:
        _add_styled_paragraph(doc, "", is_arabic)

    buffer = io.BytesIO()
    try:
        doc.save(buffer)
        docx_bytes = buffer.getvalue()
        return docx_bytes
    finally:
        buffer.close()
        del doc
        gc.collect(1)
