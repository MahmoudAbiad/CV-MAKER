"""
اختبارات الوحدة والتكامل لمولّد الـ PDF (pdf_generator)
"""
from __future__ import annotations

from typing import Any
import pytest

from app.services.pdf_generator import (
    _format_paragraph_text,
    _get_font_suite,
    _get_registered_font,
    _reshape_text,
    _tag_dual_fonts,
    generate_cv_pdf,
)


def test_pdf_starts_with_pdf_header(sample_arabic_cv_data: dict[str, Any]):
    """التحقق من أن الناتج هو ملف PDF صالح يبدأ بتوقيع %PDF-"""
    pdf_bytes = generate_cv_pdf(sample_arabic_cv_data)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 1000
    assert pdf_bytes.startswith(b"%PDF-")


def test_generate_cv_pdf_arabic(sample_arabic_cv_data: dict[str, Any]):
    """التحقق من سلامة توليد سيرة ذاتية عربية كاملة"""
    pdf_bytes = generate_cv_pdf(sample_arabic_cv_data)
    assert len(pdf_bytes) > 5000
    assert pdf_bytes.startswith(b"%PDF-")


def test_generate_cv_pdf_english(sample_english_cv_data: dict[str, Any]):
    """التحقق من سلامة توليد سيرة ذاتية إنجليزية كاملة"""
    pdf_bytes = generate_cv_pdf(sample_english_cv_data)
    assert len(pdf_bytes) > 5000
    assert pdf_bytes.startswith(b"%PDF-")


def test_generate_cv_pdf_with_xml_entities_and_edge_cases(edge_case_cv_data: dict[str, Any]):
    """
    التحقق من معالجة الرموز الخاصة (&, <, >, quotes) دون حدوث أخطاء بارسر ReportLab
    (Crucial Bug Regression Test)
    """
    pdf_bytes = generate_cv_pdf(edge_case_cv_data)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 2000
    assert pdf_bytes.startswith(b"%PDF-")


def test_generate_cv_pdf_empty_dict():
    """التحقق من أن تمرير قاموس فارغ لا يسقط التطبيق وينتج ملفاً صالحاً"""
    pdf_bytes = generate_cv_pdf({})
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF-")


def test_generate_cv_pdf_partial_fields():
    """التحقق من دعم السير الذاتية التي تنقصها بعض الأقسام (لا خبرات، لا تعليم)"""
    partial_data = {
        "full_name": "سالم الأحمد",
        "title": "مصمم جرافيك",
        "skills": ["Photoshop", "Illustrator"],
        "_language": "ar",
    }
    pdf_bytes = generate_cv_pdf(partial_data)
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 1000
    assert pdf_bytes.startswith(b"%PDF-")


def test_reshape_text_arabic_and_bidi():
    """التحقق من دالة تشكيل النص العربي ومعالجة BiDi"""
    text_ar = "مهندس برمجيات"
    reshaped = _reshape_text(text_ar, is_arabic=True)
    assert isinstance(reshaped, str)
    assert len(reshaped) > 0
    assert reshaped != text_ar

    text_en = "Software Engineer"
    not_reshaped = _reshape_text(text_en, is_arabic=False)
    assert not_reshaped == text_en

    assert _reshape_text("", is_arabic=True) == ""
    assert _reshape_text(None, is_arabic=True) == ""


def test_multiline_arabic_logical_line_order():
    """التحقق من عدم انعكاس ترتيب الأسطر في الفقرات العربية الطويلة (السطر الأول يبقى أولاً)"""
    ar_reg, _, lat_reg, _ = _get_font_suite()
    long_summary = (
        "البداية الأولى للفقرة المهنية في السطر الأول مع تفاصيل موسعة جداً حول هندسة البرمجيات الحديثة "
        "والنهاية الختامية للفقرة في السطر الأخير."
    )
    formatted = _format_paragraph_text(long_summary, True, 10.0, 220.0, ar_reg, lat_reg)
    lines = formatted.split("<br/>")
    assert len(lines) >= 2
    first_word_shaped = _reshape_text("البداية", is_arabic=True)
    last_word_shaped = _reshape_text("الأخير", is_arabic=True)
    assert first_word_shaped in lines[0]
    assert last_word_shaped in lines[-1]


def test_dual_font_tagging_prevents_tofu():
    """التحقق من فصل الحروف العربية عن الإنجليزية والرموز لمنع المربعات الفارغة (Tofu)"""
    ar_reg, _, lat_reg, _ = _get_font_suite()
    visual = _reshape_text("مطور Python في دمشق (ahmad@example.com)", is_arabic=True)
    tagged = _tag_dual_fonts(visual, ar_reg, lat_reg)
    assert "Python" in tagged
    assert "ahmad@example.com" in tagged
    if ar_reg != lat_reg:
        assert f'<font name="{lat_reg}">' in tagged
        assert f'<font name="{ar_reg}">' in tagged


def test_registered_font_resolution():
    """التحقق من أن نظام الخطوط يعيد خطاً صالحاً ومسجلاً"""
    font_name = _get_registered_font()
    assert isinstance(font_name, str)
    assert len(font_name) > 0


def test_pdf_generator_handles_string_skills_and_bullets():
    """التحقق من مرونة المولد وقبوله للمهارات والنقاط كسلاسل نصية بدلاً من مصفوفات"""
    cv_data = {
        "full_name": "سامر العلي",
        "title": "مطور برمجيات",
        "summary": "خبرة واسعة في البناء والتطوير البرمجي.",
        "skills": "Python, Django, PostgreSQL, Docker",
        "languages": "العربية، الإنجليزية",
        "experience": [
            {
                "role": "مبرمج بايثون",
                "company": "شركة الحلول التقنية",
                "period": "2020 - 2024",
                "bullets": "- تطوير واجهات برمجة التطبيقات\n- تحسين أداء قواعد البيانات",
            }
        ],
        "_language": "ar",
    }
    pdf_bytes = generate_cv_pdf(cv_data)
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF-")


def test_pdf_generator_fallback_on_build_failure(monkeypatch):
    """التحقق من عمل المولد الاحتياطي فائق الاعتمادية عند حدوث أي استثناء في التخطيط الأساسي"""
    from reportlab.platypus import SimpleDocTemplate

    original_build = SimpleDocTemplate.build
    call_count = 0

    def faulty_build(self, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Simulated ReportLab Layout Error")
        return original_build(self, *args, **kwargs)

    monkeypatch.setattr(SimpleDocTemplate, "build", faulty_build)

    cv_data = {
        "full_name": "طارق النجار",
        "title": "مهندس شبكات",
        "summary": "ملخص مهني تفصيلي لمهندس شبكات.",
        "skills": ["Cisco", "Linux", "Routing"],
        "_language": "ar",
    }
    pdf_bytes = generate_cv_pdf(cv_data)
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes.startswith(b"%PDF-")
    assert call_count >= 2

