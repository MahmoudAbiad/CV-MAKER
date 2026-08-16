"""
خدمة التكامل مع Google Gemini API
- استخراج وهيكلة بيانات السيرة الذاتية من نص عشوائي (Structured JSON Output)
- فحص صور إيصالات الدفع عبر Gemini Vision (Multimodal)
تتضمن معالجة أخطاء الشبكة وحالة تجاوز الحد المسموح (HTTP 429) عبر إعادة المحاولة بتأخير تصاعدي
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# مخطط JSON الذي يجب أن يلتزم به Gemini عند استخراج بيانات السيرة الذاتية
_CV_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "full_name": {"type": "string"},
        "title": {"type": "string", "description": "المسمى الوظيفي المقترح"},
        "summary": {"type": "string", "description": "ملخص احترافي قصير"},
        "contact": {
            "type": "object",
            "properties": {
                "phone": {"type": "string"},
                "email": {"type": "string"},
                "location": {"type": "string"},
            },
        },
        "experience": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "period": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "institution": {"type": "string"},
                    "degree": {"type": "string"},
                    "period": {"type": "string"},
                },
            },
        },
        "skills": {"type": "array", "items": {"type": "string"}},
        "languages": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["full_name", "summary", "experience", "skills"],
}

_SYSTEM_PROMPT = (
    "أنت خبير موارد بشرية متخصص في كتابة السير الذاتية الاحترافية. "
    "سيصلك نص عشوائي بالعامية أو الفصحى يصف خبرة شخص ما. "
    "مهمتك: استخرج المعلومات وأعد صياغتها بأسلوب احترافي يستخدم أفعال حركية قوية "
    "(مثل: قاد، طوّر، حسّن، نفّذ)، وأرجع النتيجة حصراً وفق مخطط JSON المحدد دون أي نص إضافي."
)


async def _call_gemini_with_retry(
    url: str, body: dict[str, Any], max_retries: int = 4
) -> dict[str, Any]:
    """يستدعي Gemini API مع إعادة محاولة تصاعدية عند 429 أو أعطال الشبكة المؤقتة."""
    delay = 1.5
    last_error: Exception | None = None

    async with httpx.AsyncClient(timeout=45.0) as client:
        for attempt in range(1, max_retries + 1):
            try:
                resp = await client.post(url, json=body)
                if resp.status_code == 429:
                    logger.warning("Gemini rate limit (429) - المحاولة %s/%s", attempt, max_retries)
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                logger.warning("خطأ شبكة مع Gemini (محاولة %s/%s): %s", attempt, max_retries, exc)
                await asyncio.sleep(delay)
                delay *= 2
            except httpx.HTTPStatusError as exc:
                last_error = exc
                logger.error("خطأ HTTP من Gemini: %s", exc)
                break

    raise RuntimeError(f"فشل الاتصال بـ Gemini بعد {max_retries} محاولات: {last_error}")


async def extract_cv_from_text(raw_text: str) -> dict[str, Any]:
    """يرسل نص المستخدم الخام إلى Gemini ويستقبل بيانات سيرة ذاتية مهيكلة."""
    url = f"{_GEMINI_BASE}/{settings.gemini_text_model}:generateContent?key={settings.gemini_api_key}"
    body = {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": raw_text}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": _CV_JSON_SCHEMA,
            "temperature": 0.4,
        },
    }
    data = await _call_gemini_with_retry(url, body)
    text_part = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text_part)


async def verify_payment_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """
    يستخدم Gemini Vision للتحقق مما إذا كانت الصورة إيصال دفع حقيقي (Sham Cash)
    يُعيد: {"is_receipt": bool, "confidence": float, "reason": str}
    """
    url = f"{_GEMINI_BASE}/{settings.gemini_vision_model}:generateContent?key={settings.gemini_api_key}"
    prompt = (
        "افحص هذه الصورة وحدد إن كانت إيصال تحويل مالي حقيقي (مثل إيصال Sham Cash أو تطبيق تحويلات مشابه) "
        "يحتوي على رقم عملية أو مبلغ أو تاريخ، أم أنها صورة غير متعلقة بالدفع (سكرين شوت عشوائي، صورة شخصية، إلخ). "
        "أرجع النتيجة حصراً بصيغة JSON بالمفاتيح التالية: "
        'is_receipt (boolean), confidence (رقم بين 0 و1), reason (سبب قصير بالعربية).'
    )
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}},
                ],
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": {
                "type": "object",
                "properties": {
                    "is_receipt": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["is_receipt", "confidence", "reason"],
            },
            "temperature": 0.1,
        },
    }
    data = await _call_gemini_with_retry(url, body)
    text_part = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text_part)
