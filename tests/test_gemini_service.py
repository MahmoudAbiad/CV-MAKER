"""
اختبارات الوحدة لخدمة Gemini ومعالجة النصوص وهيكلة JSON (gemini_service)
"""
from __future__ import annotations

import json
from typing import Any
import httpx
import pytest

from app.services.gemini_service import (
    _build_system_prompt,
    _call_gemini_with_retry,
    _clean_body_field,
    _clean_email_field,
    _clean_text_field,
    _extract_json_from_response,
    _has_minimum_cv_content,
    _looks_degenerate,
    _sanitize_cv_result,
    _sanitize_dialogue_result,
)


def test_looks_degenerate():
    """التحقق من كاشف التكرار والانحلال في نصوص النماذج"""
    # نصوص طبيعية سليمة
    assert not _looks_degenerate("مهندس برمجيات خبرة 5 سنوات في بايثون وداتابيز")
    assert not _looks_degenerate("0991234567")
    assert not _looks_degenerate("-----------------")
    assert not _looks_degenerate("")

    # نصوص تكرارية متحللة
    assert _looks_degenerate("مهندس مهندس مهندس مهندس مهندس")
    assert _looks_degenerate("خبرة عمل خبرة عمل خبرة عمل خبرة عمل")
    assert _looks_degenerate("aaaaaaaaaaaaaaaaaaaa")


def test_clean_text_field():
    """التحقق من تنظيف الحقول النصية القصيرة"""
    assert _clean_text_field("  أحمد محمود  \nسطر ثاني  ") == "أحمد محمود"
    
    # التحقق من القص عند تجاوز الحد الأقصى دون أن يكون النص تكرارياً متحللاً
    long_desc = "مهندس حواسيب وبرمجيات متخصص في بناء وتطوير المنظومات السحابية الحديثة والموزعة"
    assert len(_clean_text_field(long_desc, max_len=30)) <= 30
    assert _clean_text_field(None) == ""
    assert _clean_text_field(12345) == "12345"

    # التحقق من عدم مسح المهارات التي تحتوي على فواصل منقوطة
    assert _clean_text_field("Python; FastAPI; Docker", field_type="skill") == "Python; FastAPI; Docker"


def test_clean_email_field():
    """التحقق من استخراج وتطهير البريد الإلكتروني"""
    assert _clean_email_field("  user@example.com  ") == "user@example.com"
    assert _clean_email_field("Contact me at info.dev@company.sy. ") == "info.dev@company.sy"
    assert _clean_email_field("لا يوجد بريد إلكتروني") == ""
    assert _clean_email_field(None) == ""


def test_clean_body_field():
    """التحقق من تنظيف النصوص الطويلة (الملخص التنفيذي ونقاط الخبرة)"""
    long_text = "مهندس متمرس " * 20
    cleaned = _clean_body_field(long_text, max_len=100)
    assert len(cleaned) <= 100
    assert not cleaned.endswith(" ")
    assert _clean_body_field("") == ""
    assert _clean_body_field(None) == ""


def test_extract_json_from_response():
    """التحقق من استخراج كائنات JSON الصالحة من ردود Gemini النصية والـ Markdown"""
    # 1. JSON صافٍ مباشر
    raw_json = '{"status": "complete", "full_name": "أحمد"}'
    assert _extract_json_from_response(raw_json) == {"status": "complete", "full_name": "أحمد"}

    # 2. JSON داخل كود ماركداون ```json ... ```
    md_json = "```json\n{\"status\": \"needs_more_info\", \"question\": {\"text\": \"سؤال\"}}\n```"
    assert _extract_json_from_response(md_json)["status"] == "needs_more_info"

    # 3. JSON محاط بنصوص شرح قبل وبعد
    embedded = "Here is the extracted resume data:\n{\"status\": \"complete\", \"skills\": [\"Python\"]}\nHope this helps!"
    assert _extract_json_from_response(embedded)["status"] == "complete"

    # 4. نص فارغ أو غير صالح
    with pytest.raises(ValueError):
        _extract_json_from_response("")

    with pytest.raises(json.JSONDecodeError):
        _extract_json_from_response("هذا نص عادي لا يحتوي على JSON إطلاقاً")


def test_has_minimum_cv_content():
    """التحقق من فحص الحد الأدنى لمحتوى السيرة الذاتية"""
    # غير مكتملة (لا اسم)
    assert not _has_minimum_cv_content({"full_name": "", "skills": ["Python"]})

    # غير مكتملة (اسم فقط دون أي بيانات إضافية)
    assert not _has_minimum_cv_content({"full_name": "سامر"})

    # مكتملة بالحد الأدنى
    assert _has_minimum_cv_content({"full_name": "سامر", "skills": ["Python"]})
    assert _has_minimum_cv_content({"full_name": "سامر", "summary": "نبذة مهنية"})
    assert _has_minimum_cv_content({"full_name": "سامر", "education": [{"degree": "بكالوريوس"}]})


def test_build_system_prompt():
    """التحقق من تجميع برومبت النظام بمختلف اللغات والأنماط"""
    prompt_ar = _build_system_prompt("ar", "enhanced", force_complete=False)
    assert "ATS" in prompt_ar
    assert "عامية سورية مهذبة" in prompt_ar

    prompt_en = _build_system_prompt("en", "raw", force_complete=False)
    assert "Executive Resume Strategist" in prompt_en

    # التحقق من وجود توجيه الإلزام عند تفعيل force_complete
    prompt_forced = _build_system_prompt("ar", "enhanced", force_complete=True)
    assert "توجيه استثنائي" in prompt_forced


def test_sanitize_cv_result(sample_arabic_cv_data: dict[str, Any]):
    """التحقق من تطهير مخرجات السيرة الذاتية بالكامل"""
    sanitized = _sanitize_cv_result(sample_arabic_cv_data)
    assert sanitized["full_name"] == "أحمد محمود العلي"
    assert sanitized["contact"]["email"] == "ahmad.ali@example.com"
    assert len(sanitized["skills"]) > 0


@pytest.mark.asyncio
async def test_call_gemini_with_retry_on_429(monkeypatch: pytest.MonkeyPatch):
    """التحقق من آلية إعادة المحاولة عند حدوث ضغط معدل الاستدعاء 429 (Rate Limit)"""
    call_count = 0

    class MockResponse:
        def __init__(self, status_code: int, json_data: dict, headers: dict | None = None):
            self.status_code = status_code
            self._json = json_data
            self.headers = headers or {}
            self.text = json.dumps(json_data)

        def json(self):
            return self._json

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("Error", request=None, response=self)

    async def mock_post(client, url, json=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return MockResponse(429, {"error": "Rate limit"}, headers={"Retry-After": "0"})
        return MockResponse(200, {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    res = await _call_gemini_with_retry("https://dummy.api/test", {"test": True}, max_retries=3)
    assert call_count == 2
    assert "candidates" in res
