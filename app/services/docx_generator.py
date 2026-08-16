"""
مولّد ملف Word (.docx) قابل للتعديل باستخدام python-docx
يُستخدم بعد موافقة الأدمن على الدفعة
"""
from __future__ import annotations

import io
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor


def _set_rtl(paragraph) -> None:
    """يضبط اتجاه الفقرة من اليمين لليسار (مطلوب للنصوص العربية)."""
    pPr = paragraph._p.get_or_add_pPr()
    bidi = pPr.makeelement(qn("w:bidi"), {})
    pPr.append(bidi)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT


def _add_heading(doc: Document, text: str, size: int = 14, color: RGBColor | None = None):
    p = doc.add_paragraph()
    _set_rtl(p)
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(size)
    run.font.name = "Calibri"
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.makeelement(qn("w:rFonts"), {qn("w:cs"): "Traditional Arabic"})
    rPr.append(rFonts)
    if color:
        run.font.color.rgb = color
    return p


def _add_paragraph(doc: Document, text: str, size: int = 11, bold: bool = False):
    p = doc.add_paragraph()
    _set_rtl(p)
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.makeelement(qn("w:rFonts"), {qn("w:cs"): "Traditional Arabic"})
    rPr.append(rFonts)
    return p


def generate_cv_docx(cv_data: dict[str, Any]) -> bytes:
    """يولّد ملف DOCX بالكامل في الذاكرة."""
    doc = Document()

    # اتجاه المستند بالكامل من اليمين لليسار
    section = doc.sections[0]
    sectPr = section._sectPr
    bidi = sectPr.makeelement(qn("w:bidi"), {})
    sectPr.append(bidi)

    accent = RGBColor(0x12, 0x3A, 0x5E)

    _add_heading(doc, cv_data.get("full_name", ""), size=22, color=accent)
    if cv_data.get("title"):
        _add_paragraph(doc, cv_data["title"], size=13, bold=True)

    contact = cv_data.get("contact") or {}
    contact_line = "  |  ".join(
        filter(None, [contact.get("phone"), contact.get("email"), contact.get("location")])
    )
    if contact_line:
        _add_paragraph(doc, contact_line, size=10)

    doc.add_paragraph()

    if cv_data.get("summary"):
        _add_heading(doc, "نبذة مهنية", size=14, color=accent)
        _add_paragraph(doc, cv_data["summary"])

    experience = cv_data.get("experience") or []
    if experience:
        _add_heading(doc, "الخبرات العملية", size=14, color=accent)
        for exp in experience:
            header = f"{exp.get('role', '')} — {exp.get('company', '')} ({exp.get('period', '')})"
            _add_paragraph(doc, header, size=12, bold=True)
            for bullet in exp.get("bullets", []):
                _add_paragraph(doc, f"•  {bullet}", size=10.5)

    education = cv_data.get("education") or []
    if education:
        _add_heading(doc, "التعليم", size=14, color=accent)
        for edu in education:
            line = f"{edu.get('degree', '')} — {edu.get('institution', '')} ({edu.get('period', '')})"
            _add_paragraph(doc, line)

    skills = cv_data.get("skills") or []
    if skills:
        _add_heading(doc, "المهارات", size=14, color=accent)
        _add_paragraph(doc, "  •  ".join(skills))

    languages = cv_data.get("languages") or []
    if languages:
        _add_heading(doc, "اللغات", size=14, color=accent)
        _add_paragraph(doc, "  •  ".join(languages))

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()
