"""
اختبارات الوحدة والتكامل لمولّد الـ DOCX (docx_generator)
"""
from __future__ import annotations

import io
from typing import Any
import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

from app.services.docx_generator import generate_cv_docx


def test_generate_cv_docx_arabic(sample_arabic_cv_data: dict[str, Any]):
    """التحقق من إنشاء ملف DOCX عربي كامل صالح للاستخدام ببرنامج Word"""
    docx_bytes = generate_cv_docx(sample_arabic_cv_data)
    assert isinstance(docx_bytes, bytes)
    assert len(docx_bytes) > 20000

    # التحقق من إمكانية فتح الملف وقراءته عبر python-docx
    doc = Document(io.BytesIO(docx_bytes))
    all_text = " ".join(p.text for p in doc.paragraphs)
    assert "أحمد محمود العلي" in all_text
    assert "مهندس برمجيات" in all_text
    assert "جامعة دمشق" in all_text


def test_generate_cv_docx_english(sample_english_cv_data: dict[str, Any]):
    """التحقق من إنشاء ملف DOCX إنجليزي متكامل"""
    docx_bytes = generate_cv_docx(sample_english_cv_data)
    assert isinstance(docx_bytes, bytes)
    assert len(docx_bytes) > 20000

    doc = Document(io.BytesIO(docx_bytes))
    all_text = " ".join(p.text for p in doc.paragraphs)
    assert "Sarah Jenkins" in all_text
    assert "Product Manager" in all_text


def test_docx_arabic_bidi_and_rtl_attributes(sample_arabic_cv_data: dict[str, Any]):
    """
    التحقق من ضبط سمات OpenXML الخاصة بـ RTL & BiDi:
    - محاذاة الفقرات لليمين (WD_ALIGN_PARAGRAPH.RIGHT)
    - وجود عنصر w:bidi داخل w:pPr
    - وجود عنصر w:rtl داخل w:rPr للمقاطع النصية
    """
    docx_bytes = generate_cv_docx(sample_arabic_cv_data)
    doc = Document(io.BytesIO(docx_bytes))

    # فحص الفقرات النصية
    has_bidi_p = False
    for p in doc.paragraphs:
        if p.text.strip():
            pPr = p._p.find(qn("w:pPr"))
            if pPr is not None and pPr.find(qn("w:bidi")) is not None:
                has_bidi_p = True
            assert p.alignment == WD_ALIGN_PARAGRAPH.RIGHT
            break
    assert has_bidi_p, "لم يتم العثور على سمة w:bidi في فقرات المستند العربي"

    # فحص المقاطع النصية (Runs)
    has_rtl_run = False
    for p in doc.paragraphs:
        for run in p.runs:
            rPr = run._element.find(qn("w:rPr"))
            if rPr is not None and rPr.find(qn("w:rtl")) is not None:
                has_rtl_run = True
                break
        if has_rtl_run:
            break
    assert has_rtl_run, "لم يتم العثور على سمة w:rtl في مقاطع النصوص العربية"


def test_docx_special_characters_and_xml_entities(edge_case_cv_data: dict[str, Any]):
    """التحقق من عدم تلف المستند عند احتوائه على رموز XML ونصوص معقدة"""
    docx_bytes = generate_cv_docx(edge_case_cv_data)
    doc = Document(io.BytesIO(docx_bytes))
    all_text = " ".join(p.text for p in doc.paragraphs)
    assert "R&D Lead <Ahmed & Omar> \"Specialist\"" in all_text
    assert "AT&T" in all_text


def test_docx_empty_dict():
    """التحقق من التعامل الآمن مع قاموس بيانات فارغ"""
    docx_bytes = generate_cv_docx({})
    assert isinstance(docx_bytes, bytes)
    assert len(docx_bytes) > 5000
    doc = Document(io.BytesIO(docx_bytes))
    assert len(doc.paragraphs) >= 1
